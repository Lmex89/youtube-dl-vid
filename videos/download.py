import json
import shutil
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

TMP_DIR_NAME = ".tmp"

URL_PREVIEW_LEN = 50

_download_threads: dict = {}
_threads_lock = threading.Lock()


def _preview(url: str) -> str:
    return url[:URL_PREVIEW_LEN] + ("..." if len(url) > URL_PREVIEW_LEN else "")


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
    downloads_dir = Path(settings.DOWNLOADS_DIR)
    staging_root = downloads_dir / TMP_DIR_NAME
    staging_dir = staging_root / codecurl_id
    workdir: Optional[Path] = None
    runner = get_command_runner()

    save_progress = ProgressSaver(codecurl_id)

    try:
        codecurl = CodecUrls.objects.get(pk=codecurl_id)
        profile = get_profile(codecurl.output_profile)

        destination = downloads_dir / f"{codecurl.id}.{profile.extension}"
        log_path = Path(settings.DOWNLOAD_LOGS_DIR) / f"{codecurl.id}.txt"

        if profile.uses_temporary_files:
            workdir = staging_dir
            workdir.mkdir(parents=True, exist_ok=True)
        else:
            workdir = downloads_dir

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
                "url_preview": _preview(codecurl.url),
                "request_id": request_id,
            })
        )

        title_path = Path(settings.DOWNLOAD_LOGS_DIR) / f"{codecurl.id}.title.txt"
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
            process_log = (
                Path(settings.DOWNLOAD_LOGS_DIR)
                / f"{codecurl.id}.ffmpeg.txt"
            )
            profile.post_process(
                source,
                destination,
                process_log,
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
        if (
            codecurl_id not in {".", ".."}
            and Path(codecurl_id).name == codecurl_id
            and staging_dir.parent == staging_root
            and not staging_root.is_symlink()
            and not staging_dir.is_symlink()
        ):
            shutil.rmtree(staging_dir, ignore_errors=True)
        with _threads_lock:
            _download_threads.pop(codecurl_id, None)
        close_old_connections()


def cleanup_old_downloads(current_codecurl: CodecUrls) -> None:
    old_urls = CodecUrls.objects.filter(
        url=current_codecurl.url,
        output_profile=current_codecurl.output_profile,
    ).exclude(id=current_codecurl.id)
    old_count = old_urls.count()

    if old_count > 0:
        logger.info(
            json.dumps({
                "event": "cleaning_up_old_downloads",
                "url_preview": _preview(current_codecurl.url),
                "output_profile": current_codecurl.output_profile,
                "old_urls_count": old_count,
            })
        )

    old_uploads = VideosUploaded.objects.filter(codecurl__in=old_urls)
    deleted_count = 0

    for upload in old_uploads:
        if upload.file_path:
            Path(upload.file_path).unlink(missing_ok=True)
            deleted_count += 1
            logger.debug(f"Deleted old media file: {upload.file_path}")

    tmp_root = Path(settings.DOWNLOADS_DIR) / TMP_DIR_NAME
    for old_codecurl in old_urls:
        shutil.rmtree(tmp_root / str(old_codecurl.id), ignore_errors=True)

    old_uploads.delete()

    logger.debug(
        json.dumps({
            "event": "cleanup_completed",
            "files_deleted": deleted_count,
            "records_deleted": old_count,
        })
    )
