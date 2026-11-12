import json
import tempfile
from pathlib import Path

from django.test import TestCase
from loguru import logger

from videos.job_run import run_job
from videos.models import CodecUrls, OutputProfile, StatusCodec, VideosUploaded
from videos.storage import DownloadRetention, DownloadStorage
from videos.test_utils import FakeCommandRunner


class JobFailureAndTitleTests(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.storage = DownloadStorage(Path(tmp.name) / "downloads", Path(tmp.name) / "logs")
        self.storage.downloads_dir.mkdir()
        self.storage.logs_dir.mkdir()

    def run_job(self, codecurl, runner):
        return run_job(
            str(codecurl.id), "request-45", runner=runner, storage=self.storage,
            retention=DownloadRetention(self.storage),
        )

    def test_failed_download_removes_partial_staging_and_keeps_progress(self):
        codecurl = CodecUrls.objects.create(
            url="https://example.com/video", output_profile=OutputProfile.WHATSAPP_VIDEO
        )
        staging = self.storage.staging_root / str(codecurl.id)

        class PartialFailureRunner(FakeCommandRunner):
            def run(self, command, log_path, label, timeout, *args, **kwargs):
                (staging / "partial").write_text("partial")
                return super().run(command, log_path, label, timeout, *args, **kwargs)

        runner = PartialFailureRunner(scripts=[{
            "progress": [b"[download] 40%"], "fail": True, "error": "yt-dlp failed"
        }])
        outcome = self.run_job(codecurl, runner)

        codecurl.refresh_from_db()
        self.assertEqual(outcome, "failed")
        self.assertEqual(codecurl.status, StatusCodec.ERROR)
        self.assertEqual(codecurl.progress, 30.0)
        self.assertFalse(VideosUploaded.objects.filter(codecurl=codecurl).exists())
        self.assertFalse(staging.exists())

    def test_timed_out_download_removes_partial_staging(self):
        codecurl = CodecUrls.objects.create(
            url="https://example.com/video", output_profile=OutputProfile.WHATSAPP_VIDEO
        )
        staging = self.storage.staging_root / str(codecurl.id)

        class PartialTimeoutRunner(FakeCommandRunner):
            def run(self, command, log_path, label, timeout, *args, **kwargs):
                (staging / "partial").write_text("partial")
                return super().run(command, log_path, label, timeout, *args, **kwargs)

        outcome = self.run_job(codecurl, PartialTimeoutRunner(scripts=[{
            "progress": [b"[download] 40%"], "timeout": True
        }]))

        codecurl.refresh_from_db()
        self.assertEqual(outcome, "failed")
        self.assertEqual(codecurl.status, StatusCodec.ERROR)
        self.assertEqual(codecurl.progress, 30.0)
        self.assertFalse(VideosUploaded.objects.filter(codecurl=codecurl).exists())
        self.assertFalse(staging.exists())

    def test_standard_download_failure_marks_error(self):
        codecurl = CodecUrls.objects.create(url="https://example.com/video")

        outcome = self.run_job(codecurl, FakeCommandRunner(scripts=[{"fail": True}]))

        codecurl.refresh_from_db()
        self.assertEqual(outcome, "failed")
        self.assertEqual(codecurl.status, StatusCodec.ERROR)
        self.assertFalse(VideosUploaded.objects.filter(codecurl=codecurl).exists())

    def test_post_process_failure_removes_staging_but_not_other_files(self):
        codecurl = CodecUrls.objects.create(
            url="https://example.com/video", output_profile=OutputProfile.WHATSAPP_VIDEO
        )
        sentinel = self.storage.downloads_dir / "keep.txt"
        sentinel.write_text("keep")
        runner = FakeCommandRunner(scripts=[
            {"progress": [b"[download] 100%"]}, {},
            {"fail": True, "error": "ffmpeg failed"},
        ])

        outcome = self.run_job(codecurl, runner)

        codecurl.refresh_from_db()
        self.assertEqual(outcome, "failed")
        self.assertEqual(codecurl.status, StatusCodec.ERROR)
        self.assertFalse(VideosUploaded.objects.filter(codecurl=codecurl).exists())
        self.assertFalse((self.storage.staging_root / str(codecurl.id)).exists())
        self.assertEqual(sentinel.read_text(), "keep")

    def test_success_removes_staging_but_not_other_files(self):
        codecurl = CodecUrls.objects.create(
            url="https://example.com/video", output_profile=OutputProfile.WHATSAPP_VIDEO
        )
        sentinel = self.storage.downloads_dir / "keep.txt"
        sentinel.write_text("keep")

        outcome = self.run_job(codecurl, FakeCommandRunner(scripts=[{}, {}, {}]))

        codecurl.refresh_from_db()
        self.assertEqual(outcome, "succeeded")
        self.assertEqual(codecurl.status, StatusCodec.SUCCESS)
        self.assertTrue(VideosUploaded.objects.filter(codecurl=codecurl).exists())
        self.assertFalse((self.storage.staging_root / str(codecurl.id)).exists())
        self.assertEqual(sentinel.read_text(), "keep")

    def test_request_deleted_during_download_is_logged_and_staging_removed(self):
        codecurl = CodecUrls.objects.create(
            url="https://example.com/video", output_profile=OutputProfile.WHATSAPP_VIDEO
        )
        staging = self.storage.staging_root / str(codecurl.id)

        class DeletingRunner(FakeCommandRunner):
            def run(self, command, log_path, label, timeout, *args, **kwargs):
                (staging / "partial").write_text("partial")
                CodecUrls.objects.filter(pk=codecurl.pk).delete()
                return super().run(command, log_path, label, timeout, *args, **kwargs)

        messages = []
        sink = logger.add(messages.append, level="ERROR")
        self.addCleanup(logger.remove, sink)
        outcome = self.run_job(codecurl, DeletingRunner(scripts=[{}, {}, {}]))

        self.assertEqual(outcome, "failed")
        self.assertFalse(CodecUrls.objects.filter(pk=codecurl.pk).exists())
        self.assertFalse(VideosUploaded.objects.filter(codecurl_id=codecurl.pk).exists())
        self.assertFalse(staging.exists())
        self.assertTrue(self.storage.downloads_dir.exists())
        events = [json.loads(message.record["message"]) for message in messages]
        self.assertTrue(any(
            event.get("event") == "video_download_failed"
            and event.get("error_type") == "DoesNotExist"
            and event.get("request_id") == "request-45"
            for event in events
        ))

    def test_title_from_tool_and_filename_fallback(self):
        for script, expected_title in [
            ({"title": "  Media title  \n"}, "Media title"),
            ({"title": "  \n"}, None),
            ({}, None),
        ]:
            with self.subTest(script=script):
                codecurl = CodecUrls.objects.create(url="https://example.com/video")
                title_path = self.storage.logs_dir / f"{codecurl.id}.title.txt"
                title_path.write_text("stale title")

                outcome = self.run_job(codecurl, FakeCommandRunner(scripts=[script]))

                self.assertEqual(outcome, "succeeded")
                upload = VideosUploaded.objects.get(codecurl=codecurl)
                self.assertEqual(upload.title, expected_title or str(codecurl.id))
                self.assertFalse(title_path.exists())

    def test_standard_video_never_creates_staging_during_download(self):
        codecurl = CodecUrls.objects.create(url="https://example.com/video")
        staging = self.storage.staging_root
        observed = []

        class ObservingRunner(FakeCommandRunner):
            def run(self, command, log_path, label, timeout, *args, **kwargs):
                observed.append(staging.exists())
                result = super().run(command, log_path, label, timeout, *args, **kwargs)
                observed.append(staging.exists())
                return result

        outcome = self.run_job(codecurl, ObservingRunner())

        self.assertEqual(outcome, "succeeded")
        self.assertEqual(observed, [False, False])
        self.assertFalse(staging.exists())
