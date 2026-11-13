import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.db import OperationalError, connection
from django.test import TransactionTestCase

from videos.job_run import run_job
from videos.models import CodecUrls, StatusCodec, VideosUploaded
from videos.storage import DownloadRetention, DownloadStorage
from videos.test_utils import FakeCommandRunner


class JobCommitSafetyTests(TransactionTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.storage = DownloadStorage(Path(tmp.name) / "downloads", Path(tmp.name) / "logs")

    def run_job(self, codecurl):
        return run_job(
            str(codecurl.id), "request-safety", runner=FakeCommandRunner(),
            storage=self.storage, retention=DownloadRetention(self.storage),
        )

    def test_lost_commit_acknowledgement_preserves_success_and_supersedes(self):
        previous = CodecUrls.objects.create(url="https://example.com/video")
        self.assertEqual(self.run_job(previous), "succeeded")
        previous_file = Path(VideosUploaded.objects.get(codecurl=previous).file_path)
        codecurl = CodecUrls.objects.create(url=previous.url)
        commit = connection.commit
        calls = []

        def commit_then_fail():
            commit()
            calls.append(True)
            if len(calls) == 1:
                raise OperationalError("commit acknowledgement lost")

        with patch.object(connection, "commit", side_effect=commit_then_fail):
            outcome = self.run_job(codecurl)

        codecurl.refresh_from_db()
        upload = VideosUploaded.objects.get(codecurl=codecurl)
        self.assertEqual(outcome, "succeeded")
        self.assertEqual(codecurl.status, StatusCodec.SUCCESS)
        self.assertEqual(codecurl.progress, 100.0)
        self.assertTrue(Path(upload.file_path).is_file())
        self.assertFalse(CodecUrls.objects.filter(pk=previous.pk).exists())
        self.assertFalse(previous_file.exists())

    def test_failed_write_rolls_back_and_removes_only_its_file(self):
        previous = CodecUrls.objects.create(url="https://example.com/video")
        self.assertEqual(self.run_job(previous), "succeeded")
        previous_file = Path(VideosUploaded.objects.get(codecurl=previous).file_path)
        codecurl = CodecUrls.objects.create(url=previous.url)
        final = self.storage.downloads_dir / f"{codecurl.id}.mp4"

        def fail_write(execute, sql, params, many, context):
            if (sql.startswith('UPDATE "videos_codecurls"')
                    and '"status"' in sql and '"progress"' in sql):
                return execute("SELECT * FROM nonexistent_success_write_table", None, many, context)
            return execute(sql, params, many, context)

        with connection.execute_wrapper(fail_write):
            outcome = self.run_job(codecurl)

        codecurl.refresh_from_db()
        self.assertEqual(outcome, "failed")
        self.assertEqual(codecurl.status, StatusCodec.ERROR)
        self.assertFalse(VideosUploaded.objects.filter(codecurl=codecurl).exists())
        self.assertFalse(final.exists())
        self.assertTrue(previous_file.is_file())
        self.assertTrue(VideosUploaded.objects.filter(codecurl=previous).exists())

    def test_unavailable_database_after_commit_preserves_success_and_file(self):
        previous = CodecUrls.objects.create(url="https://example.com/video")
        self.assertEqual(self.run_job(previous), "succeeded")
        previous_file = Path(VideosUploaded.objects.get(codecurl=previous).file_path)
        codecurl = CodecUrls.objects.create(url=previous.url)
        final = self.storage.downloads_dir / f"{codecurl.id}.mp4"
        commit = connection.commit
        unavailable = []

        def commit_then_lose_connection():
            commit()
            unavailable.append(True)
            raise OperationalError("commit acknowledgement lost")

        def reject_queries(execute, sql, params, many, context):
            if unavailable:
                raise OperationalError("database unavailable")
            return execute(sql, params, many, context)

        with connection.execute_wrapper(reject_queries):
            with patch.object(connection, "commit", side_effect=commit_then_lose_connection):
                with self.assertLogs("videos", level="ERROR") as logs:
                    outcome = self.run_job(codecurl)

        codecurl.refresh_from_db()
        upload = VideosUploaded.objects.get(codecurl=codecurl)
        self.assertEqual(outcome, "failed")
        self.assertEqual(codecurl.status, StatusCodec.SUCCESS)
        self.assertEqual(codecurl.progress, 100.0)
        self.assertEqual(Path(upload.file_path), final)
        self.assertTrue(final.is_file())
        self.assertTrue(previous_file.is_file())
        self.assertTrue(VideosUploaded.objects.filter(codecurl=previous).exists())
        self.assertEqual([json.loads(r.getMessage()) for r in logs.records], [{
            "event": "download_success_unconfirmed",
            "codecurl_id": str(codecurl.id),
            "error_type": "OperationalError",
            "request_id": "request-safety",
        }])

    def test_unconfirmed_commit_without_success_keeps_pending_and_file(self):
        codecurl = CodecUrls.objects.create(url="https://example.com/video")
        final = self.storage.downloads_dir / f"{codecurl.id}.mp4"

        with patch.object(connection, "commit", side_effect=OperationalError("connection lost")):
            with self.assertLogs("videos", level="ERROR"):
                outcome = self.run_job(codecurl)

        codecurl.refresh_from_db()
        self.assertEqual(outcome, "failed")
        self.assertEqual(codecurl.status, StatusCodec.PENDING)
        self.assertFalse(VideosUploaded.objects.filter(codecurl=codecurl).exists())
        self.assertTrue(final.is_file())

    def test_outer_failure_does_not_overwrite_existing_success(self):
        codecurl = CodecUrls.objects.create(url="https://example.com/video")
        final = self.storage.downloads_dir / f"{codecurl.id}.mp4"

        class ConcurrentSuccessRunner(FakeCommandRunner):
            def run(self, *args, **kwargs):
                super().run(*args, **kwargs)
                VideosUploaded.objects.create(codecurl=codecurl, file_path=str(final), title="Video")
                CodecUrls.objects.filter(pk=codecurl.pk).update(status=StatusCodec.SUCCESS, progress=100)
                raise RuntimeError("download failed after concurrent completion")

        outcome = run_job(
            str(codecurl.id), "request-safety", runner=ConcurrentSuccessRunner(),
            storage=self.storage, retention=DownloadRetention(self.storage),
        )

        codecurl.refresh_from_db()
        self.assertEqual(outcome, "failed")
        self.assertEqual(codecurl.status, StatusCodec.SUCCESS)
        self.assertEqual(codecurl.progress, 100.0)
        self.assertTrue(VideosUploaded.objects.filter(codecurl=codecurl).exists())
        self.assertTrue(final.is_file())
