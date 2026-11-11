import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from django.conf import settings
from loguru import logger

from videos.runner import CommandRunner, get_command_runner

FFPROBE_TIMEOUT = 30  # seconds


def probe_duration(
    path: Path, runner: Optional[CommandRunner] = None
) -> Optional[float]:
    """Return media duration in seconds via the command-execution interface.

    ffprobe writes the duration to stdout, which the runner captures in the
    log file.  We read it back from there — no new return value on the
    interface.
    """
    if runner is None:
        runner = get_command_runner()

    fd, tmp = tempfile.mkstemp(suffix=".log")
    os.close(fd)
    log_path = Path(tmp)
    try:
        command = [
            settings.FFPROBE_BINARY,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
        runner.run(
            command, log_path, label="ffprobe", timeout=FFPROBE_TIMEOUT
        )
        text = log_path.read_text().strip()
        if not text:
            return None
        duration = float(text)
    except (RuntimeError, TimeoutError, subprocess.SubprocessError, ValueError, OSError):
        logger.warning(
            json.dumps({
                "event": "ffprobe_duration_failed",
                "path": str(path),
            })
        )
        return None
    finally:
        log_path.unlink(missing_ok=True)

    if duration <= 0:
        return None
    return duration
