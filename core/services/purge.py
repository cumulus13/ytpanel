"""Purge: remove finished entries (completed / failed / cancelled) from the list, always
cleaning leftover partial files, and optionally the downloaded files themselves."""
import os
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ..models import AppSettings, Download
from . import cleanup, security


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def purge(*, user=None, statuses=None, older_than_days=None, delete_files=False, dry_run=False, now=None):
    app = AppSettings.get_solo()
    now = now or timezone.now()
    statuses = set(statuses) if statuses else set(Download.TERMINAL)
    bad = statuses - set(Download.TERMINAL)
    if bad:
        raise ValueError("only finished entries can be purged (completed/failed/cancelled), not: %s"
                         % ", ".join(sorted(bad)))
    qs = Download.objects.filter(status__in=statuses)
    if user is not None:
        qs = qs.filter(owner=user)
    if older_than_days is not None:
        cutoff = now - timedelta(days=older_than_days)
        qs = qs.filter(Q(finished_at__lt=cutoff) | Q(finished_at__isnull=True, created_at__lt=cutoff))

    result = {"entries": 0, "files_deleted": 0, "partials_deleted": 0, "bytes_freed": 0, "dry_run": dry_run}
    roots = security.allowed_dirs(app)

    for pk in list(qs.values_list("pk", flat=True)):
        with transaction.atomic():
            dl = Download.objects.select_for_update().filter(pk=pk, status__in=statuses).first()
            if dl is None:  # changed state meanwhile (e.g. user retried it): leave it alone
                continue
            result["entries"] += 1
            if dry_run:
                for p in cleanup.partial_paths(dl):
                    if os.path.isfile(p):
                        result["partials_deleted"] += 1
                        result["bytes_freed"] += _size(p)
                if delete_files and dl.status == Download.COMPLETED and dl.filepath and os.path.isfile(dl.filepath):
                    result["files_deleted"] += 1
                    result["bytes_freed"] += _size(dl.filepath)
                continue
            for p in cleanup.partial_paths(dl):
                size = _size(p)
                if security.safe_unlink(p, app):
                    result["partials_deleted"] += 1
                    result["bytes_freed"] += size
            if delete_files and dl.status == Download.COMPLETED and dl.filepath:
                size = _size(dl.filepath)
                if cleanup.remove_final(dl, app):
                    result["files_deleted"] += 1
                    result["bytes_freed"] += size
            dl.delete()
    return result


def auto_purge(now=None):
    """Periodic task body: apply the retention setting (0 = disabled)."""
    app = AppSettings.get_solo()
    if not app.retention_days:
        return None
    return purge(older_than_days=app.retention_days, delete_files=app.retention_delete_files, now=now)
