import subprocess as sp
from pathlib import Path
from typing import List

from loguru import logger

from videos.models import CodecUrls, VideosUploaded

COMMAND_YT_DLP = [
    "yt-dlp",
    "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]",
    "-S", "res,ext:mp4:m4a",
    "--merge-output-format", "mp4",
    "--no-cache-dir",
]


def build_command(url: str, output_path: Path) -> List[str]:
    return [*COMMAND_YT_DLP, url, "-o", str(output_path)]


def run_download(command: List[str], log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w") as f:
        retcode = sp.call(command, stdout=f)
    if retcode != 0:
        raise RuntimeError(f"yt-dlp exited with code {retcode}")
    with open(log_path) as f:
        for line in f:
            logger.info(line.rstrip())


def cleanup_old_downloads(current_codecurl: CodecUrls) -> None:
    old_urls = CodecUrls.objects.filter(url=current_codecurl.url).exclude(
        id=current_codecurl.id
    )
    old_uploads = VideosUploaded.objects.filter(codecurl__in=old_urls)
    for upload in old_uploads:
        if upload.video_path:
            Path(upload.video_path).unlink(missing_ok=True)
    old_uploads.delete()
