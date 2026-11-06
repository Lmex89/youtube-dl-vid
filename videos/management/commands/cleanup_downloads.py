import json
import uuid

from django.core.management.base import BaseCommand

from videos.storage import DownloadStorage


class Command(BaseCommand):
    help = "Delete Video subido rows without a Solicitud de descarga and their files."

    def add_arguments(self, parser):
        parser.add_argument(
            "--request-id",
            default=None,
            help="Correlation id for lifecycle logs (default: fresh uuid4).",
        )

    def handle(self, *args, **options):
        request_id = options["request_id"] or str(uuid.uuid4())
        counts = DownloadStorage().cleanup_orphans(request_id=request_id)
        self.stdout.write(json.dumps(counts))
