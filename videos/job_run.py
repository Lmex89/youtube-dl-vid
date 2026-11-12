import json
from pathlib import Path

from django.conf import settings
from django.utils import timezone
from loguru import logger

from videos.models import CodecUrls, StatusCodec, VideosUploaded
from videos.process import parse_yt_dlp_progress
from videos.profiles import get_profile
from videos.storage import preview_url


class ProgressSaver:
    """Clamp to 0-100, never go backwards, and write only on >= min_step gains."""

    def __init__(self, codecurl_id: str, min_step: float = 1.0):
        self.codecurl_id = codecurl_id
        self.min_step = min_step
        self.last = 0.0  # start_download_async resets stored progress to 0

    def __call__(self, percent: float) -> None:
        value = max(0.0, min(100.0, percent))
        if value < self.last + self.min_step and not (value == 100.0 > self.last):
            return
        self.last = value
        CodecUrls.objects.filter(pk=self.codecurl_id).update(
            progress=value, updated_at=timezone.now()
        )


def _fetch_and_process(codecurl, profile, paths, save_progress, runner) -> str:
    """Download, post-process and return the video title."""
    destination = paths.final
    workdir = paths.workdir
    if profile.uses_temporary_files:
        workdir.mkdir(parents=True, exist_ok=True)

    download_weight = profile.download_weight

    def on_download_progress(percent: float) -> None:
        save_progress(percent * download_weight)

    def on_process_progress(percent: float) -> None:
        base = download_weight * 100
        save_progress(base + percent * (100 - base) / 100)

    title_path = paths.title_log
    title_path.unlink(missing_ok=True)
    command = profile.build_command(codecurl.url, destination, workdir)
    command += ["--print-to-file", "%(title)s", str(title_path)]
    runner.run(
        command,
        paths.log,
        label="yt_dlp",
        timeout=settings.MEDIA_PROCESS_TIMEOUT,
        progress_callback=on_download_progress,
        progress_parser=parse_yt_dlp_progress,
    )

    with open(paths.log, "r", errors="replace") as f:
        for line in f:
            logger.debug(line.rstrip())

    source = profile.locate_source(workdir, destination)
    if profile.uses_temporary_files:
        profile.post_process(
            source,
            destination,
            paths.process_log,
            on_process_progress,
            runner=runner,
        )

    title = (
        title_path.read_text(errors="replace").strip()
        if title_path.exists()
        else ""
    ) or destination.stem
    title_path.unlink(missing_ok=True)
    return title


def _record_success(codecurl_id: str, profile, destination: Path, title: str) -> CodecUrls:
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


def _mark_failed(codecurl_id: str) -> None:
    codecurl = CodecUrls.objects.filter(pk=codecurl_id).first()
    if codecurl:
        codecurl.status = StatusCodec.ERROR
        codecurl.save(update_fields=["status", "updated_at"])


def run_job(codecurl_id: str, request_id: str, *, runner, storage, retention) -> str:
    """Run one download synchronously and return succeeded, failed or discarded."""
    save_progress = ProgressSaver(codecurl_id)

    try:
        codecurl = CodecUrls.objects.get(pk=codecurl_id)
        profile = get_profile(codecurl.output_profile)
        paths = storage.job_paths(codecurl_id, profile)
        destination = paths.final

        logger.info(
            json.dumps({
                "event": "download_pipeline_started",
                "codecurl_id": codecurl_id,
                "output_profile": profile.name,
                "url_preview": preview_url(codecurl.url),
                "request_id": request_id,
            })
        )

        title = _fetch_and_process(codecurl, profile, paths, save_progress, runner)
        codecurl = _record_success(codecurl_id, profile, destination, title)

        discarded = False
        try:
            discarded = retention.discard_if_superseded(
                codecurl, request_id=request_id
            )
            if not discarded:
                retention.supersede_earlier(codecurl, request_id=request_id)
        except Exception as exc:
            logger.exception(
                json.dumps({
                    "event": "download_supersede_failed",
                    "codecurl_id": codecurl_id,
                    "error": str(exc)[:100],
                    "error_type": exc.__class__.__name__,
                    "request_id": request_id,
                })
            )

        if not discarded:
            logger.info(
                json.dumps({
                    "event": "video_download_completed",
                    "codecurl_id": codecurl_id,
                    "file_path": str(destination),
                    "output_profile": profile.name,
                    "request_id": request_id,
                })
            )
        return "discarded" if discarded else "succeeded"
    except Exception as exc:
        logger.error(
            json.dumps({
                "event": "video_download_failed",
                "codecurl_id": codecurl_id,
                "error": str(exc)[:100],
                "error_type": exc.__class__.__name__,
                "request_id": request_id,
            })
        )
        _mark_failed(codecurl_id)
        return "failed"
    finally:
        storage.discard_staging(codecurl_id)
