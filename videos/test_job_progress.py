import tempfile
from pathlib import Path

from django.db import connection
from django.test import TestCase, override_settings

from videos.job_run import run_job
from videos.models import CodecUrls, OutputProfile, StatusCodec, VideosUploaded
from videos.storage import DownloadRetention, DownloadStorage
from videos.test_utils import FakeCommandRunner


@override_settings(WHATSAPP_MEDIA_MAX_BYTES=100)
class JobProgressTests(TestCase):
    def run_with_progress(self, profile, scripts):
        codecurl = CodecUrls.objects.create(
            url="https://example.com/video", output_profile=profile
        )
        saved = []
        self.saved_states = []

        def capture(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if sql.startswith('UPDATE "videos_codecurls"') and '"progress"' in sql:
                progress, status = CodecUrls.objects.values_list(
                    "progress", "status"
                ).get(pk=codecurl.pk)
                saved.append(progress)
                self.saved_states.append(
                    (status, VideosUploaded.objects.filter(codecurl=codecurl).exists())
                )
            return result

        with tempfile.TemporaryDirectory() as tmp:
            storage = DownloadStorage(Path(tmp) / "downloads", Path(tmp) / "logs")
            with connection.execute_wrapper(capture):
                outcome = run_job(
                    str(codecurl.id), "request-48", runner=FakeCommandRunner(scripts),
                    storage=storage, retention=DownloadRetention(storage),
                )
        codecurl.refresh_from_db()
        return outcome, codecurl, saved

    def test_whatsapp_completes_post_process_without_progress_callbacks(self):
        outcome, _, saved = self.run_with_progress(
            OutputProfile.WHATSAPP_VIDEO,
            [{"progress": [b"[download] 100%"]}, {"log": "10.0"}, {}],
        )
        self.assertEqual(outcome, "succeeded")
        self.assertEqual(saved, [75.0, 100.0, 100.0])

    def test_standard_download_saves_throttled_progress_and_success(self):
        outcome, codecurl, saved = self.run_with_progress(
            OutputProfile.STANDARD_VIDEO,
            [{"progress": [b"[download] 20%", b"[download] 20.5%",
                           b"[download] 19%", b"[download] 80%"]}],
        )
        self.assertEqual(outcome, "succeeded")
        self.assertEqual(saved, [20.0, 80.0, 100.0])
        self.assertEqual(self.saved_states, [
            (StatusCodec.PENDING, False), (StatusCodec.PENDING, False),
            (StatusCodec.SUCCESS, True),
        ])
        self.assertEqual(codecurl.status, StatusCodec.SUCCESS)
        self.assertTrue(VideosUploaded.objects.filter(codecurl=codecurl).exists())

    def test_standard_keeps_download_100_and_success_writes(self):
        outcome, _, saved = self.run_with_progress(
            OutputProfile.STANDARD_VIDEO,
            [{"progress": [b"[download] 100%"]}],
        )
        self.assertEqual(outcome, "succeeded")
        self.assertEqual(saved, [100.0, 100.0])
        self.assertEqual(self.saved_states, [
            (StatusCodec.PENDING, False), (StatusCodec.SUCCESS, True),
        ])

    def test_audio_download_and_post_process_progress(self):
        outcome, codecurl, saved = self.run_with_progress(
            OutputProfile.AUDIO_M4A,
            [{"progress": [b"[download] 40%", b"[download] 100%"]}],
        )
        self.assertEqual(outcome, "succeeded")
        self.assertEqual(saved, [36.0, 90.0, 100.0, 100.0])
        upload = VideosUploaded.objects.get(codecurl=codecurl)
        self.assertTrue(upload.file_path.endswith(f"{codecurl.id}.m4a"))
        self.assertEqual(upload.mime_type, "audio/mp4")

    def test_whatsapp_download_and_encode_progress(self):
        outcome, codecurl, saved = self.run_with_progress(
            OutputProfile.WHATSAPP_VIDEO,
            [{"progress": [b"[download] 20%", b"[download] 100%"]},
             {"log": "10.0"},
             {"progress": [b"out_time_us=3000000", b"out_time_us=10000000"]}],
        )
        self.assertEqual(outcome, "succeeded")
        self.assertEqual(saved, [15.0, 75.0, 82.5, 100.0, 100.0])
        upload = VideosUploaded.objects.get(codecurl=codecurl)
        self.assertTrue(upload.file_path.endswith(f"{codecurl.id}.mp4"))
        self.assertEqual(upload.mime_type, "video/mp4")

    def test_whatsapp_ladder_retry_never_rewinds_progress(self):
        outcome, _, saved = self.run_with_progress(
            OutputProfile.WHATSAPP_VIDEO,
            [{"progress": [b"[download] 100%"]}, {"log": "10.0"},
             {"progress": [b"out_time_us=5000000"], "output_content": b"x" * 500},
             {"progress": [b"out_time_us=1000000", b"out_time_us=7000000",
                           b"out_time_us=10000000"]}],
        )
        self.assertEqual(outcome, "succeeded")
        self.assertEqual(saved, [75.0, 87.5, 92.5, 100.0, 100.0])

    def test_failed_post_process_preserves_last_saved_progress(self):
        outcome, codecurl, saved = self.run_with_progress(
            OutputProfile.WHATSAPP_VIDEO,
            [{"progress": [b"[download] 40%", b"[download] 100%"]},
             {"log": "10.0"},
             {"progress": [b"out_time_us=4000000", b"out_time_us=4100000"],
              "fail": True}],
        )
        self.assertEqual(outcome, "failed")
        self.assertEqual(saved, [30.0, 75.0, 85.0])
        self.assertEqual(codecurl.status, StatusCodec.ERROR)
        self.assertEqual(codecurl.progress, 85.0)
        self.assertFalse(VideosUploaded.objects.filter(codecurl=codecurl).exists())

    def test_failed_ladder_preserves_100_from_an_earlier_attempt(self):
        oversized = {
            "progress": [b"out_time_us=10000000"], "output_content": b"x" * 500
        }
        outcome, codecurl, saved = self.run_with_progress(
            OutputProfile.WHATSAPP_VIDEO,
            [{"progress": [b"[download] 100%"]}, {"log": "10.0"},
             oversized, oversized, oversized],
        )
        self.assertEqual(outcome, "failed")
        self.assertEqual(saved, [75.0, 100.0])
        self.assertEqual(codecurl.status, StatusCodec.ERROR)
        self.assertEqual(codecurl.progress, 100.0)
