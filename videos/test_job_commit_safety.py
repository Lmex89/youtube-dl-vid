import tempfile
import threading
import time
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

from django.db import OperationalError, connection, connections, transaction
from django.test import TransactionTestCase

from videos.job_run import run_job
from videos.models import CodecUrls, StatusCodec, VideosUploaded
from videos.storage import DownloadRetention, DownloadStorage
from videos.test_utils import (
    FakeCommandRunner, capture_job_run_logs, fail_success_write,
    first_commit_fails,
)


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

        side_effect, _ = first_commit_fails(
            connection, "commit acknowledgement lost", apply_first=True
        )
        with capture_job_run_logs() as messages:
            with patch.object(connection, "commit", side_effect=side_effect):
                outcome = self.run_job(codecurl)

        codecurl.refresh_from_db()
        upload = VideosUploaded.objects.get(codecurl=codecurl)
        self.assertEqual(outcome, "succeeded")
        self.assertEqual(codecurl.status, StatusCodec.SUCCESS)
        self.assertEqual(codecurl.progress, 100.0)
        self.assertTrue(Path(upload.file_path).is_file())
        self.assertFalse(CodecUrls.objects.filter(pk=previous.pk).exists())
        self.assertFalse(previous_file.exists())
        self.assertEqual(messages, [])

    def test_failed_write_rolls_back_and_removes_only_its_file(self):
        previous = CodecUrls.objects.create(url="https://example.com/video")
        self.assertEqual(self.run_job(previous), "succeeded")
        previous_file = Path(VideosUploaded.objects.get(codecurl=previous).file_path)
        codecurl = CodecUrls.objects.create(url=previous.url)
        final = self.storage.downloads_dir / f"{codecurl.id}.mp4"

        with connection.execute_wrapper(fail_success_write()):
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

        with capture_job_run_logs() as messages:
            with connection.execute_wrapper(reject_queries):
                with patch.object(connection, "commit", side_effect=commit_then_lose_connection):
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
        self.assertEqual(messages, [
            {
                "event": "download_success_unconfirmed",
                "codecurl_id": str(codecurl.id),
                "error_type": "OperationalError",
                "request_id": "request-safety",
            },
            {
                "event": "video_download_failed",
                "codecurl_id": str(codecurl.id),
                "error": "commit acknowledgement lost",
                "error_type": "OperationalError",
                "request_id": "request-safety",
            },
        ])

    def test_unconfirmed_commit_without_success_keeps_pending_and_file(self):
        codecurl = CodecUrls.objects.create(url="https://example.com/video")
        final = self.storage.downloads_dir / f"{codecurl.id}.mp4"

        with capture_job_run_logs():
            with patch.object(
                connection, "commit", side_effect=OperationalError("connection lost")
            ):
                outcome = self.run_job(codecurl)

        codecurl.refresh_from_db()
        self.assertEqual(outcome, "failed")
        self.assertEqual(codecurl.status, StatusCodec.PENDING)
        self.assertFalse(VideosUploaded.objects.filter(codecurl=codecurl).exists())
        self.assertTrue(final.is_file())

    def test_rolled_back_commit_is_verified_and_cleans_only_its_file(self):
        previous = CodecUrls.objects.create(url="https://example.com/video")
        self.assertEqual(self.run_job(previous), "succeeded")
        previous_file = Path(VideosUploaded.objects.get(codecurl=previous).file_path)
        codecurl = CodecUrls.objects.create(url=previous.url)
        final = self.storage.downloads_dir / f"{codecurl.id}.mp4"

        side_effect, _ = first_commit_fails(connection)
        with capture_job_run_logs() as messages:
            with patch.object(connection, "commit", side_effect=side_effect):
                outcome = self.run_job(codecurl)

        codecurl.refresh_from_db()
        self.assertEqual(outcome, "failed")
        self.assertEqual(codecurl.status, StatusCodec.ERROR)
        self.assertFalse(VideosUploaded.objects.filter(codecurl=codecurl).exists())
        self.assertFalse(final.exists())
        self.assertTrue(previous_file.is_file())
        self.assertTrue(VideosUploaded.objects.filter(codecurl=previous).exists())
        self.assertEqual(messages, [{
            "event": "video_download_failed",
            "codecurl_id": str(codecurl.id),
            "error": "commit not applied",
            "error_type": "OperationalError",
            "request_id": "request-safety",
        }])

    def test_lost_reconciliation_commit_acknowledgement_keeps_file(self):
        codecurl = CodecUrls.objects.create(url="https://example.com/video")
        final = self.storage.downloads_dir / f"{codecurl.id}.mp4"
        commit = connection.commit
        calls = []

        def lose_acknowledgements():
            calls.append(True)
            if len(calls) == 1:
                raise OperationalError("original commit not applied")
            commit()
            raise OperationalError("reconciliation acknowledgement lost")

        with capture_job_run_logs() as messages:
            with patch.object(connection, "commit", side_effect=lose_acknowledgements):
                outcome = self.run_job(codecurl)

        codecurl.refresh_from_db()
        self.assertEqual(len(calls), 2)
        self.assertEqual(outcome, "failed")
        self.assertEqual(codecurl.status, StatusCodec.ERROR)
        self.assertFalse(VideosUploaded.objects.filter(codecurl=codecurl).exists())
        self.assertTrue(final.is_file())
        self.assertEqual(messages, [
            {
                "event": "download_success_unconfirmed",
                "codecurl_id": str(codecurl.id),
                "error_type": "OperationalError",
                "request_id": "request-safety",
            },
            {
                "event": "video_download_failed",
                "codecurl_id": str(codecurl.id),
                "error": "original commit not applied",
                "error_type": "OperationalError",
                "request_id": "request-safety",
            },
        ])

    def test_pending_request_with_upload_remains_unconfirmed(self):
        codecurl = CodecUrls.objects.create(url="https://example.com/video")
        upload = VideosUploaded.objects.create(codecurl=codecurl, title="Existing")
        final = self.storage.downloads_dir / f"{codecurl.id}.mp4"

        side_effect, _ = first_commit_fails(connection)
        with capture_job_run_logs():
            with patch.object(connection, "commit", side_effect=side_effect):
                outcome = self.run_job(codecurl)

        codecurl.refresh_from_db()
        self.assertEqual(outcome, "failed")
        self.assertEqual(codecurl.status, StatusCodec.PENDING)
        self.assertTrue(VideosUploaded.objects.filter(pk=upload.pk).exists())
        self.assertTrue(final.is_file())

    def test_reconciliation_waits_for_in_flight_success(self):
        codecurl = CodecUrls.objects.create(url="https://example.com/video")
        final = self.storage.downloads_dir / f"{codecurl.id}.mp4"
        locked, release, checking, finished = (threading.Event() for _ in range(4))
        errors, outcomes = [], []

        def finish_success():
            try:
                with transaction.atomic():
                    request = CodecUrls.objects.select_for_update().get(pk=codecurl.pk)
                    request.status = StatusCodec.SUCCESS
                    request.progress = 100.0
                    request.save(update_fields=["status", "progress", "updated_at"])
                    VideosUploaded.objects.create(codecurl=request, file_path=str(final), title="Video")
                    locked.set()
                    if not release.wait(5):
                        raise TimeoutError("success transaction was not released")
            except Exception as exc:
                errors.append(exc)
                locked.set()
            finally:
                connections.close_all()

        holder = threading.Thread(target=finish_success)

        def run_with_uncertain_commit():
            rollback = connection.rollback

            def rollback_then_hold_row():
                rollback()
                holder.start()
                if not locked.wait(5):
                    raise TimeoutError("success transaction did not acquire row lock")

            def observe_lock(execute, sql, params, many, context):
                if "FOR UPDATE" in sql:
                    checking.set()
                return execute(sql, params, many, context)

            try:
                side_effect, _ = first_commit_fails(
                    connection, "commit acknowledgement lost"
                )
                with connection.execute_wrapper(observe_lock):
                    with patch.object(connection, "commit", side_effect=side_effect):
                        with patch.object(connection, "rollback", side_effect=rollback_then_hold_row):
                            outcomes.append(self.run_job(codecurl))
            except Exception as exc:
                errors.append(exc)
            finally:
                connections.close_all()
                finished.set()

        job = threading.Thread(target=run_with_uncertain_commit)
        job.start()
        try:
            self.assertTrue(locked.wait(5))
            self.assertTrue(checking.wait(5))
            self.assertFalse(finished.wait(0.2))
        finally:
            release.set()
            job.join(10)
            if holder.ident is not None:
                holder.join(10)

        self.assertFalse(job.is_alive())
        self.assertFalse(holder.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(outcomes, ["succeeded"])
        codecurl.refresh_from_db()
        self.assertEqual(codecurl.status, StatusCodec.SUCCESS)
        self.assertTrue(VideosUploaded.objects.filter(codecurl=codecurl, file_path=str(final)).exists())
        self.assertTrue(final.is_file())

    @skipUnless(connection.vendor == "postgresql", "requires PostgreSQL row locks")
    def test_reconciliation_times_out_without_overwriting_in_flight_success(self):
        codecurl = CodecUrls.objects.create(url="https://example.com/video")
        final = self.storage.downloads_dir / f"{codecurl.id}.mp4"
        locked, release = threading.Event(), threading.Event()
        errors = []

        def finish_success():
            try:
                with transaction.atomic():
                    request = CodecUrls.objects.select_for_update().get(pk=codecurl.pk)
                    request.status = StatusCodec.SUCCESS
                    request.progress = 100.0
                    request.save(update_fields=["status", "progress", "updated_at"])
                    VideosUploaded.objects.create(codecurl=request, file_path=str(final), title="Video")
                    locked.set()
                    if not release.wait(8):
                        raise TimeoutError("success transaction was not released")
            except Exception as exc:
                errors.append(exc)
                locked.set()
            finally:
                connections.close_all()

        holder = threading.Thread(target=finish_success)
        rollback = connection.rollback

        def rollback_then_hold_row():
            rollback()
            if holder.ident is None:
                holder.start()
                if not locked.wait(5) or errors:
                    raise RuntimeError("success transaction did not acquire row lock")

        lock_errors = []

        def capture_lock_error(execute, sql, params, many, context):
            try:
                return execute(sql, params, many, context)
            except OperationalError as exc:
                if "FOR UPDATE" in sql:
                    lock_errors.append(exc)
                raise

        with connection.cursor() as cursor:
            cursor.execute("SHOW lock_timeout")
            original_timeout = cursor.fetchone()[0]
        try:
            side_effect, _ = first_commit_fails(
                connection, "commit acknowledgement lost"
            )
            with capture_job_run_logs() as messages:
                with connection.execute_wrapper(capture_lock_error):
                    with patch.object(connection, "commit", side_effect=side_effect):
                        with patch.object(connection, "rollback", side_effect=rollback_then_hold_row):
                            started = time.monotonic()
                            outcome = self.run_job(codecurl)
                            elapsed = time.monotonic() - started
            locked_on_return = holder.is_alive()
            file_on_return = final.is_file()
        finally:
            release.set()
            if holder.ident is not None:
                holder.join(10)

        self.assertFalse(holder.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(lock_errors), 1)
        self.assertEqual(lock_errors[0].__cause__.sqlstate, "55P03")
        self.assertTrue(locked_on_return)
        self.assertLess(elapsed, 5)
        self.assertEqual(outcome, "failed")
        self.assertTrue(file_on_return)
        codecurl.refresh_from_db()
        self.assertEqual(codecurl.status, StatusCodec.SUCCESS)
        self.assertEqual(codecurl.progress, 100.0)
        self.assertTrue(VideosUploaded.objects.filter(codecurl=codecurl, file_path=str(final)).exists())
        self.assertTrue(final.is_file())
        self.assertEqual(messages, [
            {
                "event": "download_success_unconfirmed",
                "codecurl_id": str(codecurl.id),
                "error_type": "OperationalError",
                "request_id": "request-safety",
            },
            {
                "event": "video_download_failed",
                "codecurl_id": str(codecurl.id),
                "error": "commit acknowledgement lost",
                "error_type": "OperationalError",
                "request_id": "request-safety",
            },
        ])
        with connection.cursor() as cursor:
            cursor.execute("SHOW lock_timeout")
            self.assertEqual(cursor.fetchone()[0], original_timeout)

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
