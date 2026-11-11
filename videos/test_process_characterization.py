"""
Characterization tests for the real command executor in videos/process.py.

These tests use tiny OS commands (shell/python one-liners) to verify the actual
subprocess execution behavior, not mocks or fakes.
"""
import json
import tempfile
import time
from pathlib import Path

from django.test import TestCase
from loguru import logger as loguru_logger

from videos.process import build_progress_parser, parse_yt_dlp_progress, run_process


class RunProcessSuccessTests(TestCase):
    """Success returns exit code 0 and the per-job log holds the tool output."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log_path = Path(self._tmp.name) / "test.log"

    def test_echo_returns_zero_and_captures_output(self):
        retcode = run_process(
            command=["echo", "hello"],
            log_path=self.log_path,
            label="test_echo",
            timeout=10,
        )

        self.assertEqual(retcode, 0)
        self.assertTrue(self.log_path.exists())
        log_content = self.log_path.read_bytes()
        self.assertIn(b"hello", log_content)

    def test_python_print_returns_zero_and_captures_output(self):
        retcode = run_process(
            command=["python", "-c", "print('hello from python')"],
            log_path=self.log_path,
            label="test_python",
            timeout=10,
        )

        self.assertEqual(retcode, 0)
        log_content = self.log_path.read_bytes()
        self.assertIn(b"hello from python", log_content)


class RunProcessFailureTests(TestCase):
    """A non-zero exit raises an error naming the label and exit code."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log_path = Path(self._tmp.name) / "test.log"

    def test_nonzero_exit_raises_runtime_error_with_label_and_code(self):
        with self.assertRaises(RuntimeError) as cm:
            run_process(
                command=["python", "-c", "import sys; sys.exit(2)"],
                log_path=self.log_path,
                label="test_failure",
                timeout=10,
            )

        error_msg = str(cm.exception)
        self.assertIn("test_failure", error_msg)
        self.assertIn("2", error_msg)

    def test_exit_code_1_raises_runtime_error(self):
        with self.assertRaises(RuntimeError) as cm:
            run_process(
                command=["python", "-c", "import sys; sys.exit(1)"],
                log_path=self.log_path,
                label="another_label",
                timeout=10,
            )

        self.assertIn("another_label", str(cm.exception))
        self.assertIn("1", str(cm.exception))


class RunProcessTimeoutTests(TestCase):
    """A command outliving the timeout is killed and raises the proper error."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log_path = Path(self._tmp.name) / "test.log"

    def test_timeout_kills_process_and_raises_runtime_error(self):
        start = time.time()
        with self.assertRaises(RuntimeError) as cm:
            run_process(
                command=["sleep", "10"],
                log_path=self.log_path,
                label="test_timeout",
                timeout=2,
            )
        elapsed = time.time() - start

        error_msg = str(cm.exception)
        self.assertIn("test_timeout", error_msg)
        self.assertIn("timed out after 2 seconds", error_msg)
        self.assertLess(elapsed, 5.0)  # Should not wait full 10 seconds


class YtDlpProgressParserTests(TestCase):
    """The yt-dlp progress parser delivers percentages through the real executor."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log_path = Path(self._tmp.name) / "test.log"

    def test_parser_extracts_percentage_from_download_line(self):
        chunk = b"[download] 42.5%\n"
        percent = parse_yt_dlp_progress(chunk)
        self.assertEqual(percent, 42.5)

    def test_parser_returns_none_when_no_match(self):
        chunk = b"some other output\n"
        percent = parse_yt_dlp_progress(chunk)
        self.assertIsNone(percent)

    def test_progress_callback_receives_percentages_via_real_executor(self):
        # Script that prints yt-dlp-style progress lines
        script = """
import time
for i in range(5):
    print(f'[download] {i*25}%')
    time.sleep(0.05)
"""
        received = []

        retcode = run_process(
            command=["python", "-c", script],
            log_path=self.log_path,
            label="yt_dlp_test",
            timeout=10,
            progress_callback=lambda pct: received.append(pct),
            progress_parser=parse_yt_dlp_progress,
        )

        self.assertEqual(retcode, 0)
        self.assertGreater(len(received), 0)
        self.assertIn(0.0, received)
        self.assertIn(100.0, received)


class FfmpegProgressParserTests(TestCase):
    """The ffmpeg progress parser delivers percentages through the real executor."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log_path = Path(self._tmp.name) / "test.log"

    def test_build_progress_parser_extracts_time_and_computes_percentage(self):
        parser = build_progress_parser(duration=10.0)
        # out_time_us is in microseconds
        chunk = b"out_time_us=5000000\n"  # 5 seconds
        percent = parser(chunk)
        self.assertEqual(percent, 50.0)

    def test_build_progress_parser_clamps_to_100(self):
        parser = build_progress_parser(duration=10.0)
        chunk = b"out_time_us=15000000\n"  # 15 seconds > 10s duration
        percent = parser(chunk)
        self.assertEqual(percent, 100.0)

    def test_progress_callback_receives_ffmpeg_percentages_via_real_executor(self):
        # Script that prints ffmpeg-style progress lines
        # Duration = 10 seconds, so out_time_us=5000000 = 50%
        script = """
import time
for i in range(5):
    print(f'out_time_us={i * 2500000}')
    time.sleep(0.05)
"""
        received = []
        parser = build_progress_parser(duration=10.0)

        retcode = run_process(
            command=["python", "-c", script],
            log_path=self.log_path,
            label="ffmpeg_test",
            timeout=10,
            progress_callback=lambda pct: received.append(pct),
            progress_parser=parser,
        )

        self.assertEqual(retcode, 0)
        self.assertGreater(len(received), 0)
        self.assertIn(0.0, received)
        self.assertIn(100.0, received)


class JsonLoggingEventTests(TestCase):
    """The started / completed / failed / timed-out JSON events are emitted."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log_path = Path(self._tmp.name) / "test.log"
        self.messages = []
        self.sink = loguru_logger.add(self.messages.append, level="INFO")
        self.addCleanup(loguru_logger.remove, self.sink)

    def _get_events(self):
        return [json.loads(m.record["message"]) for m in self.messages]

    def test_success_emits_started_and_completed_events(self):
        run_process(
            command=["echo", "test"],
            log_path=self.log_path,
            label="success_test",
            timeout=10,
        )

        events = self._get_events()
        started = [e for e in events if e.get("event") == "media_process_started"]
        completed = [e for e in events if e.get("event") == "media_process_completed"]

        self.assertEqual(len(started), 1)
        self.assertEqual(started[0]["label"], "success_test")
        self.assertIn("command_preview", started[0])
        self.assertIn("log_path", started[0])
        self.assertIn("timeout_seconds", started[0])

        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["label"], "success_test")
        self.assertEqual(completed[0]["exit_code"], 0)
        self.assertIn("duration_ms", completed[0])

    def test_failure_emits_started_and_failed_events(self):
        with self.assertRaises(RuntimeError):
            run_process(
                command=["python", "-c", "import sys; sys.exit(1)"],
                log_path=self.log_path,
                label="failure_test",
                timeout=10,
            )

        events = self._get_events()
        started = [e for e in events if e.get("event") == "media_process_started"]
        failed = [e for e in events if e.get("event") == "media_process_failed"]

        self.assertEqual(len(started), 1)
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["label"], "failure_test")
        self.assertEqual(failed[0]["exit_code"], 1)
        self.assertIn("duration_ms", failed[0])

    def test_timeout_emits_started_and_timed_out_events(self):
        with self.assertRaises(RuntimeError):
            run_process(
                command=["sleep", "10"],
                log_path=self.log_path,
                label="timeout_test",
                timeout=2,
            )

        events = self._get_events()
        started = [e for e in events if e.get("event") == "media_process_started"]
        timed_out = [e for e in events if e.get("event") == "media_process_timed_out"]

        self.assertEqual(len(started), 1)
        self.assertEqual(len(timed_out), 1)
        self.assertEqual(timed_out[0]["label"], "timeout_test")
        self.assertEqual(timed_out[0]["timeout_seconds"], 2)
        self.assertIn("duration_ms", timed_out[0])
        self.assertIn("log_path", timed_out[0])


class SlowProcessWarningTests(TestCase):
    """The slow-process warning is emitted when duration exceeds threshold."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log_path = Path(self._tmp.name) / "test.log"
        self.messages = []
        self.sink = loguru_logger.add(self.messages.append, level="WARNING")
        self.addCleanup(loguru_logger.remove, self.sink)

    def test_slow_process_emits_warning_event(self):
        # Command that takes > 500ms
        run_process(
            command=["sleep", "1"],
            log_path=self.log_path,
            label="slow_test",
            timeout=10,
            slow_threshold_ms=500,
        )

        events = [json.loads(m.record["message"]) for m in self.messages]
        slow = [e for e in events if e.get("event") == "slow_media_process_detected"]

        self.assertEqual(len(slow), 1)
        self.assertEqual(slow[0]["label"], "slow_test")
        self.assertIn("duration_ms", slow[0])
        self.assertIn("threshold_ms", slow[0])
        self.assertEqual(slow[0]["threshold_ms"], 500)
        self.assertIn("exit_code", slow[0])
        self.assertEqual(slow[0]["exit_code"], 0)
        self.assertGreater(slow[0]["duration_ms"], 500)

    def test_fast_process_does_not_emit_slow_warning(self):
        run_process(
            command=["echo", "fast"],
            log_path=self.log_path,
            label="fast_test",
            timeout=10,
            slow_threshold_ms=500,
        )

        events = [json.loads(m.record["message"]) for m in self.messages]
        slow = [e for e in events if e.get("event") == "slow_media_process_detected"]

        self.assertEqual(len(slow), 0)
