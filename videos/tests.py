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
from videos.models import CodecUrls, OutputProfile, StatusCodec, VideosUploaded
from videos.profiles import PROFILES, get_profile
from videos.runner import FakeCommandRunner, set_test_runner
from videos.storage import (
    STAGING_DIR_NAME,
    DownloadRetention,
    DownloadStorage,
    OrphanCounts,
)


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


class SuccessOrderingTests(DownloadJobTestCase):
    """Estado exitoso is only ever written after VideosUploaded exists."""

    def _post(self):
        post = self.client.post(
            "/api/v1/yt/videos-uploaded/",
            {"url": "https://example.com/video"},
            format="json",
        )
        self.assertEqual(post.status_code, 202)
        return post.data["id"]

    def test_success_is_saved_only_when_upload_record_exists(self):
        set_test_runner(FakeCommandRunner(scripts=[{"progress": [100.0]}]))
        seen = []
        real_save = CodecUrls.save

        def spy(instance, *args, **kwargs):
            if instance.status == StatusCodec.SUCCESS:
                seen.append(
                    VideosUploaded.objects.filter(codecurl_id=instance.pk).exists()
                )
            return real_save(instance, *args, **kwargs)

        with patch.object(CodecUrls, "save", spy):
            codecurl_id = self._post()
            _wait_for_download(codecurl_id)

        self.assertEqual(seen, [True])

    def test_failed_upload_record_never_yields_success(self):
        set_test_runner(FakeCommandRunner(scripts=[{"progress": [100.0]}]))
        with patch.object(
            VideosUploaded.objects, "create", side_effect=RuntimeError("db down")
        ):
            codecurl_id = self._post()
            _wait_for_download(codecurl_id)

        self.assertEqual(CodecUrls.objects.get(pk=codecurl_id).status, StatusCodec.ERROR)
        self.assertFalse(VideosUploaded.objects.exists())


class StorageLayoutEndToEndTests(DownloadJobTestCase):
    """HTTP jobs keep final files and logs under the injected roots."""

    def test_standard_success_uses_storage_layout(self):
        runner = FakeCommandRunner(scripts=[{"progress": [100.0]}])
        set_test_runner(runner)

        post = self.client.post(
            "/api/v1/yt/videos-uploaded/",
            {"url": "https://example.com/video"},
            format="json",
        )
        codecurl_id = post.data["id"]
        _wait_for_download(codecurl_id)

        upload = VideosUploaded.objects.get(codecurl_id=codecurl_id)
        self.assertEqual(
            upload.file_path, str(self._downloads / f"{codecurl_id}.mp4")
        )
        self.assertTrue((self._downloads / f"{codecurl_id}.mp4").exists())
        self.assertTrue((self._logs / f"{codecurl_id}.txt").exists())
        self.assertFalse(
            (self._downloads / STAGING_DIR_NAME / codecurl_id).exists()
        )

        get = self.client.get(f"/api/v1/yt/videos-uploaded/{upload.id}")
        self.assertEqual(get.status_code, 200)
        b"".join(get.streaming_content)

    def test_whatsapp_success_uses_storage_layout_for_process_log(self):
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
        codecurl_id = post.data["id"]
        _wait_for_download(codecurl_id)

        upload = VideosUploaded.objects.get(codecurl_id=codecurl_id)
        self.assertEqual(
            upload.file_path, str(self._downloads / f"{codecurl_id}.mp4")
        )
        self.assertTrue((self._logs / f"{codecurl_id}.txt").exists())
        self.assertTrue((self._logs / f"{codecurl_id}.ffmpeg.txt").exists())
        self.assertFalse(
            (self._downloads / STAGING_DIR_NAME / codecurl_id).exists()
        )


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
            (self._downloads / STAGING_DIR_NAME / str(codecurl_id)).exists()
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
            (self._downloads / STAGING_DIR_NAME / str(codecurl_id)).exists()
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
            (self._downloads / STAGING_DIR_NAME / str(codecurl_id)).exists()
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
            (self._downloads / STAGING_DIR_NAME / str(codecurl_id)).exists()
        )

    def test_missing_request_is_logged_and_staging_is_removed(self):
        downloads = self._downloads

        class DeletingRunner(FakeCommandRunner):
            def run(self, command, log_path, label, timeout, *a, **kw):
                if "-o" in command:
                    cid = Path(command[command.index("-o") + 1]).parent.name
                    (downloads / STAGING_DIR_NAME / cid).mkdir(
                        parents=True, exist_ok=True
                    )
                    (downloads / STAGING_DIR_NAME / cid / "partial").write_text(
                        "partial"
                    )
                    CodecUrls.objects.filter(pk=cid).delete()
                return super().run(command, log_path, label, timeout, *a, **kw)

        set_test_runner(DeletingRunner())
        messages = []
        sink = loguru_logger.add(messages.append, level="ERROR")
        self.addCleanup(loguru_logger.remove, sink)

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
        self.assertFalse(CodecUrls.objects.filter(pk=codecurl_id).exists())
        self.assertFalse(
            (self._downloads / STAGING_DIR_NAME / str(codecurl_id)).exists()
        )
        self.assertTrue(self._downloads.exists())
        events = [json.loads(m.record["message"]) for m in messages]
        self.assertTrue(
            any(
                event.get("event") == "video_download_failed"
                and event.get("error_type") == "DoesNotExist"
                for event in events
            )
        )


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

    def test_second_success_removes_first_file_staging_logs_and_upload(self):
        set_test_runner(FakeCommandRunner(scripts=[{}, {}, {}, {}]))
        with patch("videos.profiles.probe_duration", return_value=None):
            first_id = self._post(
                "https://example.com/same", OutputProfile.WHATSAPP_VIDEO
            )
            _wait_for_download(first_id)
            first = self._artifact_paths(first_id)
            self.assertTrue(first["final"].exists())
            self.assertTrue(first["log"].exists())
            self.assertTrue(first["process_log"].exists())

            first["staging"].mkdir(parents=True)
            (first["staging"] / "source.mp4").write_text("partial")
            first["title_log"].write_text("First title")
            first_upload = VideosUploaded.objects.get(codecurl_id=first_id)

            messages = []
            sink = loguru_logger.add(messages.append, level="INFO")
            self.addCleanup(loguru_logger.remove, sink)

            second_id = self._post(
                "https://example.com/same", OutputProfile.WHATSAPP_VIDEO
            )
            _wait_for_download(second_id)

        self.assertFalse(
            VideosUploaded.objects.filter(pk=first_upload.pk).exists()
        )
        self.assertFalse(CodecUrls.objects.filter(pk=first_id).exists())
        for path in first.values():
            self.assertFalse(path.exists(), path)

        second = self._artifact_paths(second_id)
        self.assertTrue(second["final"].exists())
        self.assertTrue(second["log"].exists())
        self.assertTrue(second["process_log"].exists())
        self.assertTrue(
            VideosUploaded.objects.filter(codecurl_id=second_id).exists()
        )

        events = [json.loads(m.record["message"]) for m in messages]
        superseded = [
            e for e in events if e.get("event") == "download_superseded"
        ]
        self.assertEqual(len(superseded), 1)
        self.assertEqual(superseded[0]["superseded_requests"], 1)
        self.assertEqual(superseded[0]["files_deleted"], 1)
        self.assertEqual(superseded[0]["staging_dirs_deleted"], 1)
        self.assertEqual(superseded[0]["logs_deleted"], 3)
        self.assertEqual(superseded[0]["uploads_deleted"], 1)
        self.assertEqual(superseded[0]["codecurls_deleted"], 1)

    def test_superseded_event_has_request_id_and_truncated_url(self):
        url = "https://example.com/watch?v=" + "x" * 200
        set_test_runner(FakeCommandRunner(scripts=[{}, {}]))
        messages = []
        sink = loguru_logger.add(messages.append, level="INFO")
        self.addCleanup(loguru_logger.remove, sink)

        first_id = self._post(url)
        _wait_for_download(first_id)
        second_id = self._post(url)
        _wait_for_download(second_id)

        events = [json.loads(m.record["message"]) for m in messages]
        superseded = [
            e for e in events if e.get("event") == "download_superseded"
        ]
        self.assertEqual(len(superseded), 1)
        self.assertTrue(superseded[0]["request_id"])
        self.assertNotEqual(superseded[0]["request_id"], "unknown")
        self.assertLessEqual(len(superseded[0]["url_preview"]), 53)
        self.assertNotEqual(superseded[0]["url_preview"], url)

    def test_failed_attempt_row_stays_as_history(self):
        set_test_runner(
            FakeCommandRunner(scripts=[{"fail": True, "error": "boom"}])
        )
        failed_id = self._post("https://example.com/same")
        _wait_for_download(failed_id)
        self.assertEqual(
            CodecUrls.objects.get(pk=failed_id).status, StatusCodec.ERROR
        )

        messages = []
        sink = loguru_logger.add(messages.append, level="INFO")
        self.addCleanup(loguru_logger.remove, sink)

        set_test_runner(FakeCommandRunner(scripts=[{"progress": [100.0]}]))
        success_id = self._post("https://example.com/same")
        _wait_for_download(success_id)

        failed = CodecUrls.objects.get(pk=failed_id)
        self.assertEqual(failed.status, StatusCodec.ERROR)
        self.assertEqual(
            CodecUrls.objects.get(pk=success_id).status, StatusCodec.SUCCESS
        )

        events = [json.loads(m.record["message"]) for m in messages]
        superseded = [
            e for e in events if e.get("event") == "download_superseded"
        ]
        self.assertEqual(len(superseded), 1)
        self.assertEqual(superseded[0]["superseded_requests"], 1)
        self.assertEqual(superseded[0]["codecurls_deleted"], 0)

    def test_failed_second_download_leaves_first_servable(self):
        set_test_runner(FakeCommandRunner(scripts=[{"progress": [100.0]}]))
        first_id = self._post("https://example.com/same")
        _wait_for_download(first_id)
        first = self._artifact_paths(first_id)
        first_upload = VideosUploaded.objects.get(codecurl_id=first_id)

        set_test_runner(
            FakeCommandRunner(scripts=[{"fail": True, "error": "boom"}])
        )
        second_id = self._post("https://example.com/same")
        _wait_for_download(second_id)

        self.assertEqual(
            CodecUrls.objects.get(pk=second_id).status, StatusCodec.ERROR
        )
        self.assertFalse(
            VideosUploaded.objects.filter(codecurl_id=second_id).exists()
        )
        self.assertTrue(
            VideosUploaded.objects.filter(pk=first_upload.pk).exists()
        )
        self.assertTrue(first["final"].exists())
        self.assertTrue(first["log"].exists())

        get = self.client.get(
            f"/api/v1/yt/videos-uploaded/{first_upload.id}"
        )
        self.assertEqual(get.status_code, 200)
        self.assertEqual(b"".join(get.streaming_content), b"fake")

    def test_other_profile_and_url_are_untouched(self):
        set_test_runner(FakeCommandRunner())
        first_id = self._post("https://example.com/same")
        _wait_for_download(first_id)
        first = self._artifact_paths(first_id)
        first_upload = VideosUploaded.objects.get(codecurl_id=first_id)

        with patch("videos.profiles.probe_duration", return_value=None):
            other_profile_id = self._post(
                "https://example.com/same", OutputProfile.WHATSAPP_VIDEO
            )
            _wait_for_download(other_profile_id)

        other_url_id = self._post("https://example.com/other")
        _wait_for_download(other_url_id)

        self.assertTrue(
            VideosUploaded.objects.filter(pk=first_upload.pk).exists()
        )
        self.assertTrue(first["final"].exists())
        self.assertTrue(first["log"].exists())
        self.assertTrue(
            VideosUploaded.objects.filter(
                codecurl_id=other_profile_id
            ).exists()
        )
        self.assertTrue(
            VideosUploaded.objects.filter(codecurl_id=other_url_id).exists()
        )

    def test_post_does_not_delete_previous_file_while_job_is_pending(self):
        set_test_runner(FakeCommandRunner(scripts=[{"progress": [100.0]}]))
        first_id = self._post("https://example.com/same")
        _wait_for_download(first_id)
        first = self._artifact_paths(first_id)
        first_upload = VideosUploaded.objects.get(codecurl_id=first_id)

        class BlockingRunner(FakeCommandRunner):
            def __init__(self):
                super().__init__(scripts=[{"progress": [100.0]}])
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

    def test_later_requests_are_not_superseded(self):
        url = "https://example.com/same"
        later = CodecUrls.objects.create(url=url)
        later_file = self._downloads / f"{later.id}.mp4"
        later_file.write_text("later")
        later_log = self._logs / f"{later.id}.txt"
        later_log.write_text("later log")
        later_upload = VideosUploaded.objects.create(
            file_path=str(later_file), codecurl=later
        )
        CodecUrls.objects.filter(pk=later.pk).update(
            created_at=timezone.now() + timedelta(hours=1)
        )

        set_test_runner(FakeCommandRunner(scripts=[{"progress": [100.0]}]))
        current_id = self._post(url)
        _wait_for_download(current_id)

        self.assertTrue(
            VideosUploaded.objects.filter(pk=later_upload.pk).exists()
        )
        self.assertTrue(later_file.exists())
        self.assertTrue(later_log.exists())
        self.assertTrue(CodecUrls.objects.filter(pk=later.pk).exists())
        self.assertTrue(
            VideosUploaded.objects.filter(codecurl_id=current_id).exists()
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
        runner = self.GatedRunner(scripts=[{}, {}, {}, {}])
        set_test_runner(runner)
        with patch("videos.profiles.probe_duration", return_value=None):
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


class LifecycleLoggingTests(DownloadJobTestCase):
    def test_events_are_json_with_request_id_and_truncated_url(self):
        set_test_runner(FakeCommandRunner())
        messages = []
        sink = loguru_logger.add(messages.append, level="INFO")
        self.addCleanup(loguru_logger.remove, sink)
        url = "https://example.com/watch?v=" + "x" * 200

        post = self.client.post(
            "/api/v1/yt/videos-uploaded/", {"url": url}, format="json"
        )
        _wait_for_download(post.data["id"])

        events = [json.loads(m.record["message"]) for m in messages]
        started = [e for e in events if e["event"] == "download_pipeline_started"]
        self.assertEqual(len(started), 1)
        self.assertIn("request_id", started[0])
        self.assertLessEqual(len(started[0]["url_preview"]), 53)
        lifecycle = [e for e in events if "codecurl_id" in e or "url_preview" in e]
        self.assertNotIn(url, json.dumps(lifecycle))

    @override_settings(MEDIA_PROCESS_TIMEOUT=42)
    def test_single_timeout_setting_governs_download(self):
        runner = FakeCommandRunner()
        set_test_runner(runner)
        post = self.client.post(
            "/api/v1/yt/videos-uploaded/",
            {"url": "https://example.com/v"},
            format="json",
        )
        _wait_for_download(post.data["id"])
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
