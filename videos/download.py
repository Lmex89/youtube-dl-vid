import json
import logging
import subprocess as sp
import time
from pathlib import Path
from typing import List

from loguru import logger

from videos.models import CodecUrls, VideosUploaded

logger = logging.getLogger('videos')

COMMAND_YT_DLP = [
    "yt-dlp",
    "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]",
    "-S", "res,ext:mp4:m4a",
    "--merge-output-format", "mp4",
    "--no-cache-dir",
]

SLOW_DOWNLOAD_THRESHOLD = 500  # ms


def build_command(url: str, output_path: Path) -> List[str]:
    logger.debug(
        json.dumps({
            "event": "building_yt_dlp_command",
            "url_preview": url[:50] + "..." if len(url) > 50 else url,
            "output_path": str(output_path),
        })
    )
    return [*COMMAND_YT_DLP, url, "-o", str(output_path)]


def run_download(command: List[str], log_path: Path) -> None:
    logger.info(
        json.dumps({
            "event": "starting_video_download",
            "command_preview": " ".join(command[:4]),
            "log_path": str(log_path),
        })
    )
    
    start_time = time.time()
    
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "w") as f:
            retcode = sp.call(command, stdout=f)
        
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
        
        with open(log_path) as f:
            for line in f:
                logger.debug(line.rstrip())
                
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
