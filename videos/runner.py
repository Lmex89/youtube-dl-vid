from typing import Optional

from videos.process import CommandRunner, _RealCommandRunner

__all__ = ["CommandRunner", "get_command_runner", "set_test_runner"]

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
