import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase
from loguru import logger

from videos.job_run import run_job
from videos.models import CodecUrls, OutputProfile, StatusCodec, VideosUploaded
from videos.profiles import get_profile
from videos.storage import DownloadRetention, DownloadStorage
from videos.test_utils import FakeCommandRunner


class JobSupersessionTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.storage = DownloadStorage(
            Path(temporary.name) / "downloads", Path(temporary.name) / "logs"
        )
        self.retention = DownloadRetention(self.storage)

    def run_request(self, request, runner=None, retention=None):
        return run_job(
            str(request.id), "request-46", runner=runner or FakeCommandRunner(),
            storage=self.storage, retention=retention or self.retention,
        )

    def paths(self, request):
        return self.storage.job_paths(str(request.id), get_profile(request.output_profile))

    def test_success_replaces_earlier_success_and_owned_artifacts(self):
        url = "https://example.com/watch?v=" + "x" * 200
        first = CodecUrls.objects.create(url=url, output_profile=OutputProfile.WHATSAPP_VIDEO)
        self.assertEqual(self.run_request(first), "succeeded")
        old = self.paths(first)
        upload = VideosUploaded.objects.get(codecurl=first)
        self.assertTrue(old.final.is_file())
        self.assertTrue(old.log.is_file())
        self.assertTrue(old.process_log.is_file())
        old.workdir.mkdir(parents=True)
        (old.workdir / "partial").write_text("partial")
        old.title_log.write_text("title")

        second = CodecUrls.objects.create(url=url, output_profile=OutputProfile.WHATSAPP_VIDEO)
        with self.assertLogs("videos", level="INFO") as logs:
            self.assertEqual(self.run_request(second), "succeeded")

        self.assertFalse(CodecUrls.objects.filter(pk=first.pk).exists())
        self.assertFalse(VideosUploaded.objects.filter(pk=upload.pk).exists())
        for path in (old.final, old.workdir, old.log, old.process_log, old.title_log):
            self.assertFalse(path.exists(), path)
        second.refresh_from_db()
        self.assertEqual(second.status, StatusCodec.SUCCESS)
        self.assertEqual(second.progress, 100)
        self.assertTrue(VideosUploaded.objects.filter(codecurl=second).exists())
        self.assertTrue(self.paths(second).final.is_file())
        self.assertTrue(self.paths(second).log.is_file())
        self.assertTrue(self.paths(second).process_log.is_file())

        events = [json.loads(record.getMessage()) for record in logs.records]
        event = next(e for e in events if e["event"] == "download_superseded")
        self.assertEqual(event["request_id"], "request-46")
        self.assertEqual(event["url_preview"], url[:50] + "...")
        self.assertEqual(
            [event[key] for key in (
                "superseded_requests", "files_deleted", "staging_dirs_deleted",
                "logs_deleted", "uploads_deleted", "codecurls_deleted",
            )],
            [1, 1, 1, 3, 1, 1],
        )

    def test_staging_cleanup_refusal_during_supersession_keeps_new_artifact(self):
        first = CodecUrls.objects.create(
            url="https://example.com/same", output_profile=OutputProfile.WHATSAPP_VIDEO
        )
        self.assertEqual(self.run_request(first), "succeeded")
        old = self.paths(first)
        old.workdir.mkdir(parents=True)
        second = CodecUrls.objects.create(url=first.url, output_profile=first.output_profile)

        real_rmtree = shutil.rmtree

        def refuse_old_staging(path, *args, **kwargs):
            if path == old.workdir:
                raise PermissionError("secret")
            return real_rmtree(path, *args, **kwargs)

        with patch("videos.storage.shutil.rmtree", side_effect=refuse_old_staging):
            with self.assertLogs("videos", level="INFO") as logs:
                self.assertEqual(self.run_request(second), "succeeded")

        self.assertFalse(CodecUrls.objects.filter(pk=first.pk).exists())
        self.assertTrue(old.workdir.exists())
        second.refresh_from_db()
        self.assertEqual(second.status, StatusCodec.SUCCESS)
        self.assertTrue(Path(VideosUploaded.objects.get(codecurl=second).file_path).is_file())
        events = [json.loads(record.getMessage()) for record in logs.records]
        failure = next(event for event in events if event["event"] == "download_staging_cleanup_failed")
        self.assertEqual(failure["codecurl_id"], str(first.id))
        self.assertEqual(failure["request_id"], "request-46")
        self.assertNotIn("secret", str(events))
        superseded = next(event for event in events if event["event"] == "download_superseded")
        self.assertEqual(superseded["staging_dirs_deleted"], 0)

    def test_failed_attempt_is_kept_as_history_after_success(self):
        failed = CodecUrls.objects.create(url="https://example.com/same")
        self.assertEqual(
            self.run_request(failed, FakeCommandRunner(scripts=[{"fail": True}])),
            "failed",
        )
        success = CodecUrls.objects.create(url=failed.url)
        with self.assertLogs("videos", level="INFO") as logs:
            self.assertEqual(self.run_request(success), "succeeded")

        failed.refresh_from_db()
        success.refresh_from_db()
        self.assertEqual(failed.status, StatusCodec.ERROR)
        self.assertEqual(success.status, StatusCodec.SUCCESS)
        self.assertFalse(VideosUploaded.objects.filter(codecurl=failed).exists())
        event = next(json.loads(r.getMessage()) for r in logs.records
                     if json.loads(r.getMessage())["event"] == "download_superseded")
        self.assertEqual(event["superseded_requests"], 1)
        self.assertEqual(event["codecurls_deleted"], 0)

    def test_failed_later_attempt_leaves_previous_artifact(self):
        first = CodecUrls.objects.create(url="https://example.com/same")
        self.assertEqual(self.run_request(first), "succeeded")
        upload = VideosUploaded.objects.get(codecurl=first)
        second = CodecUrls.objects.create(url=first.url)
        self.assertEqual(
            self.run_request(second, FakeCommandRunner(scripts=[{"fail": True}])),
            "failed",
        )

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, StatusCodec.SUCCESS)
        self.assertEqual(second.status, StatusCodec.ERROR)
        self.assertTrue(VideosUploaded.objects.filter(pk=upload.pk).exists())
        self.assertFalse(VideosUploaded.objects.filter(codecurl=second).exists())
        self.assertEqual(Path(upload.file_path).read_bytes(), b"fake")
        self.assertTrue(self.paths(first).log.is_file())

    def test_other_urls_and_profiles_are_not_superseded(self):
        url = "https://example.com/same"
        other_profile = CodecUrls.objects.create(url=url, output_profile=OutputProfile.WHATSAPP_VIDEO)
        other_url = CodecUrls.objects.create(url="https://example.com/other")
        self.assertEqual(self.run_request(other_profile), "succeeded")
        self.assertEqual(self.run_request(other_url), "succeeded")
        uploads = [VideosUploaded.objects.get(codecurl=r) for r in (other_profile, other_url)]

        current = CodecUrls.objects.create(url=url)
        self.assertEqual(self.run_request(current), "succeeded")

        for request, upload in zip((other_profile, other_url), uploads):
            request.refresh_from_db()
            self.assertEqual(request.status, StatusCodec.SUCCESS)
            self.assertTrue(VideosUploaded.objects.filter(pk=upload.pk).exists())
            self.assertTrue(Path(upload.file_path).is_file())
            self.assertTrue(self.paths(request).log.is_file())
        self.assertTrue(VideosUploaded.objects.filter(codecurl=current).exists())

    def test_later_pending_request_is_not_superseded(self):
        current = CodecUrls.objects.create(url="https://example.com/same")
        later = CodecUrls.objects.create(url=current.url)
        later_paths = self.paths(later)
        later_paths.final.parent.mkdir(parents=True, exist_ok=True)
        later_paths.log.parent.mkdir(parents=True, exist_ok=True)
        later_paths.final.write_text("later")
        later_paths.log.write_text("later log")
        upload = VideosUploaded.objects.create(file_path=str(later_paths.final), codecurl=later)

        self.assertEqual(self.run_request(current), "succeeded")

        later.refresh_from_db()
        self.assertEqual(later.status, StatusCodec.PENDING)
        self.assertTrue(VideosUploaded.objects.filter(pk=upload.pk).exists())
        self.assertEqual(later_paths.final.read_text(), "later")
        self.assertEqual(later_paths.log.read_text(), "later log")
        self.assertTrue(VideosUploaded.objects.filter(codecurl=current).exists())

    def test_equal_timestamps_do_not_supersede_either_success(self):
        current = CodecUrls.objects.create(url="https://example.com/same")
        equal = CodecUrls.objects.create(url=current.url, status=StatusCodec.SUCCESS)
        CodecUrls.objects.filter(pk=equal.pk).update(created_at=current.created_at)

        self.assertEqual(self.run_request(current), "succeeded")

        current.refresh_from_db()
        equal.refresh_from_db()
        self.assertEqual(current.status, StatusCodec.SUCCESS)
        self.assertEqual(equal.status, StatusCodec.SUCCESS)
        self.assertTrue(VideosUploaded.objects.filter(codecurl=current).exists())
        self.assertTrue(self.paths(current).final.is_file())

    def test_newer_failure_and_unrelated_successes_do_not_discard_job(self):
        current = CodecUrls.objects.create(url="https://example.com/same")
        failed = CodecUrls.objects.create(url=current.url, status=StatusCodec.ERROR)
        other_url = CodecUrls.objects.create(
            url="https://example.com/other", status=StatusCodec.SUCCESS
        )
        other_profile = CodecUrls.objects.create(
            url=current.url, output_profile=OutputProfile.AUDIO_M4A, status=StatusCodec.SUCCESS
        )

        self.assertEqual(self.run_request(current), "succeeded")

        for request in (current, failed, other_url, other_profile):
            self.assertTrue(CodecUrls.objects.filter(pk=request.pk).exists())
        self.assertTrue(VideosUploaded.objects.filter(codecurl=current).exists())
        self.assertTrue(self.paths(current).final.is_file())

    def test_out_of_order_job_is_discarded_without_completion_event(self):
        older = CodecUrls.objects.create(
            url="https://example.com/same", output_profile=OutputProfile.WHATSAPP_VIDEO
        )
        newer = CodecUrls.objects.create(url=older.url, output_profile=older.output_profile)
        self.assertEqual(self.run_request(newer), "succeeded")
        messages = []
        sink = logger.add(messages.append, level="INFO")
        self.addCleanup(logger.remove, sink)
        with self.assertLogs("videos", level="INFO") as logs:
            self.assertEqual(self.run_request(older), "discarded")

        self.assertFalse(CodecUrls.objects.filter(pk=older.pk).exists())
        self.assertFalse(VideosUploaded.objects.filter(codecurl_id=older.pk).exists())
        old = self.paths(older)
        for path in (old.final, old.workdir, old.log, old.process_log, old.title_log):
            self.assertFalse(path.exists(), path)
        newer.refresh_from_db()
        self.assertEqual(newer.status, StatusCodec.SUCCESS)
        self.assertTrue(VideosUploaded.objects.filter(codecurl=newer).exists())
        self.assertTrue(self.paths(newer).final.is_file())
        events = [json.loads(message.record["message"]) for message in messages]
        self.assertNotIn("video_download_completed", [event["event"] for event in events])
        event = next(json.loads(r.getMessage()) for r in logs.records
                     if json.loads(r.getMessage())["event"] == "download_discarded_as_superseded")
        self.assertEqual(event["request_id"], "request-46")
        self.assertEqual(event["codecurl_id"], str(older.id))

    def test_supersession_exceptions_preserve_success_and_are_logged(self):
        class FailingDiscard(DownloadRetention):
            def discard_if_superseded(self, codecurl, request_id="unknown"):
                raise RuntimeError("discard failed")

        class FailingSupersede(DownloadRetention):
            def supersede_earlier(self, codecurl, request_id="unknown"):
                raise RuntimeError("supersede failed")

        for retention_class, error in ((FailingDiscard, "discard failed"),
                                       (FailingSupersede, "supersede failed")):
            with self.subTest(error=error):
                request = CodecUrls.objects.create(url=f"https://example.com/{error}")
                messages = []
                sink = logger.add(messages.append, level="INFO")
                try:
                    outcome = self.run_request(request, retention=retention_class(self.storage))
                finally:
                    logger.remove(sink)

                self.assertEqual(outcome, "succeeded")
                request.refresh_from_db()
                self.assertEqual(request.status, StatusCodec.SUCCESS)
                self.assertEqual(request.progress, 100)
                upload = VideosUploaded.objects.get(codecurl=request)
                self.assertTrue(Path(upload.file_path).is_file())
                events = [json.loads(message.record["message"]) for message in messages]
                failure = next(e for e in events if e["event"] == "download_supersede_failed")
                self.assertEqual(failure["request_id"], "request-46")
                self.assertEqual(failure["codecurl_id"], str(request.id))
                self.assertEqual(failure["error"], error)
                self.assertEqual(failure["error_type"], "RuntimeError")
                self.assertIn("video_download_completed", [e["event"] for e in events])
