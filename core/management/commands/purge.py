from django.core.management.base import BaseCommand, CommandError

from core.models import Download
from core.services import purge


class Command(BaseCommand):
    help = "Purge finished downloads (completed/failed/cancelled) and their leftover partial files."

    def add_arguments(self, parser):
        parser.add_argument("--status", action="append", choices=list(Download.TERMINAL),
                            help="repeatable; default: all finished states")
        parser.add_argument("--older-than-days", type=int, default=None)
        parser.add_argument("--delete-files", action="store_true", help="also delete completed files")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *a, **o):
        try:
            res = purge.purge(statuses=o["status"], older_than_days=o["older_than_days"],
                              delete_files=o["delete_files"], dry_run=o["dry_run"])
        except ValueError as exc:
            raise CommandError(str(exc))
        self.stdout.write(str(res))
