import json
import re
import select
import subprocess as sp
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, List, Optional

from loguru import logger

PROGRESS_POLL_INTERVAL = 1.0  # seconds
SLOW_PROCESS_THRESHOLD_MS = 500

YT_DLP_PROGRESS_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
FFMPEG_TIME_RE = re.compile(r"out_time_us=(\d+)")

ProgressCallback = Callable[[float], None]
ProgressParser = Callable[[bytes], Optional[float]]


def parse_yt_dlp_progress(chunk: bytes) -> Optional[float]:
    text = chunk.decode("utf-8", errors="replace")
    matches = YT_DLP_PROGRESS_RE.findall(text)
    if not matches:
        return None
    try:
        return float(matches[-1])
    except ValueError:
        return None


def build_progress_parser(duration: float) -> ProgressParser:
    """Return a parser that converts ffmpeg ``out_time_us`` lines to percent."""

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


def _stream_output(
    process: sp.Popen,
    log_file,
    progress_callback: Optional[ProgressCallback],
    progress_parser: Optional[ProgressParser],
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
        if progress_callback and progress_parser:
            percent = progress_parser(chunk)
            if percent is not None:
                progress_callback(percent)
    process.wait()


class CommandRunner(ABC):
    """Injectable adapter for subprocess execution (yt-dlp / ffmpeg / ffprobe)."""

    @abstractmethod
    def run(
        self,
        command: List[str],
        log_path: Path,
        label: str,
        timeout: int,
        progress_callback: Optional[ProgressCallback] = None,
        progress_parser: Optional[ProgressParser] = None,
        slow_threshold_ms: int = SLOW_PROCESS_THRESHOLD_MS,
    ) -> int:
        raise NotImplementedError


class _RealCommandRunner(CommandRunner):
    """Production adapter: streams output to the log, enforces the timeout."""

    def run(
        self,
        command: List[str],
        log_path: Path,
        label: str,
        timeout: int,
        progress_callback: Optional[ProgressCallback] = None,
        progress_parser: Optional[ProgressParser] = None,
        slow_threshold_ms: int = SLOW_PROCESS_THRESHOLD_MS,
    ) -> int:
        logger.info(
            json.dumps({
                "event": "media_process_started",
                "label": label,
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
                    _stream_output(
                        process,
                        log_file,
                        progress_callback,
                        progress_parser,
                        timeout,
                        start_time,
                    )
                    retcode = process.returncode

            duration_ms = (time.time() - start_time) * 1000

            logger.info(
                json.dumps({
                    "event": "media_process_completed",
                    "label": label,
                    "exit_code": retcode,
                    "duration_ms": round(duration_ms, 2),
                })
            )

            if duration_ms > slow_threshold_ms:
                logger.warning(
                    json.dumps({
                        "event": "slow_media_process_detected",
                        "label": label,
                        "duration_ms": round(duration_ms, 2),
                        "threshold_ms": slow_threshold_ms,
                        "exit_code": retcode,
                    })
                )

            if retcode != 0:
                logger.error(
                    json.dumps({
                        "event": "media_process_failed",
                        "label": label,
                        "exit_code": retcode,
                        "duration_ms": round(duration_ms, 2),
                    })
                )
                raise RuntimeError(f"{label} exited with code {retcode}")

            return retcode

        except sp.TimeoutExpired as exc:
            duration_ms = (time.time() - start_time) * 1000
            logger.error(
                json.dumps({
                    "event": "media_process_timed_out",
                    "label": label,
                    "timeout_seconds": timeout,
                    "duration_ms": round(duration_ms, 2),
                    "log_path": str(log_path),
                })
            )
            raise RuntimeError(f"{label} timed out after {timeout} seconds") from exc

        except Exception as exc:
            duration_ms = (time.time() - start_time) * 1000
            logger.error(
                json.dumps({
                    "event": "media_process_error",
                    "label": label,
                    "error": str(exc)[:100],
                    "error_type": exc.__class__.__name__,
                    "duration_ms": round(duration_ms, 2),
                })
            )
            raise
