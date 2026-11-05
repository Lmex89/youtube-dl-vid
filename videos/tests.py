import json
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

from django.core.cache import caches
from django.test import TestCase, TransactionTestCase, override_settings
from rest_framework.test import APIClient

from videos.download import (
    _async_download_worker,
    cleanup_old_downloads,
    is_download_running,
    start_download_async,
)
from videos.models import CodecUrls, OutputProfile, StatusCodec, VideosUploaded
from videos.profiles import PROFILES, get_profile
from videos.runner import FakeCommandRunner, set_test_runner


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
            Path("/tmp/downloads/.tmp"),
        )
        output_template = command[command.index("-o") + 1]
        self.assertTrue(output_template.endswith("source.%(ext)s"))
        self.assertNotIn("/tmp/downloads/video.mp4", command)

    def test_audio_profile_extracts_m4a(self):
        command = get_profile(OutputProfile.AUDIO_M4A).build_command(
            "https://example.com/video",
            Path("/tmp/downloads/audio.m4a"),
            Path("/tmp/downloads/.tmp"),
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


class AsyncWorkerTests(TestCase):
    def _temp_settings(self, tmp):
        downloads = Path(tmp) / "downloads"
        downloads.mkdir()
        logs = Path(tmp) / "logs"
        return override_settings(
            DOWNLOADS_DIR=downloads,
            DOWNLOAD_LOGS_DIR=logs,
        )

    def test_standard_worker_marks_success_and_stores_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._temp_settings(tmp):
                codecurl = CodecUrls.objects.create(url="https://example.com")
                with patch("videos.download.run_download"), patch(
                    "videos.download.close_old_connections"
                ):
                    _async_download_worker(str(codecurl.id), "req-1")

                codecurl.refresh_from_db()
                self.assertEqual(codecurl.status, StatusCodec.SUCCESS)
                self.assertEqual(codecurl.progress, 100.0)

                upload = VideosUploaded.objects.get(codecurl=codecurl)
                self.assertTrue(upload.file_path.endswith(f"{codecurl.id}.mp4"))
                self.assertEqual(upload.mime_type, "video/mp4")

    def test_worker_marks_error_when_download_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._temp_settings(tmp):
                codecurl = CodecUrls.objects.create(url="https://example.com")
                with patch(
                    "videos.download.run_download",
                    side_effect=RuntimeError("boom"),
                ), patch("videos.download.close_old_connections"):
                    _async_download_worker(str(codecurl.id), "req-2")

                codecurl.refresh_from_db()
                self.assertEqual(codecurl.status, StatusCodec.ERROR)
                self.assertFalse(VideosUploaded.objects.exists())

    def test_whatsapp_worker_runs_transcode_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._temp_settings(tmp):
                downloads = Path(tmp) / "downloads"
                codecurl = CodecUrls.objects.create(
                    url="https://example.com",
                    output_profile=OutputProfile.WHATSAPP_VIDEO,
                )
                workdir = downloads / ".tmp" / str(codecurl.id)

                def fake_download(command, log_path, progress_callback=None, timeout=300, runner=None):
                    workdir.mkdir(parents=True, exist_ok=True)
                    (workdir / "source.mp4").write_bytes(b"source")
                    if progress_callback:
                        progress_callback(100.0)

                def fake_ffmpeg(command, log_path, duration=None, progress_callback=None, runner=None):
                    Path(command[-1]).write_bytes(b"encoded")
                    if progress_callback:
                        progress_callback(100.0)

                with patch(
                    "videos.download.run_download", side_effect=fake_download
                ), patch(
                    "videos.profiles.run_ffmpeg", side_effect=fake_ffmpeg
                ), patch(
                    "videos.profiles.probe_duration", return_value=None
                ), patch(
                    "videos.download.close_old_connections"
                ):
                    _async_download_worker(str(codecurl.id), "req-3")

                codecurl.refresh_from_db()
                self.assertEqual(codecurl.status, StatusCodec.SUCCESS)
                destination = downloads / f"{codecurl.id}.mp4"
                self.assertTrue(destination.exists())
                self.assertFalse(workdir.exists())

                upload = VideosUploaded.objects.get(codecurl=codecurl)
                self.assertEqual(upload.mime_type, "video/mp4")
                self.assertEqual(upload.file_path, str(destination))


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

    def test_download_uses_profile_mime_and_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            file_path = Path(tmp) / "audio.m4a"
            file_path.write_bytes(b"audio-bytes")
            codecurl = CodecUrls.objects.create(
                url="https://example.com/v",
                output_profile=OutputProfile.AUDIO_M4A,
            )
            upload = VideosUploaded.objects.create(
                file_path=str(file_path),
                mime_type="audio/mp4",
                codecurl=codecurl,
            )

            response = self.client.get(
                f"/api/v1/yt/videos-uploaded/{upload.id}"
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Content-Type"], "audio/mp4")
            self.assertIn(f"{upload.id}.m4a", response["Content-Disposition"])
            b"".join(response.streaming_content)

    def test_download_returns_404_when_file_missing(self):
        codecurl = CodecUrls.objects.create(url="https://example.com/v")
        upload = VideosUploaded.objects.create(
            file_path="/tmp/does-not-exist-anywhere.mp4",
            mime_type="video/mp4",
            codecurl=codecurl,
        )
        response = self.client.get(f"/api/v1/yt/videos-uploaded/{upload.id}")
        self.assertEqual(response.status_code, 404)

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


class CleanupTests(TestCase):
    def test_cleanup_is_scoped_by_output_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(DOWNLOADS_DIR=Path(tmp)):
                old_standard = CodecUrls.objects.create(
                    url="https://example.com/same",
                    output_profile=OutputProfile.STANDARD_VIDEO,
                )
                old_audio = CodecUrls.objects.create(
                    url="https://example.com/same",
                    output_profile=OutputProfile.AUDIO_M4A,
                )
                current = CodecUrls.objects.create(
                    url="https://example.com/same",
                    output_profile=OutputProfile.STANDARD_VIDEO,
                )
                standard_upload = VideosUploaded.objects.create(
                    file_path="",
                    codecurl=old_standard,
                )
                audio_upload = VideosUploaded.objects.create(
                    file_path="",
                    codecurl=old_audio,
                )

                cleanup_old_downloads(current)

                self.assertFalse(
                    VideosUploaded.objects.filter(pk=standard_upload.pk).exists()
                )
                self.assertTrue(
                    VideosUploaded.objects.filter(pk=audio_upload.pk).exists()
                )


def _wait_for_download(codecurl_id: str, timeout: float = 5.0) -> None:
    """Poll until the background download thread finishes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_download_running(codecurl_id):
            return
        time.sleep(0.02)
    raise TimeoutError(f"download {codecurl_id} did not finish in {timeout}s")


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


class StandardVideoEndToEndTests(DownloadJobTestCase):
    """End-to-end HTTP test: POST → background job → GET status.

    Uses FakeCommandRunner so no real yt-dlp / ffmpeg runs.
    """

    def test_standard_video_success_end_to_end(self):
        runner = FakeCommandRunner(
            scripts=[{"progress": [50.0, 100.0], "title": "Real Title"}]
        )
        set_test_runner(runner)

        post = self.client.post(
            "/api/v1/yt/videos-uploaded/",
            {"url": "https://example.com/video"},
            format="json",
        )
        self.assertEqual(post.status_code, 202)
        codecurl_id = post.data["id"]
        self.assertEqual(post.data["status"], "pending")
        self.assertEqual(post.data["output_profile"], "standard_video")

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

        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0]["label"], "yt_dlp")

    def test_standard_video_failure_end_to_end(self):
        runner = FakeCommandRunner(scripts=[{"fail": True, "error": "boom"}])
        set_test_runner(runner)

        post = self.client.post(
            "/api/v1/yt/videos-uploaded/",
            {"url": "https://example.com/video"},
            format="json",
        )
        self.assertEqual(post.status_code, 202)
        codecurl_id = post.data["id"]

        _wait_for_download(codecurl_id)

        get = self.client.get(f"/api/v1/yt/videos/{codecurl_id}")
        self.assertEqual(get.status_code, 200)
        self.assertEqual(get.data["status"], StatusCodec.ERROR)
        self.assertFalse(VideosUploaded.objects.filter(codecurl_id=codecurl_id).exists())


class RecordingRunner(FakeCommandRunner):
    """Records (label, progress seen over HTTP) after each progress emission."""

    def __init__(self, client, codecurl_ids, scripts=None):
        super().__init__(scripts)
        self.client = client
        self.ids = codecurl_ids  # filled once the POST returns
        self.seen = []

    def run(self, command, log_path, label, timeout, progress_callback=None, **kw):
        def spy(pct):
            progress_callback(pct)
            if self.ids:
                got = self.client.get(f"/api/v1/yt/videos/{self.ids[0]}")
                self.seen.append((label, got.data["progress"]))

        return super().run(command, log_path, label, timeout, spy, **kw)


@override_settings(WHATSAPP_MEDIA_MAX_BYTES=100)
class ReencodeProfileEndToEndTests(DownloadJobTestCase):
    def _post(self, profile):
        post = self.client.post(
            "/api/v1/yt/videos-uploaded/",
            {"url": "https://example.com/video", "output_profile": profile},
            format="json",
        )
        self.assertEqual(post.status_code, 202)
        return post.data["id"]

    def _run(self, profile, scripts):
        ids = []
        runner = RecordingRunner(self.client, ids, scripts)
        set_test_runner(runner)
        with patch("videos.profiles.probe_duration", return_value=10.0):
            ids.append(self._post(profile))
            _wait_for_download(ids[0])
        return ids[0], runner

    def _assert_monotonic_across_phases(self, runner):
        values = [v for _, v in runner.seen]
        self.assertEqual(values, sorted(values))
        download = [v for label, v in runner.seen if label == "yt_dlp"]
        process = [v for label, v in runner.seen if label != "yt_dlp"]
        self.assertTrue(download and process)
        self.assertGreater(max(process), max(download))

    def test_whatsapp_progress_spans_download_and_encode(self):
        cid, runner = self._run(
            "whatsapp_video",
            [
                {"progress": [20.0, 100.0]},
                {"progress": [30.0, 100.0], "output_content": b"x" * 50},
            ],
        )
        self._assert_monotonic_across_phases(runner)
        get = self.client.get(f"/api/v1/yt/videos/{cid}")
        self.assertEqual(get.data["status"], StatusCodec.SUCCESS)
        self.assertEqual(get.data["progress"], 100.0)
        upload = VideosUploaded.objects.get(codecurl_id=cid)
        self.assertTrue(upload.file_path.endswith(f"{cid}.mp4"))
        self.assertEqual(upload.mime_type, "video/mp4")

    def test_whatsapp_ladder_steps_down_until_fit(self):
        cid, runner = self._run(
            "whatsapp_video",
            [
                {"progress": [100.0]},
                {"progress": [100.0], "output_content": b"x" * 500},
                {"progress": [100.0], "output_content": b"x" * 500},
                {"progress": [100.0], "output_content": b"x" * 50},
            ],
        )
        ffmpeg = [c["command"] for c in runner.calls if c["label"] == "ffmpeg"]
        self.assertEqual(len(ffmpeg), 3)
        self.assertIn("scale=-2:'min(360,ih)'", ffmpeg[-1])
        get = self.client.get(f"/api/v1/yt/videos/{cid}")
        self.assertEqual(get.data["status"], StatusCodec.SUCCESS)
        self.assertEqual(
            Path(VideosUploaded.objects.get(codecurl_id=cid).file_path).stat().st_size,
            50,
        )

    def test_whatsapp_errors_when_no_ladder_step_fits(self):
        big = {"progress": [100.0], "output_content": b"x" * 500}
        cid, runner = self._run(
            "whatsapp_video", [{"progress": [100.0]}, big, big, big]
        )
        get = self.client.get(f"/api/v1/yt/videos/{cid}")
        self.assertEqual(get.data["status"], StatusCodec.ERROR)
        self.assertFalse(VideosUploaded.objects.filter(codecurl_id=cid).exists())

    def test_audio_progress_spans_download_and_post_process(self):
        cid, runner = self._run(
            "audio_m4a", [{"progress": [40.0, 100.0]}]
        )
        values = [v for _, v in runner.seen]
        self.assertEqual(values, sorted(values))
        self.assertLessEqual(max(values), 90.0)  # download is weighted 0.9
        get = self.client.get(f"/api/v1/yt/videos/{cid}")
        self.assertEqual(get.data["status"], StatusCodec.SUCCESS)
        self.assertEqual(get.data["progress"], 100.0)
        upload = VideosUploaded.objects.get(codecurl_id=cid)
        self.assertTrue(upload.file_path.endswith(f"{cid}.m4a"))
        self.assertEqual(upload.mime_type, "audio/mp4")


class ProgressSaverTests(TestCase):
    def _saver(self):
        from videos.download import ProgressSaver

        codecurl = CodecUrls.objects.create(url="https://example.com")
        return codecurl, ProgressSaver(str(codecurl.id))

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


class FailureCleanupEndToEndTests(DownloadJobTestCase):
    def test_download_failure_marks_error_and_removes_staging(self):
        runner = FakeCommandRunner(
            scripts=[{"fail": True, "error": "yt-dlp failed"}]
        )
        set_test_runner(runner)

        post = self.client.post(
            "/api/v1/yt/videos-uploaded/",
            {
                "url": "https://example.com/video",
                "output_profile": OutputProfile.WHATSAPP_VIDEO,
            },
            format="json",
        )
        self.assertEqual(post.status_code, 202)
        codecurl_id = post.data["id"]
        _wait_for_download(codecurl_id)

        get = self.client.get(f"/api/v1/yt/videos/{codecurl_id}")
        self.assertEqual(get.data["status"], StatusCodec.ERROR)
        self.assertFalse(VideosUploaded.objects.filter(codecurl_id=codecurl_id).exists())
        self.assertFalse(
            (self._downloads / ".tmp" / str(codecurl_id)).exists()
        )

    def test_success_removes_staging_over_http(self):
        sentinel = self._downloads / "keep.txt"
        sentinel.write_text("keep")
        runner = FakeCommandRunner(
            scripts=[{"progress": [100.0]}, {"progress": [100.0]}]
        )
        set_test_runner(runner)

        with patch("videos.profiles.probe_duration", return_value=None):
            post = self.client.post(
                "/api/v1/yt/videos-uploaded/",
                {
                    "url": "https://example.com/video",
                    "output_profile": OutputProfile.WHATSAPP_VIDEO,
                },
                format="json",
            )

        self.assertEqual(post.status_code, 202)
        codecurl_id = post.data["id"]
        _wait_for_download(codecurl_id)

        get = self.client.get(f"/api/v1/yt/videos/{codecurl_id}")
        self.assertEqual(get.data["status"], StatusCodec.SUCCESS)
        self.assertTrue(VideosUploaded.objects.filter(codecurl_id=codecurl_id).exists())
        self.assertFalse(
            (self._downloads / ".tmp" / str(codecurl_id)).exists()
        )
        self.assertTrue(sentinel.exists())

    def test_post_process_failure_marks_error_and_removes_staging(self):
        sentinel = self._downloads / "keep.txt"
        sentinel.write_text("keep")
        runner = FakeCommandRunner(
            scripts=[
                {"progress": [100.0]},
                {"fail": True, "error": "ffmpeg failed"},
            ]
        )
        set_test_runner(runner)

        with patch("videos.profiles.probe_duration", return_value=None):
            post = self.client.post(
                "/api/v1/yt/videos-uploaded/",
                {
                    "url": "https://example.com/video",
                    "output_profile": OutputProfile.WHATSAPP_VIDEO,
                },
                format="json",
            )

        self.assertEqual(post.status_code, 202)
        codecurl_id = post.data["id"]
        _wait_for_download(codecurl_id)

        get = self.client.get(f"/api/v1/yt/videos/{codecurl_id}")
        self.assertEqual(get.data["status"], StatusCodec.ERROR)
        self.assertFalse(VideosUploaded.objects.filter(codecurl_id=codecurl_id).exists())
        self.assertFalse(
            (self._downloads / ".tmp" / str(codecurl_id)).exists()
        )
        self.assertTrue(sentinel.exists())

    def test_timeout_marks_error_and_leaves_no_uploaded_video(self):
        runner = FakeCommandRunner(
            scripts=[{"timeout": True, "error": "yt-dlp timed out"}]
        )
        set_test_runner(runner)

        post = self.client.post(
            "/api/v1/yt/videos-uploaded/",
            {
                "url": "https://example.com/video",
                "output_profile": OutputProfile.WHATSAPP_VIDEO,
            },
            format="json",
        )
        self.assertEqual(post.status_code, 202)
        codecurl_id = post.data["id"]
        _wait_for_download(codecurl_id)

        get = self.client.get(f"/api/v1/yt/videos/{codecurl_id}")
        self.assertEqual(get.data["status"], StatusCodec.ERROR)
        self.assertFalse(VideosUploaded.objects.filter(codecurl_id=codecurl_id).exists())
        self.assertFalse(
            (self._downloads / ".tmp" / str(codecurl_id)).exists()
        )

    @patch("videos.download._async_download_worker")
    @patch("videos.download.logger.error")
    def test_missing_request_is_logged_and_staging_is_removed(
        self, mocked_error, mocked_worker
    ):
        def delete_request_and_run(codecurl_id, request_id):
            staging = self._downloads / ".tmp" / codecurl_id
            staging.mkdir(parents=True)
            (staging / "partial").write_text("partial")
            CodecUrls.objects.filter(pk=codecurl_id).delete()
            _async_download_worker(codecurl_id, request_id)

        mocked_worker.side_effect = delete_request_and_run

        post = self.client.post(
            "/api/v1/yt/videos-uploaded/",
            {"url": "https://example.com/video"},
            format="json",
        )

        self.assertEqual(post.status_code, 202)
        codecurl_id = post.data["id"]
        _wait_for_download(codecurl_id)
        self.assertFalse(CodecUrls.objects.filter(pk=codecurl_id).exists())
        self.assertFalse(
            (self._downloads / ".tmp" / str(codecurl_id)).exists()
        )
        self.assertTrue(self._downloads.exists())
        events = [
            json.loads(call.args[0])
            for call in mocked_error.call_args_list
            if call.args
        ]
        self.assertTrue(
            any(
                event.get("event") == "video_download_failed"
                and event.get("error_type") == "DoesNotExist"
                for event in events
            )
        )


class ConcurrentStartTests(DownloadJobTestCase):
    def test_concurrent_starts_run_exactly_one_job(self):
        release = threading.Event()

        class BlockingRunner(FakeCommandRunner):
            def run(self, *args, **kwargs):
                release.wait(5)
                return super().run(*args, **kwargs)

        runner = BlockingRunner(scripts=[{"progress": [100.0]}])
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
