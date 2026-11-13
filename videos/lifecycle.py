from pathlib import Path

from django.db import transaction
from django.utils import timezone

from videos.models import CodecUrls, StatusCodec, VideosUploaded


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


@transaction.atomic
def record_success(codecurl_id: str, profile, destination: Path, title: str) -> CodecUrls:
    codecurl = CodecUrls.objects.get(pk=codecurl_id)
    VideosUploaded.objects.create(
        file_path=str(destination),
        mime_type=profile.mime_type,
        title=title[:250],
        codecurl=codecurl,
    )
    codecurl.status = StatusCodec.SUCCESS
    codecurl.progress = 100.0
    codecurl.save(update_fields=["status", "progress", "updated_at"])
    return codecurl


def mark_failed(codecurl_id: str) -> None:
    codecurl = CodecUrls.objects.filter(pk=codecurl_id).first()
    if codecurl:
        codecurl.status = StatusCodec.ERROR
        codecurl.save(update_fields=["status", "updated_at"])
