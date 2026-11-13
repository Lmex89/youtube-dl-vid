import json
import logging
from enum import StrEnum

from django.conf import settings
from loguru import logger

from videos.lifecycle import (
    ProgressReporter, SuccessCommitUncertain, confirmed_success, mark_failed,
    record_success,
)
from videos.models import CodecUrls
from videos.process import parse_yt_dlp_progress
from videos.profiles import get_profile
from videos.storage import preview_url

cleanup_logger = logging.getLogger("videos")


class JobOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DISCARDED = "discarded"


def _fetch_and_process(codecurl, profile, paths, progress, runner) -> str:
    """Download, post-process and return the video title."""
    destination = paths.final
    workdir = paths.workdir
    if profile.has_post_process:
        workdir.mkdir(parents=True, exist_ok=True)

    title_path = paths.title_log
    title_path.unlink(missing_ok=True)
    command = profile.build_command(codecurl.url, destination, workdir)
    command += ["--print-to-file", "%(title)s", str(title_path)]
    runner.run(
        command,
        paths.log,
        label="yt_dlp",
        timeout=settings.MEDIA_PROCESS_TIMEOUT,
        progress_callback=progress.download,
        progress_parser=parse_yt_dlp_progress,
    )

    source = profile.locate_source(workdir, destination)
    if profile.has_post_process:
        profile.post_process(
            source,
            destination,
            paths.process_log,
            progress.post_process,
            runner=runner,
        )
        progress.post_process(100.0)

    title = (
        title_path.read_text(errors="replace").strip()
        if title_path.exists()
        else ""
    ) or destination.stem
    title_path.unlink(missing_ok=True)
    return title


def _log_failure(codecurl_id: str, request_id: str, exc: Exception) -> None:
    logger.error(json.dumps({
        "event": "video_download_failed",
        "codecurl_id": codecurl_id,
        "error": str(exc)[:100],
        "error_type": exc.__class__.__name__,
        "request_id": request_id,
    }))


def run_job(codecurl_id: str, request_id: str, *, runner, storage, retention) -> JobOutcome:
    """Run one download synchronously and return succeeded, failed or discarded."""
    try:
        codecurl = CodecUrls.objects.get(pk=codecurl_id)
        profile = get_profile(codecurl.output_profile)
        progress = ProgressReporter(codecurl_id, download_weight=profile.download_weight)
        paths = storage.job_paths(codecurl_id, profile)
        destination = paths.final

        logger.info(
            json.dumps({
                "event": "download_pipeline_started",
                "codecurl_id": codecurl_id,
                "output_profile": profile.name,
                "url_preview": preview_url(codecurl.url),
                "request_id": request_id,
            })
        )

        title = _fetch_and_process(codecurl, profile, paths, progress, runner)
        try:
            codecurl = record_success(codecurl_id, profile, destination, title)
        except SuccessCommitUncertain as exc:
            try:
                codecurl = confirmed_success(codecurl_id, destination)
            except Exception:
                codecurl = None
            if codecurl is None:
                # shortcut: unconfirmed commits need manual reconciliation; add recovery if needed.
                cleanup_logger.error(json.dumps({
                    "event": "download_success_unconfirmed",
                    "codecurl_id": codecurl_id,
                    "error_type": exc.__cause__.__class__.__name__,
                    "request_id": request_id,
                }))
                _log_failure(codecurl_id, request_id, exc.__cause__)
                return JobOutcome.FAILED
        except Exception:
            try:
                destination.unlink(missing_ok=True)
            except Exception as cleanup_error:
                cleanup_logger.error(json.dumps({
                    "event": "download_final_cleanup_failed",
                    "codecurl_id": codecurl_id,
                    "error_type": cleanup_error.__class__.__name__,
                    "request_id": request_id,
                }))
            raise

        discarded = False
        try:
            discarded = retention.discard_if_superseded(
                codecurl, request_id=request_id
            )
            if not discarded:
                retention.supersede_earlier(codecurl, request_id=request_id)
        except Exception as exc:
            logger.exception(
                json.dumps({
                    "event": "download_supersede_failed",
                    "codecurl_id": codecurl_id,
                    "error": str(exc)[:100],
                    "error_type": exc.__class__.__name__,
                    "request_id": request_id,
                })
            )

        if not discarded:
            logger.info(
                json.dumps({
                    "event": "video_download_completed",
                    "codecurl_id": codecurl_id,
                    "file_path": str(destination),
                    "output_profile": profile.name,
                    "request_id": request_id,
                })
            )
        return JobOutcome.DISCARDED if discarded else JobOutcome.SUCCEEDED
    except Exception as exc:
        _log_failure(codecurl_id, request_id, exc)
        mark_failed(codecurl_id)
        return JobOutcome.FAILED
    finally:
        storage.discard_staging_safely(codecurl_id, request_id)
