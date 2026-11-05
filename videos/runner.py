import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional

from django.utils.module_loading import import_string

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


class SubprocessCommandRunner(CommandRunner):
    """Production runner — delegates to the real subprocess executor."""

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


class FakeCommandRunner(CommandRunner):
    """Test double — scripts progress, output files, and failures.

    Each ``run()`` call consumes one script from *scripts* (FIFO).
    A script is a dict with optional keys:

    * ``progress`` — list of float percentages emitted via *progress_callback*
    * ``output_file`` — path to create (auto-detected from command if omitted)
    * ``output_content`` — bytes written to *output_file* (default ``b"fake"``)
    * ``fail`` — if truthy, raise ``RuntimeError`` after creating the file
    * ``error`` — error message when *fail* is truthy
    * ``log`` — text written to *log_path*
    """

    def __init__(self, scripts=None):
        self._scripts = list(scripts or [{}])
        self.calls: List[dict] = []

    @staticmethod
    def _detect_output_file(command: List[str]) -> Optional[str]:
        """Extract output path from a yt-dlp or ffmpeg command."""
        if "-o" in command:
            idx = command.index("-o")
            if idx + 1 < len(command):
                return command[idx + 1]
        return command[-1] if command else None

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
        self.calls.append({"command": command, "label": label})
        script = self._scripts.pop(0) if self._scripts else {}

        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(script.get("log", ""))

        output_file = script.get("output_file") or self._detect_output_file(command)
        if output_file:
            out = Path(output_file)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(script.get("output_content", b"fake"))

        for pct in script.get("progress", []):
            if progress_callback:
                progress_callback(pct)

        if script.get("fail"):
            raise RuntimeError(script.get("error", "fake failure"))

        return 0


_test_runner: Optional[CommandRunner] = None


def set_test_runner(runner: Optional[CommandRunner]) -> None:
    """Override the active runner for tests. Pass ``None`` to restore."""
    global _test_runner
    _test_runner = runner


def get_command_runner() -> CommandRunner:
    """Resolve the configured runner from Django settings."""
    if _test_runner is not None:
        return _test_runner
    from django.conf import settings

    path = getattr(
        settings,
        "COMMAND_RUNNER",
        "videos.runner.SubprocessCommandRunner",
    )
    cls = import_string(path)
    return cls()
