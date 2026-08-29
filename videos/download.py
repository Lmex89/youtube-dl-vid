import json
import re
import select
import subprocess as sp
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

from django.db import close_old_connections
from loguru import logger

from videos.models import CodecUrls, StatusCodec, VideosUploaded

COMMAND_YT_DLP = [
    "yt-dlp",
    "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "-S", "res,ext:mp4:m4a",
    "--merge-output-format", "mp4",
    "--no-cache-dir",
    "--socket-timeout", "30",
    "--newline",
]

SLOW_DOWNLOAD_THRESHOLD = 500  # ms
DOWNLOAD_TIMEOUT = 300  # seconds
PROGRESS_POLL_INTERVAL = 1.0  # seconds

PROGRESS_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")

_download_threads: dict = {}
_threads_lock = threading.Lock()


def build_command(url: str, output_path: Path) -> List[str]:
    logger.debug(
        json.dumps({
            "event": "building_yt_dlp_command",
            "url_preview": url[:50] + "..." if len(url) > 50 else url,
            "output_path": str(output_path),
        })
    )
    return [*COMMAND_YT_DLP, url, "-o", str(output_path)]


def _parse_percent(chunk: bytes) -> Optional[float]:
    try:
        text = chunk.decode("utf-8", errors="replace")
    except Exception:
        return None
    match = PROGRESS_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _stream_process_output(
    process: sp.Popen,
    log_file,
    progress_callback: Optional[Callable[[float], None]],
    timeout: int,
    start_time: float,
) -> None:
    while True:
        if time.time() - start_time > timeout:
            process.kill()
            process.wait()
            raise sp.TimeoutExpired(cmd=process.args, timeout=timeout)
        ready, _, _ = select.select(
            [process.stdout], [], [], PROGRESS_POLL_INTERVAL
        )
        if not ready:
            continue
        chunk = process.stdout.read1()
        if not chunk:
            break
        log_file.write(chunk)
        log_file.flush()
        if progress_callback:
            percent = _parse_percent(chunk)
            if percent is not None:
                progress_callback(percent)
    process.wait()


def run_download(
    command: List[str],
    log_path: Path,
    progress_callback: Optional[Callable[[float], None]] = None,
    timeout: int = DOWNLOAD_TIMEOUT,
) -> None:
    logger.info(
        json.dumps({
            "event": "starting_video_download",
            "command_preview": " ".join(command[:4]),
            "log_path": str(log_path),
            "timeout_seconds": timeout,
        })
    )

    start_time = time.time()

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "wb") as log_file:
            with sp.Popen(command, stdout=sp.PIPE, stderr=sp.STDOUT) as process:
                _stream_process_output(
                    process, log_file, progress_callback, timeout, start_time
                )
                retcode = process.returncode

        duration_ms = (time.time() - start_time) * 1000

        logger.info(
            json.dumps({
                "event": "yt_dlp_completed",
                "exit_code": retcode,
                "duration_ms": round(duration_ms, 2),
                "duration_s": round(duration_ms / 1000, 2),
            })
        )

        if duration_ms > SLOW_DOWNLOAD_THRESHOLD:
            logger.warning(
                json.dumps({
                    "event": "slow_download_detected",
                    "duration_ms": round(duration_ms, 2),
                    "threshold_ms": SLOW_DOWNLOAD_THRESHOLD,
                    "exit_code": retcode,
                })
            )

        if retcode != 0:
            logger.error(
                json.dumps({
                    "event": "yt_dlp_failed",
                    "exit_code": retcode,
                    "duration_ms": round(duration_ms, 2),
                })
            )
            raise RuntimeError(f"yt-dlp exited with code {retcode}")

        with open(log_path, "r", errors="replace") as f:
            for line in f:
                logger.debug(line.rstrip())

    except sp.TimeoutExpired as exc:
        duration_ms = (time.time() - start_time) * 1000
        logger.error(
            json.dumps({
                "event": "download_timed_out",
                "timeout_seconds": timeout,
                "duration_ms": round(duration_ms, 2),
                "log_path": str(log_path),
            })
        )
        raise RuntimeError(f"yt-dlp timed out after {timeout} seconds") from exc

    except Exception as exc:
        duration_ms = (time.time() - start_time) * 1000
        logger.exception(
            json.dumps({
                "event": "download_process_failed",
                "error": str(exc),
                "error_type": exc.__class__.__name__,
                "duration_ms": round(duration_ms, 2),
            })
        )
        raise


def is_download_running(codecurl_id: str) -> bool:
    thread = _download_threads.get(codecurl_id)
    return bool(thread and thread.is_alive())


def start_download_async(
    codecurl: CodecUrls,
    url: str,
    output_path: Path,
    log_path: Path,
    request_id: str = "unknown",
) -> None:
    codecurl_id = str(codecurl.id)
    if is_download_running(codecurl_id):
        logger.warning(
            json.dumps({
                "event": "download_already_running",
                "codecurl_id": codecurl_id,
                "request_id": request_id,
            })
        )
        return

    codecurl.status = StatusCodec.PENDING
    codecurl.progress = 0.0
    codecurl.save(update_fields=["status", "progress", "updated_at"])

    thread = threading.Thread(
        target=_async_download_worker,
        args=(codecurl_id, url, output_path, log_path, request_id),
        name=f"download-{codecurl_id[:8]}",
        daemon=True,
    )
    with _threads_lock:
        _download_threads[codecurl_id] = thread
    thread.start()


def _async_download_worker(
    codecurl_id: str,
    url: str,
    output_path: Path,
    log_path: Path,
    request_id: str,
) -> None:
    close_old_connections()

    def progress_callback(percent: float) -> None:
        codecurl = CodecUrls.objects.filter(pk=codecurl_id).first()
        if codecurl:
            codecurl.progress = percent
            codecurl.save(update_fields=["progress", "updated_at"])

    try:
        command = build_command(url, output_path)
        run_download(command, log_path, progress_callback=progress_callback)

        codecurl = CodecUrls.objects.get(pk=codecurl_id)
        VideosUploaded.objects.create(
            video_path=str(output_path),
            title="Test",
            codecurl=codecurl,
        )
        codecurl.status = StatusCodec.SUCCESS
        codecurl.progress = 100.0
        codecurl.save(update_fields=["status", "progress", "updated_at"])

        logger.info(
            json.dumps({
                "event": "video_download_completed",
                "codecurl_id": codecurl_id,
                "video_path": str(output_path),
                "request_id": request_id,
            })
        )
    except Exception as exc:
        logger.error(
            json.dumps({
                "event": "video_download_failed",
                "codecurl_id": codecurl_id,
                "url_preview": url[:50] + "..." if len(url) > 50 else url,
                "error": str(exc),
                "error_type": exc.__class__.__name__,
                "request_id": request_id,
            })
        )
        codecurl = CodecUrls.objects.filter(pk=codecurl_id).first()
        if codecurl:
            codecurl.status = StatusCodec.ERROR
            codecurl.save(update_fields=["status", "updated_at"])
    finally:
        with _threads_lock:
            _download_threads.pop(codecurl_id, None)
        close_old_connections()


def cleanup_old_downloads(current_codecurl: CodecUrls) -> None:
    old_urls = CodecUrls.objects.filter(url=current_codecurl.url).exclude(
        id=current_codecurl.id
    )
    old_count = old_urls.count()

    if old_count > 0:
        logger.info(
            json.dumps({
                "event": "cleaning_up_old_downloads",
                "url_preview": current_codecurl.url[:50] + "..." if len(current_codecurl.url) > 50 else current_codecurl.url,
                "old_urls_count": old_count,
            })
        )

    old_uploads = VideosUploaded.objects.filter(codecurl__in=old_urls)
    deleted_count = 0

    for upload in old_uploads:
        if upload.video_path:
            Path(upload.video_path).unlink(missing_ok=True)
            deleted_count += 1
            logger.debug(f"Deleted old video file: {upload.video_path}")

    old_uploads.delete()

    logger.debug(
        json.dumps({
            "event": "cleanup_completed",
            "files_deleted": deleted_count,
            "records_deleted": old_count,
        })
    )