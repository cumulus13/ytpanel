"""Queue scheduler: claims queued/due-retry downloads up to max_simultaneous, and a reaper for
downloads whose worker died."""
import logging
import uuid
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from ..models import AppSettings, Download
from . import live, process

log = logging.getLogger(__name__)


def _default_dispatch(dl_id, token):
    from ..tasks import run_download_task
    run_download_task.apply_async(args=[dl_id, str(token)], queue="downloads")


def tick(dispatch=None, now=None):
    """Claim as many downloads as there are free slots. A singleton settings row is locked
    (SELECT ... FOR UPDATE) so concurrent ticks can never exceed max_simultaneous."""
    dispatch = dispatch or _default_dispatch
    now = now or timezone.now()
    AppSettings.get_solo()
    claimed = []
    with transaction.atomic():
        app = AppSettings.objects.select_for_update().get(pk=1)
        running = Download.objects.filter(status=Download.RUNNING).count()
        free = app.max_simultaneous - running
        if free > 0:
            due = (Q(status=Download.QUEUED) & (Q(start_at__isnull=True) | Q(start_at__lte=now))) | \
                  (Q(status=Download.RETRYING) & Q(next_retry_at__lte=now))
            rows = list(Download.objects.select_for_update(skip_locked=True)
                        .filter(due).order_by("position", "id")[:free])
            for dl in rows:
                dl.status = Download.RUNNING
                dl.run_token = uuid.uuid4()
                dl.heartbeat = now
                dl.started_at = now
                dl.runner_pid = None
                dl.ctl = ""
                dl.next_retry_at = None
                dl.save(update_fields=["status", "run_token", "heartbeat", "started_at",
                                       "runner_pid", "ctl", "next_retry_at"])
                claimed.append((dl.pk, dl.run_token))
    for dl_id, token in claimed:
        try:
            dispatch(dl_id, token)
        except Exception:  # broker down etc.: give the slot back immediately
            log.exception("dispatch failed for download %s; requeueing", dl_id)
            Download.objects.filter(pk=dl_id, run_token=token, status=Download.RUNNING).update(
                status=Download.QUEUED, run_token=None, heartbeat=None)
    return [c[0] for c in claimed]


def record_speed():
    """Append one sample to the aggregate series and to one series per user (isolation: users
    only ever read their own series)."""
    rows = list(Download.objects.filter(status=Download.RUNNING).values_list("pk", "owner_id"))
    lv = live.get_live_many([pk for pk, _ in rows])
    per_user = {}
    for pk, owner in rows:
        per_user[owner] = per_user.get(owner, 0) + (lv.get(pk) or {}).get("s", 0)
    previous = set(cache.get("series:active_users") or [])
    for uid in previous | set(per_user):
        live.push_series("user:%s" % uid, per_user.get(uid, 0))  # one trailing 0 once a user goes idle
    cache.set("series:active_users", sorted(per_user), 3600)
    total = sum(per_user.values())
    live.push_series("total", total)
    return total


def reap(now=None):
    """Downloads marked running without a heartbeat (worker killed, task lost) go back to the
    queue (or honour a pending pause/cancel). Orphan runner processes are killed, but only if
    /proc confirms the pid is the runner of that very download."""
    now = now or timezone.now()
    cutoff = now - timedelta(seconds=settings.YTP_HEARTBEAT_GRACE)
    reaped = []
    for dl in Download.objects.filter(status=Download.RUNNING, heartbeat__lt=cutoff):
        if dl.runner_pid and process.is_our_runner(dl.runner_pid, dl.pk):
            process.terminate(dl.runner_pid, grace=2.0)
        if dl.ctl == Download.CTL_CANCEL:
            new = {"status": Download.CANCELLED, "finished_at": now}
        elif dl.ctl == Download.CTL_PAUSE:
            new = {"status": Download.PAUSED}
        else:
            new = {"status": Download.QUEUED}
        n = Download.objects.filter(pk=dl.pk, status=Download.RUNNING, run_token=dl.run_token).update(
            run_token=None, runner_pid=None, ctl="", heartbeat=None, **new)
        if n:
            live.clear_live(dl.pk)
            reaped.append(dl.pk)
            log.warning("reaped download %s -> %s", dl.pk, new["status"])
    return reaped
