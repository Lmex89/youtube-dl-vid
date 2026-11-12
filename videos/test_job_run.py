import json
import tempfile
from pathlib import Path

from django.test import TestCase
from loguru import logger

from videos.job_run import run_job
from videos.models import CodecUrls, StatusCodec, VideosUploaded
from videos.storage import DownloadRetention, DownloadStorage
from videos.test_utils import FakeCommandRunner


class RunJobTests(TestCase):
    def test_tool_output_is_not_replayed_into_application_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = DownloadStorage(Path(tmp) / "downloads", Path(tmp) / "logs")
            codecurl = CodecUrls.objects.create(url="https://example.com/video")
            messages = []
            sink = logger.add(messages.append, level="DEBUG", filter=lambda record: record["name"] == "videos.job_run")
            try:
                outcome = run_job(
                    str(codecurl.id), "request-44",
                    runner=FakeCommandRunner(scripts=[{"log": "token=secret"}]),
                    storage=storage, retention=DownloadRetention(storage),
                )
            finally:
                logger.remove(sink)

            self.assertEqual(outcome, "succeeded")
            self.assertEqual((storage.logs_dir / f"{codecurl.id}.txt").read_text(), "token=secret")
            events = [json.loads(message.record["message"]) for message in messages]
            self.assertEqual([event["event"] for event in events], [
                "download_pipeline_started", "video_download_completed",
            ])
            self.assertTrue(all(event["request_id"] == "request-44" for event in events))

    def test_standard_video_succeeds_synchronously(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = DownloadStorage(Path(tmp) / "downloads", Path(tmp) / "logs")
            runner = FakeCommandRunner(scripts=[{"title": "Real Title"}])
            codecurl = CodecUrls.objects.create(url="https://example.com/video")

            outcome = run_job(
                str(codecurl.id), "request-44", runner=runner, storage=storage,
                retention=DownloadRetention(storage),
            )

            codecurl.refresh_from_db()
            self.assertEqual(outcome, "succeeded")
            self.assertEqual(codecurl.status, StatusCodec.SUCCESS)
            self.assertEqual(codecurl.progress, 100.0)
            upload = VideosUploaded.objects.get(codecurl=codecurl)
            self.assertEqual(upload.title, "Real Title")
            self.assertEqual(upload.mime_type, "video/mp4")
            self.assertEqual(
                Path(upload.file_path), storage.downloads_dir / f"{codecurl.id}.mp4"
            )
            self.assertTrue(Path(upload.file_path).is_file())

    def test_older_job_returns_discarded_without_completion_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = DownloadStorage(Path(tmp) / "downloads", Path(tmp) / "logs")
            old = CodecUrls.objects.create(url="https://example.com/video")
            CodecUrls.objects.create(url=old.url, status=StatusCodec.SUCCESS)
            messages = []
            sink = logger.add(messages.append, level="INFO")
            try:
                outcome = run_job(
                    str(old.id), "request-44", runner=FakeCommandRunner(),
                    storage=storage, retention=DownloadRetention(storage),
                )
            finally:
                logger.remove(sink)

            self.assertEqual(outcome, "discarded")
            self.assertFalse(CodecUrls.objects.filter(pk=old.pk).exists())
            self.assertFalse(VideosUploaded.objects.filter(codecurl_id=old.pk).exists())
            self.assertFalse((storage.downloads_dir / f"{old.id}.mp4").exists())
            events = [json.loads(message.record["message"]) for message in messages]
            self.assertNotIn("video_download_completed", [event["event"] for event in events])
            self.assertEqual(events[0]["request_id"], "request-44")

    def test_supersession_failure_keeps_success_and_logs_request_id(self):
        class FailingRetention(DownloadRetention):
            def discard_if_superseded(self, codecurl, request_id="unknown"):
                raise RuntimeError("retention failed")

        with tempfile.TemporaryDirectory() as tmp:
            storage = DownloadStorage(Path(tmp) / "downloads", Path(tmp) / "logs")
            codecurl = CodecUrls.objects.create(url="https://example.com/video")
            messages = []
            sink = logger.add(messages.append, level="INFO")
            try:
                outcome = run_job(
                    str(codecurl.id), "request-44", runner=FakeCommandRunner(),
                    storage=storage, retention=FailingRetention(storage),
                )
            finally:
                logger.remove(sink)

            codecurl.refresh_from_db()
            self.assertEqual(outcome, "succeeded")
            self.assertEqual(codecurl.status, StatusCodec.SUCCESS)
            self.assertTrue(VideosUploaded.objects.filter(codecurl=codecurl).exists())
            events = [json.loads(message.record["message"]) for message in messages]
            failure = next(event for event in events if event["event"] == "download_supersede_failed")
            self.assertEqual(failure["request_id"], "request-44")
            self.assertEqual(failure["error"], "retention failed")
            self.assertEqual(failure["error_type"], "RuntimeError")
