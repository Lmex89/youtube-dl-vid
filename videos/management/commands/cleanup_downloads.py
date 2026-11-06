import json

from django.core.management.base import BaseCommand

from videos.storage import DownloadStorage


class Command(BaseCommand):
    help = "Delete Video subido rows without a Solicitud de descarga and their files."

    def handle(self, *args, **options):
        counts = DownloadStorage().cleanup_orphans(
            request_id="cleanup_downloads"
        )
        self.stdout.write(json.dumps(counts))
