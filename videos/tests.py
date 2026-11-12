import json
import tempfile
import threading
import time
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from loguru import logger as loguru_logger
from django.core.cache import caches
from django.core.management import call_command
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from videos.download import (
    JobState,
    get_job_state,
    is_download_running,
    stale_pending_jobs,
    start_download_async,
)
from videos.job_run import run_job
from videos.media import probe_duration
from videos.models import CodecUrls, OutputProfile, StatusCodec, VideosUploaded
from videos.profiles import PROFILES, get_profile
from videos.runner import set_test_runner
from videos.test_utils import FakeCommandRunner
from videos.storage import (
    STAGING_DIR_NAME,
    DownloadRetention,
    DownloadStorage,
    OrphanCounts,
)


class ProgressParserTests(TestCase):
    """Progress parsers must return the LATEST value when a single read
    contains multiple progress lines (read1() returns up to N bytes)."""

    def test_yt_dlp_returns_latest_when_chunk_has_multiple_lines(self):
        from videos.process import parse_yt_dlp_progress

        chunk = b"[download] 10.0%\n[download] 50.5%\n[download] 90.0%"
        self.assertEqual(parse_yt_dlp_progress(chunk), 90.0)

    def test_yt_dlp_single_line_still_works(self):
        from videos.process import parse_yt_dlp_progress

        self.assertEqual(parse_yt_dlp_progress(b"[download] 42.5%"), 42.5)

    def test_yt_dlp_returns_none_when_no_match(self):
        from videos.process import parse_yt_dlp_progress

        self.assertIsNone(parse_yt_dlp_progress(b"no progress here"))

    def test_ffmpeg_returns_latest_when_chunk_has_multiple_lines(self):
        from videos.process import build_progress_parser

        # 100s duration, three out_time_us values: 10s, 50s, 90s
        chunk = (
            b"out_time_us=10000000\n"
            b"out_time_us=50000000\n"
            b"out_time_us=90000000\n"
        )
        parser = build_progress_parser(100.0)
        self.assertEqual(parser(chunk), 90.0)

    def test_ffmpeg_single_value_still_works(self):
        from videos.process import build_progress_parser

        parser = build_progress_parser(100.0)
        self.assertEqual(parser(b"out_time_us=42500000"), 42.5)

    def test_ffmpeg_returns_none_when_no_match(self):
        from videos.process import build_progress_parser

        parser = build_progress_parser(100.0)
        self.assertIsNone(parser(b"no progress here"))

    def test_ffmpeg_clamps_to_100_when_elapsed_exceeds_duration(self):
        from videos.process import build_progress_parser

        parser = build_progress_parser(10.0)
        chunk = b"out_time_us=5000000\nout_time_us=20000000"
        self.assertEqual(parser(chunk), 100.0)


class ProbeDurationTests(TestCase):
    """Every unknown-duration outcome returns None and emits the warning."""

    def _probe(self, script):
        messages = []
        sink = loguru_logger.add(messages.append, level="WARNING")
        self.addCleanup(loguru_logger.remove, sink)
        result = probe_duration(
            Path("/x.mp4"), runner=FakeCommandRunner(scripts=[script])
        )
        events = [json.loads(m.record["message"])["event"] for m in messages]
        return result, events

    def test_valid_duration_returns_float_without_warning(self):
        self.assertEqual(self._probe({"log": "10.5\n"}), (10.5, []))

    def test_unknown_duration_returns_none_and_warns(self):
        cases = {
            "empty": {},
            "non-numeric": {"log": "N/A"},
            "zero": {"log": "0.0"},
            "negative": {"log": "-3"},
            "ffprobe failure": {"fail": True},
        }
        for name, script in cases.items():
            with self.subTest(name):
                self.assertEqual(
                    self._probe(script), (None, ["ffprobe_duration_failed"])
                )


class FakeCommandRunnerParserTests(TestCase):
    """FakeCommandRunner routes scripted progress through the parser."""

    def _run_with(self, scripts, progress_parser, progress_callback):
        import tempfile

        runner = FakeCommandRunner(scripts)
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "test.log"
            runner.run(
                command=["fake-cmd", "-o", str(Path(tmp) / "out.mp4")],
                log_path=log_path,
                label="yt_dlp",
                timeout=30,
                progress_callback=progress_callback,
                progress_parser=progress_parser,
            )
        return runner

    def test_progress_lines_are_routed_through_parser(self):
        """Scripted bytes go through progress_parser, not straight to callback."""
        from videos.process import parse_yt_dlp_progress

        received = []
        self._run_with(
            scripts=[{"progress": [b"[download] 25%", b"[download] 75%"]}],
            progress_parser=parse_yt_dlp_progress,
            progress_callback=received.append,
        )
        self.assertEqual(received, [25.0, 75.0])

    def test_parser_returning_none_does_not_call_callback(self):
        """Non-matching output lines produce no callback."""
        from videos.process import parse_yt_dlp_progress

        received = []
        self._run_with(
            scripts=[{"progress": [b"no progress here", b"[download] 50%"]}],
            progress_parser=parse_yt_dlp_progress,
            progress_callback=received.append,
        )
        self.assertEqual(received, [50.0])

    def test_no_parser_means_no_callback(self):
        """Without a parser, progress lines are silently ignored."""
        received = []
        self._run_with(
            scripts=[{"progress": [b"[download] 50%"]}],
            progress_parser=None,
            progress_callback=received.append,
        )
        self.assertEqual(received, [])

    def test_ffmpeg_parser_is_wired(self):
        """ffmpeg output lines go through the ffmpeg parser."""
        from videos.process import build_progress_parser

        received = []
        parser = build_progress_parser(duration=10.0)
        self._run_with(
            scripts=[{"progress": [b"out_time_us=5000000"]}],
            progress_parser=parser,
            progress_callback=received.append,
        )
        self.assertEqual(received, [50.0])


class ProfileRegistryTests(TestCase):
    def test_supported_profiles_are_registered(self):
        self.assertEqual(
            sorted(PROFILES.keys()),
            ["audio_m4a", "standard_video", "whatsapp_video"],
        )

    def test_invalid_profile_raises_value_error(self):
        with self.assertRaises(ValueError):
            get_profile("not_a_profile")

    def test_profile_extensions_and_mime_types(self):
        self.assertEqual(
            get_profile(OutputProfile.STANDARD_VIDEO).mime_type, "video/mp4"
        )
        self.assertEqual(
            get_profile(OutputProfile.WHATSAPP_VIDEO).mime_type, "video/mp4"
        )
        audio = get_profile(OutputProfile.AUDIO_M4A)
        self.assertEqual(audio.extension, "m4a")
        self.assertEqual(audio.mime_type, "audio/mp4")


class BuildCommandTests(TestCase):
    def test_standard_profile_downloads_to_destination(self):
        destination = Path("/tmp/downloads/video.mp4")
        command = get_profile(OutputProfile.STANDARD_VIDEO).build_command(
            "https://example.com/video",
            destination,
            Path("/tmp/downloads"),
        )
        self.assertIn(str(destination), command)
        self.assertIn(
            "bestvideo[height<=720]+bestaudio/best[height<=720]/best", command
        )
        self.assertIn("--merge-output-format", command)
        self.assertIn("--no-cache-dir", command)
        self.assertIn("--newline", command)

    def test_whatsapp_profile_downloads_to_temporary_source(self):
        command = get_profile(OutputProfile.WHATSAPP_VIDEO).build_command(
            "https://example.com/video",
            Path("/tmp/downloads/video.mp4"),
            Path("/tmp/downloads") / STAGING_DIR_NAME,
        )
        output_template = command[command.index("-o") + 1]
        self.assertTrue(output_template.endswith("source.%(ext)s"))
        self.assertNotIn("/tmp/downloads/video.mp4", command)

    def test_audio_profile_extracts_m4a(self):
        command = get_profile(OutputProfile.AUDIO_M4A).build_command(
            "https://example.com/video",
            Path("/tmp/downloads/audio.m4a"),
            Path("/tmp/downloads") / STAGING_DIR_NAME,
        )
        self.assertIn("-x", command)
        self.assertEqual(command[command.index("--audio-format") + 1], "m4a")
        self.assertEqual(command[command.index("--audio-quality") + 1], "128K")
        self.assertIn("bestaudio/best", command)

    def test_whatsapp_ffmpeg_command_is_cloud_api_compatible(self):
        profile = get_profile(OutputProfile.WHATSAPP_VIDEO)
        command = profile._build_ffmpeg_command(
            source=Path("/tmp/source.mp4"),
            destination=Path("/tmp/out.mp4"),
            duration=60.0,
            height=720,
            audio_kbps=128,
            target_bytes=16 * 1024 * 1024,
        )
        self.assertIn("libx264", command)
        self.assertIn("aac", command)
        self.assertIn("yuv420p", command)
        self.assertIn("+faststart", command)
        self.assertEqual(command[command.index("-bf") + 1], "0")
        self.assertEqual(command[command.index("-profile:v") + 1], "main")
        self.assertEqual(command[command.index("-ac") + 1], "2")
        self.assertEqual(
            command[-1], str(Path("/tmp/out.mp4"))
        )

    def test_whatsapp_target_bitrate_respects_size_limit(self):
        profile = get_profile(OutputProfile.WHATSAPP_VIDEO)
        target_bytes = 16 * 1024 * 1024
        video_kbps = profile._target_video_kbps(600.0, 128, target_bytes)
        total_kbps = target_bytes * 8 / 600.0 / 1000
        self.assertLess(video_kbps, total_kbps)
        self.assertGreaterEqual(video_kbps, profile.MIN_VIDEO_KBPS)

    def test_whatsapp_target_bitrate_without_duration(self):
        profile = get_profile(OutputProfile.WHATSAPP_VIDEO)
        self.assertIsNone(profile._target_video_kbps(None, 128, 1024))


class DownloadStorageTests(TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.downloads = root / "downloads"
        self.logs = root / "logs"
        self.storage = DownloadStorage(
            downloads_dir=self.downloads, logs_dir=self.logs
        )
        self.codecurl_id = "1234abcd-0000-0000-0000-000000000000"

    def test_job_paths_place_final_staging_and_logs_under_injected_roots(self):
        profile = get_profile(OutputProfile.WHATSAPP_VIDEO)

        paths = self.storage.job_paths(self.codecurl_id, profile)

        self.assertEqual(
            paths.final, self.downloads / f"{self.codecurl_id}.mp4"
        )
        self.assertEqual(
            paths.workdir,
            self.downloads / STAGING_DIR_NAME / self.codecurl_id,
        )
        self.assertEqual(paths.log, self.logs / f"{self.codecurl_id}.txt")
        self.assertEqual(
            paths.process_log, self.logs / f"{self.codecurl_id}.ffmpeg.txt"
        )
        self.assertEqual(
            paths.title_log, self.logs / f"{self.codecurl_id}.title.txt"
        )

    def test_standard_profile_works_in_downloads_root(self):
        profile = get_profile(OutputProfile.STANDARD_VIDEO)

        paths = self.storage.job_paths(self.codecurl_id, profile)

        self.assertEqual(paths.workdir, self.downloads)
        self.assertEqual(
            paths.final, self.downloads / f"{self.codecurl_id}.mp4"
        )

    def test_discard_staging_removes_only_that_job_and_keeps_root(self):
        job = self.downloads / STAGING_DIR_NAME / self.codecurl_id
        other = self.downloads / STAGING_DIR_NAME / "another-job"
        job.mkdir(parents=True)
        (job / "partial.mp4").write_text("partial")
        other.mkdir(parents=True)
        keep = self.downloads / "keep.txt"
        keep.write_text("keep")

        self.storage.discard_staging(self.codecurl_id)
        self.storage.discard_staging(self.codecurl_id)

        self.assertFalse(job.exists())
        self.assertTrue(other.exists())
        self.assertTrue((self.downloads / STAGING_DIR_NAME).exists())
        self.assertTrue(keep.exists())

    def test_discard_staging_never_removes_the_staging_root(self):
        self.downloads.mkdir(parents=True)

        self.storage.discard_staging("..")
        self.storage.discard_staging("")

        self.assertTrue(self.downloads.exists())


class VideosUploadedPostTests(TestCase):
    def setUp(self):
        caches["ratelimit"].clear()
        self.client = APIClient()
        self.url = "/api/v1/yt/videos-uploaded/"

    @patch("videos.views.start_download_async")
    def test_post_without_url_returns_400(self, mocked_start):
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, 400)
        mocked_start.assert_not_called()

    @patch("videos.views.start_download_async")
    def test_post_defaults_to_standard_video(self, mocked_start):
        response = self.client.post(
            self.url, {"url": "https://example.com/v"}, format="json"
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["output_profile"], "standard_video")
        codecurl = CodecUrls.objects.get(id=response.data["id"])
        self.assertEqual(
            codecurl.output_profile, OutputProfile.STANDARD_VIDEO
        )
        mocked_start.assert_called_once()

    @patch("videos.views.start_download_async")
    def test_post_accepts_explicit_standard_video(self, mocked_start):
        response = self.client.post(
            self.url,
            {"url": "https://example.com/v", "output_profile": "standard_video"},
            format="json",
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["output_profile"], "standard_video")

    @patch("videos.views.start_download_async")
    def test_post_blank_profile_falls_back_to_standard_video(self, mocked_start):
        response = self.client.post(
            self.url,
            {"url": "https://example.com/v", "output_profile": ""},
            format="json",
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["output_profile"], "standard_video")

    @patch("videos.views.start_download_async")
    def test_post_null_profile_falls_back_to_standard_video(self, mocked_start):
        response = self.client.post(
            self.url,
            {"url": "https://example.com/v", "output_profile": None},
            format="json",
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["output_profile"], "standard_video")

    @patch("videos.views.start_download_async")
    def test_post_accepts_whatsapp_profile(self, mocked_start):
        response = self.client.post(
            self.url,
            {"url": "https://example.com/v", "output_profile": "whatsapp_video"},
            format="json",
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["output_profile"], "whatsapp_video")

    @patch("videos.views.start_download_async")
    def test_post_accepts_audio_profile(self, mocked_start):
        response = self.client.post(
            self.url,
            {"url": "https://example.com/v", "output_profile": "audio_m4a"},
            format="json",
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["output_profile"], "audio_m4a")

    @patch("videos.views.start_download_async")
    def test_post_rejects_unknown_profile(self, mocked_start):
        response = self.client.post(
            self.url,
            {"url": "https://example.com/v", "output_profile": "hacked"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("allowed", response.data)
        self.assertFalse(CodecUrls.objects.exists())
        mocked_start.assert_not_called()


class VideosUploadedFileTests(TestCase):
    def setUp(self):
        caches["ratelimit"].clear()
        self.client = APIClient()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.downloads = Path(self._tmp.name) / "downloads"
        self.downloads.mkdir()
        self._settings = override_settings(DOWNLOADS_DIR=self.downloads)
        self._settings.enable()
        self.addCleanup(self._settings.disable)

    def _upload_with_file(
        self, profile, content=b"media-bytes", mime_type=""
    ):
        codecurl = CodecUrls.objects.create(
            url="https://example.com/v", output_profile=profile
        )
        path = self.downloads / f"{codecurl.id}.{get_profile(profile).extension}"
        path.write_bytes(content)
        return VideosUploaded.objects.create(
            file_path=str(path), mime_type=mime_type, codecurl=codecurl
        )

    def test_standard_video_served_with_profile_mime_and_extension(self):
        upload = self._upload_with_file(
            OutputProfile.STANDARD_VIDEO, mime_type="application/octet-stream"
        )

        response = self.client.get(
            f"/api/v1/yt/videos-uploaded/{upload.id}"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "video/mp4")
        self.assertIn(f"{upload.id}.mp4", response["Content-Disposition"])
        self.assertEqual(
            b"".join(response.streaming_content), b"media-bytes"
        )

    def test_whatsapp_video_served_with_profile_mime_and_extension(self):
        upload = self._upload_with_file(
            OutputProfile.WHATSAPP_VIDEO, mime_type="video/quicktime"
        )

        response = self.client.get(
            f"/api/v1/yt/videos-uploaded/{upload.id}"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "video/mp4")
        self.assertIn(f"{upload.id}.mp4", response["Content-Disposition"])
        self.assertEqual(
            b"".join(response.streaming_content), b"media-bytes"
        )

    def test_audio_m4a_served_with_profile_mime_and_extension(self):
        upload = self._upload_with_file(
            OutputProfile.AUDIO_M4A, content=b"audio-bytes"
        )

        response = self.client.get(
            f"/api/v1/yt/videos-uploaded/{upload.id}"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "audio/mp4")
        self.assertIn(f"{upload.id}.m4a", response["Content-Disposition"])
        self.assertEqual(
            b"".join(response.streaming_content), b"audio-bytes"
        )

    def test_download_returns_404_when_file_missing(self):
        codecurl = CodecUrls.objects.create(url="https://example.com/v")
        upload = VideosUploaded.objects.create(
            file_path=str(self.downloads / f"{codecurl.id}.mp4"),
            mime_type="video/mp4",
            codecurl=codecurl,
        )

        response = self.client.get(f"/api/v1/yt/videos-uploaded/{upload.id}")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data, {"error": "file not found"})

    def test_download_returns_404_when_stored_path_is_empty(self):
        codecurl = CodecUrls.objects.create(url="https://example.com/v")

        for file_path in ("", None):
            with self.subTest(file_path=file_path):
                upload = VideosUploaded.objects.create(
                    file_path=file_path, codecurl=codecurl
                )
                response = self.client.get(
                    f"/api/v1/yt/videos-uploaded/{upload.id}"
                )
                self.assertEqual(response.status_code, 404)
                self.assertEqual(
                    response.data, {"error": "file not found"}
                )

    def test_null_codecurl_falls_back_to_stored_mime_and_suffix(self):
        path = self.downloads / "legacy.m4a"
        path.write_bytes(b"legacy-bytes")
        upload = VideosUploaded.objects.create(
            file_path=str(path), mime_type="audio/mp4", codecurl=None
        )

        response = self.client.get(f"/api/v1/yt/videos-uploaded/{upload.id}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "audio/mp4")
        self.assertIn(f"{upload.id}.m4a", response["Content-Disposition"])
        self.assertEqual(
            b"".join(response.streaming_content), b"legacy-bytes"
        )

    def test_serializer_exposes_file_metadata(self):
        codecurl = CodecUrls.objects.create(url="https://example.com/v")
        VideosUploaded.objects.create(
            file_path="/tmp/video.mp4",
            mime_type="video/mp4",
            codecurl=codecurl,
        )
        response = self.client.get("/api/v1/yt/videos-uploaded/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data[0]["video"], "/tmp/video.mp4")
        self.assertEqual(response.data[0]["mime_type"], "video/mp4")


def _wait_for_download(codecurl_id: str, timeout: float = 5.0) -> None:
    """Poll until the background download thread finishes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_download_running(codecurl_id):
            return
        time.sleep(0.02)
    raise TimeoutError(f"download {codecurl_id} did not finish in {timeout}s")


class ResolveLegacyFallbackTests(TestCase):
    def setUp(self):
        self.storage = DownloadStorage(
            downloads_dir=Path("/dl"), logs_dir=Path("/logs")
        )

    def test_legacy_row_without_profile_uses_stored_mime_and_suffix(self):
        upload = VideosUploaded.objects.create(
            file_path="/dl/legacy.m4a", mime_type="audio/mp4", codecurl=None
        )

        served = self.storage.resolve(upload)

        self.assertEqual(served.mime_type, "audio/mp4")
        self.assertEqual(served.filename, f"{upload.id}.m4a")

    def test_row_with_profile_uses_profile_mime_and_extension(self):
        codecurl = CodecUrls.objects.create(
            url="https://example.com/v",
            output_profile=OutputProfile.AUDIO_M4A,
        )
        upload = VideosUploaded.objects.create(
            file_path="/dl/x.mp4", mime_type="video/mp4", codecurl=codecurl
        )

        served = self.storage.resolve(upload)

        self.assertEqual(served.mime_type, "audio/mp4")
        self.assertEqual(served.filename, f"{upload.id}.m4a")


class DownloadJobTestCase(TransactionTestCase):
    """Real thread path against a real test DB and temp downloads dir."""

    def setUp(self):
        caches["ratelimit"].clear()
        self.client = APIClient()
        self._tmp = tempfile.TemporaryDirectory()
        self._downloads = Path(self._tmp.name) / "downloads"
        self._downloads.mkdir()
        self._logs = Path(self._tmp.name) / "logs"
        self._logs.mkdir()
        self._settings = override_settings(
            DOWNLOADS_DIR=self._downloads,
            DOWNLOAD_LOGS_DIR=self._logs,
        )
        self._settings.enable()

    def tearDown(self):
        self._settings.disable()
        self._tmp.cleanup()
        set_test_runner(None)


class DirectDownloadJobTestCase(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._downloads = Path(tmp.name) / "downloads"
        self._logs = Path(tmp.name) / "logs"
        self.storage = DownloadStorage(self._downloads, self._logs)

    def run_job(self, codecurl, runner):
        return run_job(
            str(codecurl.id), "request-49", runner=runner, storage=self.storage,
            retention=DownloadRetention(self.storage),
        )


class StandardVideoEndToEndTests(DownloadJobTestCase):
    """End-to-end HTTP test: POST → background job → GET status.

    Uses FakeCommandRunner so no real yt-dlp / ffmpeg runs.
    """

    def test_standard_video_success_end_to_end(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingRunner(FakeCommandRunner):
            def run(self, *args, **kwargs):
                entered.set()
                release.wait(5)
                return super().run(*args, **kwargs)

        runner = BlockingRunner(
            scripts=[{"progress": [b"[download] 50%", b"[download] 100%"], "title": "Real Title"}]
        )
        set_test_runner(runner)

        post = self.client.post(
            "/api/v1/yt/videos-uploaded/",
            {"url": "https://example.com/video"},
            format="json",
        )
        self.assertEqual(post.status_code, 202)
        codecurl_id = post.data["id"]
        try:
            self.assertEqual(post.data["status"], "pending")
            self.assertEqual(post.data["url"], "https://example.com/video")
            self.assertEqual(post.data["output_profile"], "standard_video")
            self.assertTrue(entered.wait(5))
            pending = self.client.get(f"/api/v1/yt/videos/{codecurl_id}")
            self.assertEqual(pending.status_code, 200)
            self.assertEqual(pending.data["status"], StatusCodec.PENDING)
            self.assertEqual(pending.data["progress"], 0.0)
        finally:
            release.set()
            _wait_for_download(codecurl_id)

        get = self.client.get(f"/api/v1/yt/videos/{codecurl_id}")
        self.assertEqual(get.status_code, 200)
        self.assertEqual(get.data["status"], StatusCodec.SUCCESS)
        self.assertEqual(get.data["progress"], 100.0)

        upload = VideosUploaded.objects.get(codecurl_id=codecurl_id)
        self.assertTrue(upload.file_path.endswith(f"{codecurl_id}.mp4"))
        self.assertEqual(upload.mime_type, "video/mp4")
        self.assertEqual(upload.title, "Real Title")
        self.assertTrue(Path(upload.file_path).exists())
        file_response = self.client.get(f"/api/v1/yt/videos-uploaded/{upload.id}")
        self.assertEqual(file_response.status_code, 200)
        b"".join(file_response.streaming_content)

        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0]["label"], "yt_dlp")



class SuccessOrderingTests(DirectDownloadJobTestCase):
    """Estado exitoso is only ever written after VideosUploaded exists."""

    def test_success_is_saved_only_when_upload_record_exists(self):
        codecurl = CodecUrls.objects.create(url="https://example.com/video")
        runner = FakeCommandRunner(scripts=[{"progress": [b"[download] 100%"]}])
        seen = []
        real_save = CodecUrls.save

        def spy(instance, *args, **kwargs):
            if instance.status == StatusCodec.SUCCESS:
                seen.append(
                    VideosUploaded.objects.filter(codecurl_id=instance.pk).exists()
                )
            return real_save(instance, *args, **kwargs)

        with patch.object(CodecUrls, "save", spy):
            outcome = self.run_job(codecurl, runner)

        self.assertEqual(outcome, "succeeded")
        self.assertEqual(seen, [True])

    def test_failed_upload_record_never_yields_success(self):
        codecurl = CodecUrls.objects.create(url="https://example.com/video")
        runner = FakeCommandRunner(scripts=[{"progress": [b"[download] 100%"]}])
        with patch.object(
            VideosUploaded.objects, "create", side_effect=RuntimeError("db down")
        ):
            outcome = self.run_job(codecurl, runner)

        codecurl.refresh_from_db()
        self.assertEqual(outcome, "failed")
        self.assertEqual(codecurl.status, StatusCodec.ERROR)
        self.assertFalse(VideosUploaded.objects.exists())


class StorageLayoutJobTests(DirectDownloadJobTestCase):
    """Jobs keep final files and logs under the injected roots."""

    def test_standard_success_uses_storage_layout(self):
        runner = FakeCommandRunner(scripts=[{"progress": [b"[download] 100%"]}])
        codecurl = CodecUrls.objects.create(url="https://example.com/video")
        outcome = self.run_job(codecurl, runner)
        self.assertEqual(outcome, "succeeded")
        codecurl_id = str(codecurl.id)

        upload = VideosUploaded.objects.get(codecurl_id=codecurl_id)
        self.assertEqual(
            upload.file_path, str(self._downloads / f"{codecurl_id}.mp4")
        )
        self.assertTrue((self._downloads / f"{codecurl_id}.mp4").exists())
        self.assertTrue((self._logs / f"{codecurl_id}.txt").exists())
        self.assertFalse(
            (self._downloads / STAGING_DIR_NAME / codecurl_id).exists()
        )

    def test_whatsapp_success_uses_storage_layout_for_process_log(self):
        runner = FakeCommandRunner(
            scripts=[
                {"progress": [b"[download] 100%"]},
                {},  # ffprobe: unknown duration
                {"progress": [b"out_time_us=10000000"]},
            ]
        )
        codecurl = CodecUrls.objects.create(
            url="https://example.com/video", output_profile=OutputProfile.WHATSAPP_VIDEO,
        )
        outcome = self.run_job(codecurl, runner)
        self.assertEqual(outcome, "succeeded")
        codecurl_id = str(codecurl.id)

        upload = VideosUploaded.objects.get(codecurl_id=codecurl_id)
        self.assertEqual(
            upload.file_path, str(self._downloads / f"{codecurl_id}.mp4")
        )
        self.assertTrue((self._logs / f"{codecurl_id}.txt").exists())
        self.assertTrue((self._logs / f"{codecurl_id}.ffmpeg.txt").exists())
        self.assertFalse(
            (self._downloads / STAGING_DIR_NAME / codecurl_id).exists()
        )


@override_settings(WHATSAPP_MEDIA_MAX_BYTES=100)
class ReencodeProfileJobTests(DirectDownloadJobTestCase):
    def _run(self, profile, scripts):
        if profile == "whatsapp_video":
            scripts = [scripts[0], {"log": "10.0"}, *scripts[1:]]
        runner = FakeCommandRunner(scripts)
        codecurl = CodecUrls.objects.create(
            url="https://example.com/video", output_profile=profile
        )
        outcome = self.run_job(codecurl, runner)
        codecurl.refresh_from_db()
        return codecurl, runner, outcome

    def test_whatsapp_ladder_steps_down_until_fit(self):
        codecurl, runner, outcome = self._run(
            "whatsapp_video",
            [
                {"progress": [b"[download] 100%"]},
                {"progress": [b"out_time_us=10000000"], "output_content": b"x" * 500},
                {"progress": [b"out_time_us=10000000"], "output_content": b"x" * 500},
                {"progress": [b"out_time_us=10000000"], "output_content": b"x" * 50},
            ],
        )
        ffmpeg = [c["command"] for c in runner.calls if c["label"] == "ffmpeg"]
        self.assertEqual(len(ffmpeg), 3)
        self.assertIn("scale=-2:'min(360,ih)'", ffmpeg[-1])
        self.assertEqual(outcome, "succeeded")
        self.assertEqual(codecurl.status, StatusCodec.SUCCESS)
        self.assertEqual(
            Path(VideosUploaded.objects.get(codecurl=codecurl).file_path).stat().st_size,
            50,
        )

    def test_whatsapp_errors_when_no_ladder_step_fits(self):
        big = {"progress": [b"out_time_us=10000000"], "output_content": b"x" * 500}
        codecurl, runner, outcome = self._run(
            "whatsapp_video", [{"progress": [b"[download] 100%"]}, big, big, big]
        )
        self.assertEqual(outcome, "failed")
        self.assertEqual(codecurl.status, StatusCodec.ERROR)
        self.assertFalse(VideosUploaded.objects.filter(codecurl=codecurl).exists())


class GetJobStateTests(TestCase):
    def test_returns_state_for_each_status(self):
        for status, progress in [(2, 42.5), (1, 100.0), (3, 7.0)]:
            c = CodecUrls.objects.create(
                url="https://example.com", status=status, progress=progress
            )
            self.assertEqual(get_job_state(str(c.id)), JobState(status, progress))

    def test_unknown_id_returns_none(self):
        import uuid

        self.assertIsNone(get_job_state(str(uuid.uuid4())))

    def test_detail_view_reads_through_get_job_state(self):
        c = CodecUrls.objects.create(url="https://example.com", status=2, progress=10.0)
        with patch("videos.views.get_job_state", return_value=JobState(1, 99.0)):
            data = APIClient().get(f"/api/v1/yt/videos/{c.id}").json()
        self.assertEqual((data["status"], data["progress"]), (1, 99.0))


class ProgressSaverTests(TestCase):
    def _saver(self):
        from videos.job_run import _ProgressSaver

        codecurl = CodecUrls.objects.create(url="https://example.com")
        return codecurl, _ProgressSaver(str(codecurl.id))

    def test_clamps_out_of_range(self):
        codecurl, save = self._saver()
        save(150.0)
        codecurl.refresh_from_db()
        self.assertEqual(codecurl.progress, 100.0)
        save(-5.0)
        codecurl.refresh_from_db()
        self.assertEqual(codecurl.progress, 100.0)

    def test_never_decreases(self):
        codecurl, save = self._saver()
        save(60.0)
        save(20.0)
        codecurl.refresh_from_db()
        self.assertEqual(codecurl.progress, 60.0)

    def test_noisy_stream_is_throttled(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        codecurl, save = self._saver()
        stream = [i / 10 for i in range(1001)]  # 0.0 .. 100.0 in 0.1 steps
        with CaptureQueriesContext(connection) as ctx:
            for pct in stream:
                save(pct)
        writes = [q for q in ctx.captured_queries if q["sql"].startswith("UPDATE")]
        self.assertLessEqual(len(writes), 101)
        codecurl.refresh_from_db()
        self.assertEqual(codecurl.progress, 100.0)

    def test_completion_bypasses_minimum_step(self):
        codecurl, save = self._saver()
        save(99.5)
        save(100.0)
        codecurl.refresh_from_db()
        self.assertEqual(codecurl.progress, 100.0)


class SupersedeOnSuccessTests(DownloadJobTestCase):
    """A successful job replaces earlier jobs for the same URL and profile."""

    def _post(self, url, profile=None):
        payload = {"url": url}
        if profile:
            payload["output_profile"] = profile
        post = self.client.post(
            "/api/v1/yt/videos-uploaded/", payload, format="json"
        )
        self.assertEqual(post.status_code, 202)
        return post.data["id"]

    def _artifact_paths(self, codecurl_id, extension="mp4"):
        return {
            "final": self._downloads / f"{codecurl_id}.{extension}",
            "log": self._logs / f"{codecurl_id}.txt",
            "process_log": self._logs / f"{codecurl_id}.ffmpeg.txt",
            "title_log": self._logs / f"{codecurl_id}.title.txt",
            "staging": self._downloads / STAGING_DIR_NAME / codecurl_id,
        }

    def test_post_does_not_delete_previous_file_while_job_is_pending(self):
        set_test_runner(FakeCommandRunner(scripts=[{"progress": [b"[download] 100%"]}]))
        first_id = self._post("https://example.com/same")
        _wait_for_download(first_id)
        first = self._artifact_paths(first_id)
        first_upload = VideosUploaded.objects.get(codecurl_id=first_id)

        class BlockingRunner(FakeCommandRunner):
            def __init__(self):
                super().__init__(scripts=[{"progress": [b"[download] 100%"]}])
                self.entered = threading.Event()
                self.release = threading.Event()

            def run(self, *args, **kwargs):
                self.entered.set()
                self.release.wait(5)
                return super().run(*args, **kwargs)

        runner = BlockingRunner()
        set_test_runner(runner)
        second_id = self._post("https://example.com/same")
        self.assertTrue(runner.entered.wait(5))
        self.assertTrue(first["final"].exists())
        self.assertTrue(
            VideosUploaded.objects.filter(pk=first_upload.pk).exists()
        )

        runner.release.set()
        _wait_for_download(second_id)

        self.assertFalse(first["final"].exists())
        self.assertFalse(
            VideosUploaded.objects.filter(pk=first_upload.pk).exists()
        )
        self.assertTrue(
            VideosUploaded.objects.filter(codecurl_id=second_id).exists()
        )


class RunningJobProtectionTests(DownloadJobTestCase):
    """Supersession must leave jobs that are still running alone."""

    URL = "https://example.com/same"

    class GatedRunner(FakeCommandRunner):
        """Block the first completed run so its job stays registered."""

        def __init__(self, scripts=None):
            super().__init__(scripts=scripts)
            self.entered = threading.Event()
            self.release = threading.Event()
            self._gated = False

        def run(self, *args, **kwargs):
            result = super().run(*args, **kwargs)
            if not self._gated:
                self._gated = True
                self.entered.set()
                self.release.wait(5)
            return result

    def _post(self, profile=None):
        payload = {"url": self.URL}
        if profile:
            payload["output_profile"] = profile
        post = self.client.post(
            "/api/v1/yt/videos-uploaded/", payload, format="json"
        )
        self.assertEqual(post.status_code, 202)
        return post.data["id"]

    def test_repeated_post_keeps_running_jobs_staging_and_output(self):
        runner = self.GatedRunner(scripts=[{}, {}, {}, {}, {}, {}])
        set_test_runner(runner)
        first_id = self._post(OutputProfile.WHATSAPP_VIDEO)
        self.assertTrue(runner.entered.wait(5))

        staging = self._downloads / STAGING_DIR_NAME / first_id
        log = self._logs / f"{first_id}.txt"
        staged = [path for path in staging.iterdir()]
        self.assertTrue(staged)
        self.assertTrue(log.exists())

        second_id = self._post(OutputProfile.WHATSAPP_VIDEO)
        _wait_for_download(second_id)

        self.assertTrue(staging.is_dir())
        for path in staged:
            self.assertTrue(path.exists(), path)
        self.assertTrue(log.exists())

        runner.release.set()
        _wait_for_download(first_id)

        # Newest wins: the older job removes itself once it finishes.
        self.assertFalse(
            VideosUploaded.objects.filter(codecurl_id=first_id).exists()
        )
        self.assertFalse(staging.exists())
        self.assertFalse(log.exists())
        self.assertFalse((self._downloads / f"{first_id}.mp4").exists())
        second_upload = VideosUploaded.objects.get(codecurl_id=second_id)
        self.assertTrue(Path(second_upload.file_path).exists())
        self.assertTrue((self._downloads / f"{second_id}.mp4").exists())

    def test_finishing_job_keeps_artifacts_of_running_job(self):
        runner = self.GatedRunner(scripts=[{}, {}])
        set_test_runner(runner)
        first_id = self._post()
        self.assertTrue(runner.entered.wait(5))

        first_final = self._downloads / f"{first_id}.mp4"
        first_log = self._logs / f"{first_id}.txt"
        self.assertTrue(first_final.exists())
        self.assertTrue(first_log.exists())

        second_id = self._post()
        _wait_for_download(second_id)

        self.assertTrue(first_final.exists())
        self.assertTrue(first_log.exists())

        runner.release.set()
        _wait_for_download(first_id)

        # Newest wins: the older job removes itself once it finishes.
        self.assertFalse(first_final.exists())
        self.assertFalse(first_log.exists())
        self.assertFalse(
            VideosUploaded.objects.filter(codecurl_id=first_id).exists()
        )
        self.assertTrue((self._downloads / f"{second_id}.mp4").exists())
        self.assertTrue(
            VideosUploaded.objects.filter(codecurl_id=second_id).exists()
        )

    def test_older_job_finishing_after_newer_success_leaves_nothing(self):
        runner = self.GatedRunner(scripts=[{}, {}])
        set_test_runner(runner)
        first_id = self._post()
        self.assertTrue(runner.entered.wait(5))
        second_id = self._post()
        _wait_for_download(second_id)

        messages = []
        sink = loguru_logger.add(messages.append, level="INFO")
        self.addCleanup(loguru_logger.remove, sink)
        runner.release.set()
        _wait_for_download(first_id)

        completed = [
            json.loads(m.record["message"]) for m in messages
            if '"video_download_completed"' in m.record["message"]
        ]
        self.assertNotIn(
            first_id, [e["codecurl_id"] for e in completed]
        )
        self.assertFalse(
            VideosUploaded.objects.filter(codecurl_id=first_id).exists()
        )
        self.assertEqual(
            sorted(p.name for p in self._downloads.glob("*.mp4")),
            [f"{second_id}.mp4"],
        )
        self.assertEqual(VideosUploaded.objects.count(), 1)


class ConcurrentStartTests(DownloadJobTestCase):
    def test_concurrent_starts_run_exactly_one_job(self):
        release = threading.Event()

        class BlockingRunner(FakeCommandRunner):
            def run(self, *args, **kwargs):
                release.wait(5)
                return super().run(*args, **kwargs)

        runner = BlockingRunner(scripts=[{"progress": [b"[download] 100%"]}])
        set_test_runner(runner)
        codecurl = CodecUrls.objects.create(url="https://example.com/video")

        results = []
        barrier = threading.Barrier(8)

        def start():
            barrier.wait()
            results.append(start_download_async(codecurl))

        threads = [threading.Thread(target=start) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        release.set()
        _wait_for_download(str(codecurl.id))

        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(len(results), 8)
        self.assertTrue(all(r.id == codecurl.id for r in results))


class LifecycleLoggingTests(DirectDownloadJobTestCase):
    def test_events_are_json_with_request_id_and_truncated_url(self):
        messages = []
        sink = loguru_logger.add(messages.append, level="INFO")
        self.addCleanup(loguru_logger.remove, sink)
        url = "https://example.com/watch?v=" + "x" * 200

        codecurl = CodecUrls.objects.create(url=url)
        self.assertEqual(self.run_job(codecurl, FakeCommandRunner()), "succeeded")

        events = [json.loads(m.record["message"]) for m in messages]
        started = [e for e in events if e["event"] == "download_pipeline_started"]
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0]["request_id"], "request-49")
        self.assertLessEqual(len(started[0]["url_preview"]), 53)
        lifecycle = [e for e in events if "codecurl_id" in e or "url_preview" in e]
        self.assertNotIn(url, json.dumps(lifecycle))
        completed = [e for e in events if e["event"] == "video_download_completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["request_id"], "request-49")

    @override_settings(MEDIA_PROCESS_TIMEOUT=42)
    def test_single_timeout_setting_governs_download(self):
        runner = FakeCommandRunner()
        codecurl = CodecUrls.objects.create(url="https://example.com/v")
        self.assertEqual(self.run_job(codecurl, runner), "succeeded")
        self.assertEqual(runner.calls[0]["timeout"], 42)


class StaleJobTests(TestCase):
    def test_lists_pending_jobs_without_a_running_thread(self):
        stale = CodecUrls.objects.create(url="https://example.com/a")
        CodecUrls.objects.create(
            url="https://example.com/b", status=StatusCodec.SUCCESS
        )
        self.assertEqual(list(stale_pending_jobs()), [stale])


class CleanupDownloadsCommandTests(TestCase):
    """Orphan Video subido rows and their owned files are cleaned via CLI."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._downloads = Path(self._tmp.name) / "downloads"
        self._downloads.mkdir()
        self._logs = Path(self._tmp.name) / "logs"
        self._logs.mkdir()
        self._override = override_settings(
            DOWNLOADS_DIR=self._downloads,
            DOWNLOAD_LOGS_DIR=self._logs,
        )
        self._override.enable()
        self.addCleanup(self._override.disable)

    def _run(self, request_id="test-run"):
        out = StringIO()
        call_command("cleanup_downloads", request_id=request_id, stdout=out)
        return json.loads(out.getvalue())

    def test_cleanup_orphans_returns_typed_counts(self):
        counts = DownloadRetention(DownloadStorage()).cleanup_orphans()

        self.assertEqual(counts, OrphanCounts())

    def test_removes_orphan_row_and_file_but_keeps_owned_upload(self):
        orphan_file = self._downloads / "orphan.mp4"
        orphan_file.write_text("orphan")
        orphan = VideosUploaded.objects.create(
            file_path=str(orphan_file), codecurl=None
        )

        codecurl = CodecUrls.objects.create(
            url="https://example.com/kept", status=StatusCodec.SUCCESS
        )
        kept_file = self._downloads / f"{codecurl.id}.mp4"
        kept_file.write_text("kept")
        kept = VideosUploaded.objects.create(
            file_path=str(kept_file), codecurl=codecurl
        )

        messages = []
        sink = loguru_logger.add(messages.append, level="INFO")
        self.addCleanup(loguru_logger.remove, sink)

        counts = self._run()

        self.assertEqual(
            counts,
            {"files_deleted": 1, "uploads_deleted": 1},
        )
        self.assertFalse(VideosUploaded.objects.filter(pk=orphan.pk).exists())
        self.assertFalse(orphan_file.exists())
        self.assertTrue(VideosUploaded.objects.filter(pk=kept.pk).exists())
        self.assertTrue(kept_file.exists())
        self.assertTrue(self._downloads.is_dir())

        events = [
            json.loads(m.record["message"])
            for m in messages
            if "download_orphans_cleaned" in m.record["message"]
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["files_deleted"], 1)
        self.assertEqual(events[0]["uploads_deleted"], 1)
        self.assertEqual(events[0]["request_id"], "test-run")

    def test_second_run_removes_nothing(self):
        orphan_file = self._downloads / "orphan.mp4"
        orphan_file.write_text("orphan")
        VideosUploaded.objects.create(
            file_path=str(orphan_file), codecurl=None
        )

        first = self._run()
        second = self._run()

        self.assertEqual(first["uploads_deleted"], 1)
        self.assertEqual(second["files_deleted"], 0)
        self.assertEqual(second["uploads_deleted"], 0)
        self.assertEqual(VideosUploaded.objects.count(), 0)
        self.assertTrue(self._downloads.is_dir())

    def test_orphan_file_outside_downloads_root_is_kept(self):
        outside = Path(self._tmp.name) / "outside.mp4"
        outside.write_text("outside")
        orphan = VideosUploaded.objects.create(
            file_path=str(outside), codecurl=None
        )

        counts = self._run()

        self.assertEqual(counts["files_deleted"], 0)
        self.assertEqual(counts["uploads_deleted"], 1)
        self.assertFalse(VideosUploaded.objects.filter(pk=orphan.pk).exists())
        self.assertTrue(outside.exists())

    def test_missing_orphan_file_is_tolerated(self):
        orphan = VideosUploaded.objects.create(
            file_path=str(self._downloads / "gone.mp4"), codecurl=None
        )

        counts = self._run()

        self.assertEqual(counts["files_deleted"], 0)
        self.assertEqual(counts["uploads_deleted"], 1)
        self.assertFalse(VideosUploaded.objects.filter(pk=orphan.pk).exists())
