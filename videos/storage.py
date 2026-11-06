"""Single owner of the physical layout of download artifacts."""

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from django.conf import settings

STAGING_DIR_NAME = ".tmp"


@dataclass(frozen=True)
class JobPaths:
    final: Path
    workdir: Path
    log: Path
    process_log: Path
    title_log: Path


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
