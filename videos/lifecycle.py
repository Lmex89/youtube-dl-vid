from pathlib import Path

from django.db import connection, transaction
from django.utils import timezone

from videos.models import CodecUrls, StatusCodec, VideosUploaded

_SUCCESS = StatusCodec.SUCCESS


def start(codecurl: CodecUrls) -> None:
    codecurl.status = StatusCodec.PENDING
    codecurl.progress = 0.0
    codecurl.save(update_fields=["status", "progress", "updated_at"])


def pending_requests():
    return CodecUrls.objects.filter(status=StatusCodec.PENDING)


def is_successful(codecurl: CodecUrls) -> bool:
    return codecurl.status == _SUCCESS


def successful_requests(queryset):
    return queryset.filter(status=_SUCCESS)


class ProgressReporter:
    """Clamp progress, never go backwards, and throttle small gains."""

    def __init__(
        self, codecurl_id: str, min_step: float = 1.0, download_weight: float = 1.0
    ):
        self.codecurl_id = codecurl_id
        self.min_step = min_step
        self.download_weight = download_weight
        self.last = 0.0  # start_download_async resets stored progress to 0

    def __call__(self, percent: float) -> None:
        # Only the successful completion write may store 100.
        value = max(0.0, min(99.99, percent))
        if value < self.last + self.min_step:
            return
        self.last = value
        CodecUrls.objects.filter(pk=self.codecurl_id).update(
            progress=value, updated_at=timezone.now()
        )

    def download(self, percent: float) -> None:
        self(percent * self.download_weight)

    def post_process(self, percent: float) -> None:
        base = self.download_weight * 100
        self(base + percent * (100 - base) / 100)


class SuccessCommitUncertain(Exception):
    """The success transaction may have committed despite an exception.

    `cause` is the original commit error; callers inspect it instead of
    walking the exception chain.
    """

    def __init__(self, cause: Exception) -> None:
        super().__init__("success commit outcome unknown")
        self.cause = cause


def record_success(codecurl_id: str, profile, destination: Path, title: str) -> CodecUrls:
    writes_done = False
    try:
        with transaction.atomic():
            codecurl = CodecUrls.objects.get(pk=codecurl_id)
            VideosUploaded.objects.create(
                file_path=str(destination),
                mime_type=profile.mime_type,
                title=title[:250],
                codecurl=codecurl,
            )
            codecurl.status = _SUCCESS
            codecurl.progress = 100.0
            codecurl.save(update_fields=["status", "progress", "updated_at"])
            writes_done = True
        return codecurl
    except Exception as exc:
        # An exception after the writes can only be the commit; its outcome is unknown.
        if writes_done:
            raise SuccessCommitUncertain(exc) from exc
        raise


def reconcile_success(codecurl_id: str):
    """Return the confirmed successful or failed request, or None if unknown."""
    with transaction.atomic():
        # Allow normal commits to finish without waiting indefinitely for a broken session.
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '2s'")
        codecurl = CodecUrls.objects.select_for_update().filter(pk=codecurl_id).first()
        if codecurl is None:
            return None
        uploads = VideosUploaded.objects.filter(codecurl_id=codecurl_id)
        if is_successful(codecurl):
            # The recorded upload row is authoritative; trust the database's
            # success instead of contradicting it when paths differ.
            return codecurl if uploads.exists() else None
        if codecurl.status != StatusCodec.PENDING or uploads.exists():
            return None
        codecurl.status = StatusCodec.ERROR
        codecurl.save(update_fields=["status", "updated_at"])
        return codecurl


def mark_failed(codecurl_id: str) -> None:
    # Never downgrade a recorded success: ADR 0001 ties Estado exitoso to its artifact.
    CodecUrls.objects.filter(pk=codecurl_id).exclude(status=_SUCCESS).update(
        status=StatusCodec.ERROR, updated_at=timezone.now()
    )
