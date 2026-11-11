import json
import re
import subprocess as sp
from pathlib import Path
from typing import Optional

from django.conf import settings
from loguru import logger

from videos.process import ProgressCallback, ProgressParser, run_process
from videos.runner import CommandRunner

FFPROBE_TIMEOUT = 30  # seconds
FFMPEG_TIME_RE = re.compile(r"out_time_us=(\d+)")


def probe_duration(path: Path) -> Optional[float]:
    command = [
        settings.FFPROBE_BINARY,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        result = sp.run(
            command,
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT,
        )
        duration = float(result.stdout.strip())
    except (sp.SubprocessError, ValueError):
        logger.warning(
            json.dumps({
                "event": "ffprobe_duration_failed",
                "path": str(path),
            })
        )
        return None

    if duration <= 0:
        return None
    return duration


def build_progress_parser(duration: float) -> ProgressParser:
    def parse(chunk: bytes) -> Optional[float]:
        text = chunk.decode("utf-8", errors="replace")
        matches = FFMPEG_TIME_RE.findall(text)
        if not matches:
            return None
        try:
            seconds = int(matches[-1]) / 1_000_000
        except ValueError:
            return None
        return max(0.0, min(100.0, seconds / duration * 100))

    return parse


def run_ffmpeg(
    command,
    log_path: Path,
    duration: Optional[float] = None,
    progress_callback: Optional[ProgressCallback] = None,
    runner: Optional[CommandRunner] = None,
) -> None:
    if runner is None:
        raise ValueError("run_ffmpeg requires a CommandRunner")
    progress_parser = None
    if duration and progress_callback:
        progress_parser = build_progress_parser(duration)

    runner.run(
        command,
        log_path,
        label="ffmpeg",
        timeout=settings.MEDIA_PROCESS_TIMEOUT,
        progress_callback=progress_callback if progress_parser else None,
        progress_parser=progress_parser,
    )
