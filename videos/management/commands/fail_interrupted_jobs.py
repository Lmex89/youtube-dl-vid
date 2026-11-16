from django.core.management.base import BaseCommand

from videos.lifecycle import mark_failed, pending_requests
from videos.models import VideosUploaded
from videos.profiles import get_profile
from videos.storage import DownloadStorage

REASON = "Interrumpida por un reinicio del servicio."


class Command(BaseCommand):
    help = "Mark pendiente jobs as failed and remove their leftovers. Run at boot, before workers start."

    def handle(self, *args, **options):
        storage = DownloadStorage()
        for job in pending_requests():
            mark_failed(job.id, REASON)
            storage.discard_staging_safely(str(job.id), "boot-sweep")
            if not VideosUploaded.objects.filter(codecurl=job).exists():
                storage.job_paths(job.id, get_profile(job.output_profile)).final.unlink(missing_ok=True)
            self.stdout.write(f"failed {job.id}")
