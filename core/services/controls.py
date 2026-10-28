"""User-facing actions on a download. Each takes a row lock so it cannot race the scheduler."""
from django.db import transaction
from django.utils import timezone

from ..models import AppSettings, Download
from . import cleanup, retry


class ControlError(Exception):
    pass


def _locked(dl_id):
    dl = Download.objects.select_for_update().filter(pk=dl_id).first()
    if dl is None:
        raise ControlError("download not found")
    return dl


def pause(dl_id):
    with transaction.atomic():
        dl = _locked(dl_id)
        if dl.status == Download.RUNNING:
            dl.ctl = Download.CTL_PAUSE
        elif dl.status in (Download.QUEUED, Download.RETRYING):
            dl.status = Download.PAUSED
            dl.next_retry_at = None
        else:
            raise ControlError("cannot pause a %s download" % dl.status)
        dl.save(update_fields=["ctl", "status", "next_retry_at"])
        return dl


def resume(dl_id):
    with transaction.atomic():
        dl = _locked(dl_id)
        if dl.status == Download.PAUSED:
            dl.status = Download.QUEUED
        elif dl.status == Download.RUNNING and dl.ctl == Download.CTL_PAUSE:
            dl.ctl = ""  # pause requested but not executed yet: revoke it
        else:
            raise ControlError("cannot resume a %s download" % dl.status)
        dl.save(update_fields=["status", "ctl"])
        return dl


def cancel(dl_id):
    app = AppSettings.get_solo()
    with transaction.atomic():
        dl = _locked(dl_id)
        if dl.status == Download.RUNNING:
            dl.ctl = Download.CTL_CANCEL
            dl.save(update_fields=["ctl"])
            return dl
        if dl.status not in Download.ACTIVE:
            raise ControlError("cannot cancel a %s download" % dl.status)
        dl.status = Download.CANCELLED
        dl.finished_at = timezone.now()
        dl.next_retry_at = None
        dl.save(update_fields=["status", "finished_at", "next_retry_at"])
    cleanup.remove_partials(dl, app)
    return dl


def retry_now(dl_id):
    """failed/cancelled -> back to the queue with a fresh retry budget; retrying -> run now."""
    with transaction.atomic():
        dl = _locked(dl_id)
        if dl.status in (Download.FAILED, Download.CANCELLED):
            dl.status = Download.QUEUED
            dl.attempt = 0
            dl.finished_at = None
            dl.last_error = ""
            dl.ctl = ""
        elif dl.status == Download.RETRYING:
            dl.next_retry_at = timezone.now()
        else:
            raise ControlError("cannot retry a %s download" % dl.status)
        dl.save(update_fields=["status", "attempt", "finished_at", "last_error", "ctl", "next_retry_at"])
        return dl


def set_retries(dl_id, value):
    try:
        n = retry.parse_retries(value)
    except ValueError as exc:
        raise ControlError(str(exc)) from None
    with transaction.atomic():
        dl = _locked(dl_id)
        if dl.status in Download.TERMINAL:
            raise ControlError("download is already finished")
        dl.max_retries = n
        dl.save(update_fields=["max_retries"])
        return dl


def remove(dl_id, delete_files=False):
    """Delete the entry (and leftovers). Running downloads must be cancelled first."""
    app = AppSettings.get_solo()
    with transaction.atomic():
        dl = _locked(dl_id)
        if dl.status == Download.RUNNING:
            raise ControlError("cancel the running download first")
        cleanup.remove_partials(dl, app)
        if delete_files and dl.status == Download.COMPLETED:
            cleanup.remove_final(dl, app)
        dl.delete()


def move(dl_id, direction):
    """Reorder among the owner's queued downloads: top | up | down | bottom."""
    if direction not in ("top", "up", "down", "bottom"):
        raise ControlError("invalid direction")
    with transaction.atomic():
        target = _locked(dl_id)
        if target.status != Download.QUEUED:
            raise ControlError("only queued downloads can be reordered")
        rows = list(Download.objects.select_for_update()
                    .filter(owner_id=target.owner_id, status=Download.QUEUED).order_by("position", "id"))
        idx = next(i for i, r in enumerate(rows) if r.pk == target.pk)
        row = rows.pop(idx)
        new = {"top": 0, "bottom": len(rows), "up": max(idx - 1, 0), "down": min(idx + 1, len(rows))}[direction]
        rows.insert(new, row)
        slots = sorted(r.position for r in rows)
        for i in range(1, len(slots)):
            if slots[i] <= slots[i - 1]:
                slots[i] = slots[i - 1] + 1
        for r, pos in zip(rows, slots):
            if r.position != pos:
                Download.objects.filter(pk=r.pk).update(position=pos)
