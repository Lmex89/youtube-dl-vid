"""Single owner of the physical layout of download artifacts."""

import json
import mimetypes
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from django.conf import settings
from loguru import logger

from videos.models import CodecUrls, VideosUploaded
from videos.profiles import get_profile

STAGING_DIR_NAME = ".tmp"
URL_PREVIEW_LEN = 50


def _preview(url: str) -> str:
    return url[:URL_PREVIEW_LEN] + ("..." if len(url) > URL_PREVIEW_LEN else "")


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

    @staticmethod
    def _superseded_ids(codecurl) -> list:
        # Lazy import: videos.download imports storage at module scope.
        from videos.download import is_download_running

        earlier = (
            CodecUrls.objects.filter(
                url=codecurl.url,
                output_profile=codecurl.output_profile,
                created_at__lt=codecurl.created_at,
            )
            .exclude(pk=codecurl.pk)
            .values_list("id", flat=True)
        )
        return [
            old_id
            for old_id in earlier
            if not is_download_running(str(old_id))
        ]

    @staticmethod
    def _delete_file(path: Path) -> bool:
        if path.is_file():
            path.unlink(missing_ok=True)
            return True
        return False

    def _delete_owned_file(self, file_path: Optional[str]) -> bool:
        """Delete an uploaded file only when it lives under the downloads root."""
        if not file_path:
            return False
        path = Path(file_path)
        try:
            (path.parent.resolve() / path.name).relative_to(
                self.downloads_dir.resolve()
            )
        except ValueError:
            return False
        return self._delete_file(path)

    def commit(self, codecurl, request_id: str = "unknown") -> dict:
        """Make *codecurl* replace earlier jobs for its URL and profile.

        Removes the superseded Video subido rows and their final files, staging
        directories and logs. Solicitud de descarga rows are kept as history.
        """
        superseded = self._superseded_ids(codecurl)
        counts = {
            "superseded_requests": len(superseded),
            "files_deleted": 0,
            "staging_dirs_deleted": 0,
            "logs_deleted": 0,
            "uploads_deleted": 0,
        }
        if not superseded:
            return counts

        profile = get_profile(codecurl.output_profile)
        for old_id in superseded:
            paths = self.job_paths(str(old_id), profile)

            uploads = list(
                VideosUploaded.objects.filter(codecurl_id=old_id)
            )
            for upload in uploads:
                if self._delete_owned_file(upload.file_path):
                    counts["files_deleted"] += 1
            deleted, _ = VideosUploaded.objects.filter(
                codecurl_id=old_id
            ).delete()
            counts["uploads_deleted"] += deleted

            if self._delete_file(paths.final):
                counts["files_deleted"] += 1

            staging = self.staging_root / str(old_id)
            if staging.is_dir() and not staging.is_symlink():
                self.discard_staging(str(old_id))
                counts["staging_dirs_deleted"] += 1

            for log_path in (paths.log, paths.process_log, paths.title_log):
                if self._delete_file(log_path):
                    counts["logs_deleted"] += 1

        logger.info(
            json.dumps({
                "event": "download_superseded",
                "codecurl_id": str(codecurl.id),
                "url_preview": _preview(codecurl.url),
                "output_profile": codecurl.output_profile,
                **counts,
                "request_id": request_id,
            })
        )
        return counts
