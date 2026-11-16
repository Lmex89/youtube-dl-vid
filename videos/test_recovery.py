import tempfile
from io import StringIO
from pathlib import Path

from django.core.cache import caches
from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from videos.models import CodecUrls, OutputProfile, StatusCodec, VideosUploaded


class FailInterruptedJobsTests(TestCase):
    def setUp(self):
        caches["ratelimit"].clear()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.downloads = Path(tmp.name) / "downloads"
        self.downloads.mkdir()
        override = override_settings(DOWNLOADS_DIR=self.downloads)
        override.enable()
        self.addCleanup(override.disable)

    def detail(self, codecurl):
        return APIClient().get(f"/api/v1/yt/videos/{codecurl.id}").json()

    def test_pending_jobs_become_failed_with_reason_and_only_their_leftovers_are_removed(self):
        job = CodecUrls.objects.create(url="https://youtu.be/a")
        partial = self.downloads / f"{job.id}.mp4"
        partial.write_text("partial")
        staging = self.downloads / ".tmp" / str(job.id)
        staging.mkdir(parents=True)
        (staging / "x").write_text("x")
        other = self.downloads / "unrelated.mp4"
        other.write_text("keep")
        done = CodecUrls.objects.create(url="https://youtu.be/b", status=StatusCodec.SUCCESS)
        kept = self.downloads / f"{done.id}.mp4"
        kept.write_text("valid")
        VideosUploaded.objects.create(file_path=str(kept), title="t", codecurl=done)

        call_command("fail_interrupted_jobs", stdout=StringIO())

        body = self.detail(job)
        self.assertEqual(body["status"], StatusCodec.ERROR)
        self.assertIn("reinicio", body["error_reason"])
        self.assertFalse(partial.exists() or staging.exists())
        self.assertTrue(other.exists() and kept.exists())
        self.assertEqual(self.detail(done)["status"], StatusCodec.SUCCESS)

    def test_pending_job_with_recorded_upload_keeps_its_file(self):
        job = CodecUrls.objects.create(url="https://youtu.be/a")
        f = self.downloads / f"{job.id}.mp4"
        f.write_text("valid")
        VideosUploaded.objects.create(file_path=str(f), title="t", codecurl=job)

        call_command("fail_interrupted_jobs", stdout=StringIO())

        self.assertTrue(f.exists())
