"""Single owner of the physical layout of download artifacts."""

import mimetypes
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from django.conf import settings

from videos.profiles import get_profile

STAGING_DIR_NAME = ".tmp"


@dataclass(frozen=True)
class JobPaths:
    final: Path
    workdir: Path
    log: Path
    process_log: Path
    title_log: Path


@dataclass(frozen=True)
class ServedFile:
    path: Optional[Path]
    mime_type: str
    filename: str
    exists: bool


class DownloadStorage:
    """Resolve artifact locations; roots are injected at construction time."""

    def __init__(
        self,
        downloads_dir: Optional[Path] = None,
        logs_dir: Optional[Path] = None,
    ):
        self.downloads_dir = Path(
            downloads_dir
            if downloads_dir is not None
            else settings.DOWNLOADS_DIR
        )
        self.logs_dir = Path(
            logs_dir if logs_dir is not None else settings.DOWNLOAD_LOGS_DIR
        )

    @property
    def staging_root(self) -> Path:
        return self.downloads_dir / STAGING_DIR_NAME

    def job_paths(self, codecurl_id: str, profile) -> JobPaths:
        codecurl_id = str(codecurl_id)
        workdir = (
            self.staging_root / codecurl_id
            if profile.uses_temporary_files
            else self.downloads_dir
        )
        return JobPaths(
            final=self.downloads_dir / f"{codecurl_id}.{profile.extension}",
            workdir=workdir,
            log=self.logs_dir / f"{codecurl_id}.txt",
            process_log=self.logs_dir / f"{codecurl_id}.ffmpeg.txt",
            title_log=self.logs_dir / f"{codecurl_id}.title.txt",
        )

    def resolve(self, upload) -> ServedFile:
        """Describe the servable file for a Video subido."""
        path = Path(upload.file_path) if upload.file_path else None
        profile = None
        if upload.codecurl_id:
            try:
                profile = get_profile(upload.codecurl.output_profile)
            except ValueError:
                profile = None

        extension = (
            profile.extension
            if profile
            else (path.suffix[1:] if path else "")
        )
        mime_type = (
            (profile.mime_type if profile else "")
            or upload.mime_type
            or (mimetypes.guess_type(path.name)[0] if path else "")
            or "application/octet-stream"
        )
        return ServedFile(
            path=path,
            mime_type=mime_type,
            filename=f"{upload.id}.{extension}" if extension else str(upload.id),
            exists=bool(path and path.exists()),
        )

    def discard_staging(self, codecurl_id: str) -> None:
        """Remove one job's staging directory; never the staging root."""
        codecurl_id = str(codecurl_id)
        staging_dir = self.staging_root / codecurl_id
        if (
            codecurl_id not in {".", ".."}
            and Path(codecurl_id).name == codecurl_id
            and staging_dir.parent == self.staging_root
            and not self.staging_root.is_symlink()
            and not staging_dir.is_symlink()
        ):
            shutil.rmtree(staging_dir, ignore_errors=True)
