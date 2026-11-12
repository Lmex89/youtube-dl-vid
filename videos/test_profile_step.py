import tempfile
from pathlib import Path

from django.test import SimpleTestCase, TestCase

from videos.job_run import run_job
from videos.models import CodecUrls, OutputProfile, StatusCodec, VideosUploaded
from videos.profiles import get_profile
from videos.storage import DownloadRetention, DownloadStorage
from videos.test_utils import FakeCommandRunner


class ProfileStepTests(SimpleTestCase):
    def test_registry_exposes_optional_post_process_step(self):
        self.assertIsNone(get_profile(OutputProfile.STANDARD_VIDEO).post_process)
        for name in OutputProfile.values:
            with self.subTest(profile=name):
                self.assertFalse(hasattr(get_profile(name), "uses_temporary_files"))
        for name in (OutputProfile.WHATSAPP_VIDEO, OutputProfile.AUDIO_M4A):
            with self.subTest(profile=name):
                self.assertTrue(callable(get_profile(name).post_process))

    def test_commands_use_paths_selected_for_each_profile(self):
        storage = DownloadStorage(Path("/downloads"), Path("/logs"))
        for name in OutputProfile.values:
            with self.subTest(profile=name):
                profile = get_profile(name)
                paths = storage.job_paths("job-id", profile)
                command = profile.build_command(
                    "https://example.com/video", paths.final, paths.workdir
                )
                output = Path(command[command.index("-o") + 1])
                if name == OutputProfile.STANDARD_VIDEO:
                    self.assertEqual(paths.workdir, Path("/downloads"))
                    self.assertEqual(output, paths.final)
                else:
                    self.assertEqual(paths.workdir, Path("/downloads/.tmp/job-id"))
                    self.assertEqual(output, paths.workdir / "source.%(ext)s")


class ProfileStagingTests(TestCase):
    def test_job_uses_staging_only_for_profiles_with_a_step(self):
        class ObservingRunner(FakeCommandRunner):
            def run(self, command, log_path, label, timeout, **kwargs):
                if label == "yt_dlp":
                    self.staging_exists = staging.is_dir()
                return super().run(command, log_path, label, timeout, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            storage = DownloadStorage(Path(tmp) / "downloads", Path(tmp) / "logs")
            for name in OutputProfile.values:
                with self.subTest(profile=name):
                    request = CodecUrls.objects.create(
                        url="https://example.com/video", output_profile=name
                    )
                    staging = storage.staging_root / str(request.id)
                    runner = ObservingRunner()
                    outcome = run_job(
                        str(request.id), "request-47", runner=runner, storage=storage,
                        retention=DownloadRetention(storage),
                    )
                    request.refresh_from_db()
                    self.assertEqual(outcome, "succeeded")
                    self.assertEqual(request.status, StatusCodec.SUCCESS)
                    self.assertEqual(
                        runner.staging_exists, name != OutputProfile.STANDARD_VIDEO
                    )
                    self.assertFalse(staging.exists())
                    upload = VideosUploaded.objects.get(codecurl=request)
                    self.assertTrue(Path(upload.file_path).is_file())

    def test_failed_download_removes_partial_staging_for_both_profiles(self):
        class PartialFailureRunner(FakeCommandRunner):
            def run(self, command, log_path, label, timeout, **kwargs):
                output = Path(command[command.index("-o") + 1])
                output.write_bytes(b"partial")
                self.partial_created = True
                raise RuntimeError("download failed")

        with tempfile.TemporaryDirectory() as tmp:
            storage = DownloadStorage(Path(tmp) / "downloads", Path(tmp) / "logs")
            for name in (OutputProfile.WHATSAPP_VIDEO, OutputProfile.AUDIO_M4A):
                with self.subTest(profile=name):
                    request = CodecUrls.objects.create(
                        url="https://example.com/video", output_profile=name
                    )
                    runner = PartialFailureRunner()
                    outcome = run_job(
                        str(request.id), "request-47", runner=runner,
                        storage=storage, retention=DownloadRetention(storage),
                    )
                    request.refresh_from_db()
                    self.assertEqual(outcome, "failed")
                    self.assertTrue(runner.partial_created)
                    self.assertEqual(request.status, StatusCodec.ERROR)
                    self.assertFalse(VideosUploaded.objects.filter(codecurl=request).exists())
                    self.assertFalse((storage.staging_root / str(request.id)).exists())

    def test_audio_post_process_failure_removes_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = DownloadStorage(Path(tmp) / "downloads", Path(tmp) / "logs")
            request = CodecUrls.objects.create(
                url="https://example.com/video", output_profile=OutputProfile.AUDIO_M4A
            )
            staging = storage.staging_root / str(request.id)
            destination = storage.downloads_dir / f"{request.id}.m4a"
            destination.mkdir(parents=True)
            (destination / "source.m4a").write_bytes(b"existing")
            runner = FakeCommandRunner(scripts=[{"output_file": str(staging / "source.m4a")}])

            outcome = run_job(
                str(request.id), "request-47", runner=runner, storage=storage,
                retention=DownloadRetention(storage),
            )

            request.refresh_from_db()
            self.assertEqual(outcome, "failed")
            self.assertEqual(request.status, StatusCodec.ERROR)
            self.assertFalse(VideosUploaded.objects.filter(codecurl=request).exists())
            self.assertFalse(staging.exists())
            self.assertEqual((destination / "source.m4a").read_bytes(), b"existing")
