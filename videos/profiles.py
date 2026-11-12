import json
import shutil
from pathlib import Path
from typing import Callable, Dict, List, Optional

from django.conf import settings
from loguru import logger

from videos.media import probe_duration
from videos.models import OutputProfile
from videos.process import build_progress_parser
from videos.runner import CommandRunner

SOURCE_TEMPLATE = "source.%(ext)s"

COMMON_YT_DLP_ARGS = [
    "--no-cache-dir",
    "--socket-timeout", "30",
    "--newline",
]

ProgressCallback = Callable[[float], None]


class BaseDownloadProfile:
    name: str = ""
    extension: str = "mp4"
    mime_type: str = "application/octet-stream"
    download_weight: float = 1.0
    post_process = None

    def build_command(
        self, url: str, destination: Path, workdir: Path
    ) -> List[str]:
        raise NotImplementedError

    def locate_source(self, workdir: Path, destination: Path) -> Path:
        return destination


class PostProcessedProfile(BaseDownloadProfile):
    def locate_source(self, workdir: Path, destination: Path) -> Path:
        preferred = workdir / f"source.{self.extension}"
        if preferred.exists():
            return preferred
        matches = sorted(workdir.glob("source.*"))
        if not matches:
            raise FileNotFoundError(f"yt-dlp produced no output in {workdir}")
        return matches[0]


class StandardVideoProfile(BaseDownloadProfile):
    name = OutputProfile.STANDARD_VIDEO
    extension = "mp4"
    mime_type = "video/mp4"

    def build_command(
        self, url: str, destination: Path, workdir: Path
    ) -> List[str]:
        return [
            "yt-dlp",
            "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
            "-S", "res,ext:mp4:m4a",
            "--merge-output-format", "mp4",
            *COMMON_YT_DLP_ARGS,
            url, "-o", str(destination),
        ]


class WhatsappVideoProfile(PostProcessedProfile):
    name = OutputProfile.WHATSAPP_VIDEO
    extension = "mp4"
    mime_type = "video/mp4"
    download_weight = 0.75
    height_ladder = ((720, 128), (480, 96), (360, 64))
    MIN_VIDEO_KBPS = 150
    MAX_VIDEO_KBPS = 6000

    def build_command(
        self, url: str, destination: Path, workdir: Path
    ) -> List[str]:
        return [
            "yt-dlp",
            "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
            "-S", "res,ext:mp4:m4a",
            "--merge-output-format", "mp4",
            *COMMON_YT_DLP_ARGS,
            url, "-o", str(workdir / SOURCE_TEMPLATE),
        ]

    def post_process(
        self,
        source: Path,
        destination: Path,
        log_path: Path,
        on_progress: Optional[ProgressCallback],
        runner: Optional[CommandRunner] = None,
    ) -> None:
        if runner is None:
            raise ValueError("WhatsappVideoProfile.post_process requires a CommandRunner")
        max_bytes = settings.WHATSAPP_MEDIA_MAX_BYTES
        duration = probe_duration(source, runner=runner)

        for height, audio_kbps in self.height_ladder:
            command = self._build_ffmpeg_command(
                source, destination, duration, height, audio_kbps, max_bytes
            )
            progress_parser = build_progress_parser(duration) if duration else None
            runner.run(
                command,
                log_path,
                label="ffmpeg",
                timeout=settings.MEDIA_PROCESS_TIMEOUT,
                progress_callback=on_progress if progress_parser else None,
                progress_parser=progress_parser,
            )
            size = destination.stat().st_size
            logger.info(
                json.dumps({
                    "event": "whatsapp_video_encoded",
                    "height": height,
                    "size_bytes": size,
                    "max_size_bytes": max_bytes,
                    "within_limit": size <= max_bytes,
                })
            )
            if size <= max_bytes:
                return

        raise RuntimeError(
            "whatsapp video exceeds the configured size limit after re-encoding"
        )

    def _build_ffmpeg_command(
        self,
        source: Path,
        destination: Path,
        duration: Optional[float],
        height: int,
        audio_kbps: int,
        target_bytes: int,
    ) -> List[str]:
        video_kbps = self._target_video_kbps(
            duration, audio_kbps, target_bytes
        )
        if video_kbps is None:
            rate_args = [
                "-crf", "26",
                "-maxrate", "1000k",
                "-bufsize", "2000k",
            ]
        else:
            rate_args = [
                "-b:v", f"{video_kbps}k",
                "-maxrate", f"{int(video_kbps * 1.3)}k",
                "-bufsize", f"{video_kbps * 2}k",
            ]

        return [
            settings.FFMPEG_BINARY,
            "-y",
            "-i", str(source),
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-profile:v", "main",
            "-bf", "0",
            "-pix_fmt", "yuv420p",
            *rate_args,
            "-vf", f"scale=-2:'min({height},ih)'",
            "-c:a", "aac",
            "-b:a", f"{audio_kbps}k",
            "-ac", "2",
            "-movflags", "+faststart",
            "-progress", "pipe:1",
            "-nostats",
            str(destination),
        ]

    def _target_video_kbps(
        self,
        duration: Optional[float],
        audio_kbps: int,
        target_bytes: int,
    ) -> Optional[int]:
        if not duration or duration <= 0:
            return None
        total_kbps = target_bytes * 8 / duration / 1000
        video_kbps = int(total_kbps - audio_kbps - 32)
        return max(self.MIN_VIDEO_KBPS, min(video_kbps, self.MAX_VIDEO_KBPS))


class AudioM4aProfile(PostProcessedProfile):
    name = OutputProfile.AUDIO_M4A
    extension = "m4a"
    mime_type = "audio/mp4"
    download_weight = 0.9

    def build_command(
        self, url: str, destination: Path, workdir: Path
    ) -> List[str]:
        return [
            "yt-dlp",
            "-f", "bestaudio/best",
            "-x",
            "--audio-format", "m4a",
            "--audio-quality", "128K",
            *COMMON_YT_DLP_ARGS,
            url, "-o", str(workdir / SOURCE_TEMPLATE),
        ]

    def post_process(
        self,
        source: Path,
        destination: Path,
        log_path: Path,
        on_progress: Optional[ProgressCallback],
        runner: Optional[CommandRunner] = None,
    ) -> None:
        shutil.move(str(source), str(destination))


PROFILES: Dict[str, BaseDownloadProfile] = {
    profile.name: profile
    for profile in (
        StandardVideoProfile(),
        WhatsappVideoProfile(),
        AudioM4aProfile(),
    )
}


def get_profile(name: str) -> BaseDownloadProfile:
    try:
        return PROFILES[name]
    except KeyError:
        raise ValueError(f"unsupported output_profile: {name}")
