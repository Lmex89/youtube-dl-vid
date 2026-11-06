import json
import threading
from pathlib import Path
from typing import Callable, List, Optional

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone
from loguru import logger

from videos.models import CodecUrls, StatusCodec, VideosUploaded
from videos.process import parse_yt_dlp_progress, run_process
from videos.profiles import get_profile
from videos.runner import CommandRunner, get_command_runner
from videos.storage import DownloadStorage, preview_url

_download_threads: dict = {}
_threads_lock = threading.Lock()


def stale_pending_jobs():
    """Pendiente jobs with no live thread, e.g. orphaned by a restart."""
    with _threads_lock:
        running = list(_download_threads)
    return CodecUrls.objects.filter(status=StatusCodec.PENDING).exclude(
        pk__in=running
    )


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


def run_download(
    command: List[str],
    log_path: Path,
    progress_callback: Optional[Callable[[float], None]] = None,
    runner: Optional[CommandRunner] = None,
) -> None:
    if runner is None:
        raise ValueError("run_download requires a CommandRunner")
    runner.run(
        command,
        log_path,
        label="yt_dlp",
        timeout=settings.MEDIA_PROCESS_TIMEOUT,
        progress_callback=progress_callback,
        progress_parser=parse_yt_dlp_progress,
    )

    with open(log_path, "r", errors="replace") as f:
        for line in f:
            logger.debug(line.rstrip())


def is_download_running(codecurl_id: str) -> bool:
    with _threads_lock:
        return codecurl_id in _download_threads


def start_download_async(codecurl: CodecUrls, request_id: str = "unknown") -> CodecUrls:
    """Start the job for *codecurl* unless one is already registered.

    The check and registration are one atomic step; a duplicate start is a
    no-op that returns the same Solicitud de descarga.
    """
    codecurl_id = str(codecurl.id)
    thread = threading.Thread(
        target=_async_download_worker,
        args=(codecurl_id, request_id),
        name=f"download-{codecurl_id[:8]}",
        daemon=True,
    )
    with _threads_lock:
        if codecurl_id in _download_threads:
            logger.warning(
                json.dumps({
                    "event": "download_already_running",
                    "codecurl_id": codecurl_id,
                    "request_id": request_id,
                })
            )
            return codecurl
        # Register last so a failed save/start never leaves a stale entry;
        # the worker's cleanup waits on this lock, so it can't pop early.
        codecurl.status = StatusCodec.PENDING
        codecurl.progress = 0.0
        codecurl.save(update_fields=["status", "progress", "updated_at"])
        thread.start()
        _download_threads[codecurl_id] = thread
    return codecurl


def _async_download_worker(codecurl_id: str, request_id: str) -> None:
    close_old_connections()
    storage = DownloadStorage(is_running=is_download_running)
    runner = get_command_runner()

    save_progress = ProgressSaver(codecurl_id)

    try:
        codecurl = CodecUrls.objects.get(pk=codecurl_id)
        profile = get_profile(codecurl.output_profile)
        paths = storage.job_paths(codecurl_id, profile)

        destination = paths.final
        log_path = paths.log
        workdir = paths.workdir
        if profile.uses_temporary_files:
            workdir.mkdir(parents=True, exist_ok=True)

        download_weight = profile.download_weight

        def on_download_progress(percent: float) -> None:
            save_progress(percent * download_weight)

        def on_process_progress(percent: float) -> None:
            base = download_weight * 100
            save_progress(base + percent * (100 - base) / 100)

        logger.info(
            json.dumps({
                "event": "download_pipeline_started",
                "codecurl_id": codecurl_id,
                "output_profile": profile.name,
                "url_preview": preview_url(codecurl.url),
                "request_id": request_id,
            })
        )

        title_path = paths.title_log
        title_path.unlink(missing_ok=True)
        command = profile.build_command(codecurl.url, destination, workdir)
        command += ["--print-to-file", "%(title)s", str(title_path)]
        run_download(
            command,
            log_path,
            progress_callback=on_download_progress,
            runner=runner,
        )

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

        try:
            storage.supersede_earlier(codecurl, request_id=request_id)
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

        logger.info(
            json.dumps({
                "event": "video_download_completed",
                "codecurl_id": codecurl_id,
                "file_path": str(destination),
                "output_profile": profile.name,
                "request_id": request_id,
            })
        )
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
        codecurl = CodecUrls.objects.filter(pk=codecurl_id).first()
        if codecurl:
            codecurl.status = StatusCodec.ERROR
            codecurl.save(update_fields=["status", "updated_at"])
    finally:
        storage.discard_staging(codecurl_id)
        with _threads_lock:
            _download_threads.pop(codecurl_id, None)
        close_old_connections()
