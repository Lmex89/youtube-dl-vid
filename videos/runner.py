from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional

from videos.process import (
    ProgressCallback,
    ProgressParser,
    SLOW_PROCESS_THRESHOLD_MS,
    run_process,
)


class CommandRunner(ABC):
    """Injectable adapter for subprocess execution (yt-dlp / ffmpeg)."""

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
    """Production adapter — delegates to :func:`run_process`."""

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
        return run_process(
            command,
            log_path,
            label=label,
            timeout=timeout,
            progress_callback=progress_callback,
            progress_parser=progress_parser,
            slow_threshold_ms=slow_threshold_ms,
        )


_test_runner: Optional[CommandRunner] = None


def set_test_runner(runner: Optional[CommandRunner]) -> None:
    """Override the active runner for tests. Pass ``None`` to restore."""
    global _test_runner
    _test_runner = runner


def get_command_runner() -> CommandRunner:
    """Return the active runner (test override or real subprocess adapter)."""
    if _test_runner is not None:
        return _test_runner
    return _RealCommandRunner()
