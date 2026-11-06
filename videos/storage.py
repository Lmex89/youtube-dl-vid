"""Single owner of the physical layout of download artifacts."""

import json
import logging
import mimetypes
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from django.conf import settings

from videos.models import CodecUrls, StatusCodec, VideosUploaded
from videos.profiles import get_profile

STAGING_DIR_NAME = ".tmp"
URL_PREVIEW_LEN = 50

logger = logging.getLogger("videos")


def preview_url(url: str) -> str:
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
        is_running: Callable[[str], bool] = lambda codecurl_id: False,
    ):
        # Injected to avoid importing videos.download (circular dependency).
        self._is_running = is_running
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

    def discard_staging(self, codecurl_id: str) -> bool:
        """Remove one job's staging directory; never the staging root.

        Returns True when a directory was actually removed.
        """
        codecurl_id = str(codecurl_id)
        staging_dir = self.staging_root / codecurl_id
        if (
            codecurl_id not in {".", ".."}
            and Path(codecurl_id).name == codecurl_id
            and staging_dir.parent == self.staging_root
            and not self.staging_root.is_symlink()
            and not staging_dir.is_symlink()
            and staging_dir.is_dir()
        ):
            shutil.rmtree(staging_dir, ignore_errors=True)
            return True
        return False

    def _superseded_ids(self, codecurl) -> list:
        """Earlier jobs for the same URL and profile that are not running.

        shortcut: running state is per-process (thread registry), so a job
        running in another gunicorn worker is not protected; upgrade by
        persisting a heartbeat if multi-worker races show up.
        """
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
            old_id for old_id in earlier if not self._is_running(str(old_id))
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

    def supersede_earlier(self, codecurl, request_id: str = "unknown") -> dict:
        """Make *codecurl* replace earlier jobs for its URL and profile.

        Removes the superseded Video subido rows and their final files, staging
        directories and logs. Superseded successful Solicitud de descarga rows
        are deleted with their artifacts; failed attempts stay as history.
        """
        superseded = self._superseded_ids(codecurl)
        counts = {
            "superseded_requests": len(superseded),
            "files_deleted": 0,
            "staging_dirs_deleted": 0,
            "logs_deleted": 0,
            "uploads_deleted": 0,
            "codecurls_deleted": 0,
        }
        if not superseded:
            return counts

        profile = get_profile(codecurl.output_profile)
        for old_id in superseded:
            old_codecurl = CodecUrls.objects.filter(pk=old_id).first()
            paths = self.job_paths(str(old_id), profile)

            for upload in VideosUploaded.objects.filter(codecurl_id=old_id):
                if self._delete_owned_file(upload.file_path):
                    counts["files_deleted"] += 1
                upload.delete()
                counts["uploads_deleted"] += 1

            if old_codecurl and old_codecurl.status == StatusCodec.SUCCESS:
                old_codecurl.delete()
                counts["codecurls_deleted"] += 1

            if self._delete_file(paths.final):
                counts["files_deleted"] += 1

            if self.discard_staging(str(old_id)):
                counts["staging_dirs_deleted"] += 1

            for log_path in (paths.log, paths.process_log, paths.title_log):
                if self._delete_file(log_path):
                    counts["logs_deleted"] += 1

        logger.info(
            json.dumps({
                "event": "download_superseded",
                "codecurl_id": str(codecurl.id),
                "url_preview": preview_url(codecurl.url),
                "output_profile": codecurl.output_profile,
                **counts,
                "request_id": request_id,
            })
        )
        return counts

    def cleanup_orphans(self, request_id: str = "unknown") -> dict:
        """Delete Video subido rows without a Solicitud de descarga.

        Removes each row and its file only when the file lives under the
        downloads root; never removes the root itself. Missing files are fine.
        """
        counts = {"files_deleted": 0, "uploads_deleted": 0}
        orphans = list(VideosUploaded.objects.filter(codecurl__isnull=True))
        for upload in orphans:
            if self._delete_owned_file(upload.file_path):
                counts["files_deleted"] += 1
            upload.delete()
            counts["uploads_deleted"] += 1

        logger.info(
            json.dumps({
                "event": "download_orphans_cleaned",
                **counts,
                "request_id": request_id,
            })
        )
        return counts
