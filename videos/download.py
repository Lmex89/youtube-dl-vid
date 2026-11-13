import json
import threading

from django.db import close_old_connections
from loguru import logger

from videos.job_run import run_job
from videos.lifecycle import mark_started, pending_requests
from videos.models import CodecUrls
from videos.running_jobs import is_running, register_if_absent, unregister
from videos.runner import get_command_runner
from videos.storage import DownloadRetention, DownloadStorage


def is_download_running(codecurl_id: str) -> bool:
    """Public wrapper for backward compatibility."""
    return is_running(codecurl_id)


def stale_pending_jobs():
    """Pendiente jobs with no live thread, e.g. orphaned by a restart."""
    from videos.running_jobs import running_ids

    return pending_requests().exclude(pk__in=running_ids())


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

    def while_locked():
        mark_started(codecurl)
        thread.start()

    registered = register_if_absent(codecurl_id, thread, while_locked=while_locked)
    if not registered:
        logger.warning(
            json.dumps({
                "event": "download_already_running",
                "codecurl_id": codecurl_id,
                "request_id": request_id,
            })
        )
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
        unregister(codecurl_id)
        close_old_connections()
