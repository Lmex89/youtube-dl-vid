from pathlib import Path
from typing import List, Optional

from videos.process import (
    ProgressCallback,
    ProgressParser,
    SLOW_PROCESS_THRESHOLD_MS,
)
from videos.runner import CommandRunner


class FakeCommandRunner(CommandRunner):
    """Test double — scripts progress, output files, and failures.

    Each ``run()`` call consumes one script from *scripts* (FIFO).
    A script is a dict with optional keys:

    * ``progress`` — list of float percentages emitted via *progress_callback*
    * ``output_file`` — path to create (auto-detected from command if omitted)
    * ``output_content`` — bytes written to *output_file* (default ``b"fake"``)
    * ``fail`` — if truthy, raise ``RuntimeError`` after creating the file
    * ``error`` — error message when *fail* is truthy
    * ``timeout`` — if truthy, raise ``TimeoutError`` after creating the file
    * ``log`` — text written to *log_path*
    * ``title`` — media title written to the ``--print-to-file`` path, if present
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
        self.calls.append({"command": command, "label": label, "timeout": timeout})
        script = self._scripts.pop(0) if self._scripts else {}

        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(script.get("log", ""))

        output_file = script.get("output_file") or self._detect_output_file(command)
        if output_file:
            out = Path(output_file)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(script.get("output_content", b"fake"))

        if "--print-to-file" in command and "title" in script:
            title_file = Path(command[command.index("--print-to-file") + 2])
            title_file.write_text(script["title"])

        for pct in script.get("progress", []):
            if progress_callback:
                progress_callback(pct)

        if script.get("timeout"):
            raise TimeoutError(script.get("error", "fake timeout"))

        if script.get("fail"):
            raise RuntimeError(script.get("error", "fake failure"))

        return 0
