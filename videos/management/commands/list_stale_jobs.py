from django.core.management.base import BaseCommand

from videos.download import stale_pending_jobs


class Command(BaseCommand):
    help = "List pendiente downloads with no live worker thread (run in the web container)."

    def handle(self, *args, **options):
        for job in stale_pending_jobs():
            self.stdout.write(f"{job.id}\t{job.updated_at:%Y-%m-%d %H:%M:%S}\t{job.url[:80]}")
