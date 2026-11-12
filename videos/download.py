import json
import threading
from dataclasses import dataclass
from typing import Optional

from django.db import close_old_connections
from loguru import logger

from videos.job_run import run_job
from videos.models import CodecUrls, StatusCodec
from videos.runner import get_command_runner
from videos.storage import DownloadRetention, DownloadStorage

_download_threads: dict = {}
_threads_lock = threading.Lock()


def stale_pending_jobs():
    """Pendiente jobs with no live thread, e.g. orphaned by a restart."""
    with _threads_lock:
        running = list(_download_threads)
    return CodecUrls.objects.filter(status=StatusCodec.PENDING).exclude(
        pk__in=running
    )


def is_download_running(codecurl_id: str) -> bool:
    with _threads_lock:
        return codecurl_id in _download_threads


@dataclass(frozen=True)
class JobState:
    status: int  # 2=downloading, 1=success, 3=error
    progress: float  # 0-100; raw DB value so the API response stays unchanged


def get_job_state(codecurl_id: str) -> Optional[JobState]:
    """Status/progress of the job, or None if the download request doesn't exist."""
    row = (
        CodecUrls.objects.filter(pk=codecurl_id)
        .values_list("status", "progress")
        .first()
    )
    return JobState(*row) if row else None


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
    try:
        close_old_connections()
        storage = DownloadStorage()
        retention = DownloadRetention(storage, is_running=is_download_running)
        runner = get_command_runner()
        run_job(
            codecurl_id, request_id, runner=runner, storage=storage,
            retention=retention,
        )
    finally:
        with _threads_lock:
            _download_threads.pop(codecurl_id, None)
        close_old_connections()
