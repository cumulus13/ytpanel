import importlib.metadata
import json
import os
from datetime import datetime, timezone as dt_tz
from functools import wraps

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET, require_POST

from .forms import AppSettingsForm
from .models import INFINITE, AppSettings, Download
from .services import controls, formats, live, purge as purge_service, retry, security

STATUS_ORDER = [Download.RUNNING, Download.RETRYING, Download.QUEUED, Download.PAUSED,
                Download.FAILED, Download.CANCELLED, Download.COMPLETED]


# ---- helpers ------------------------------------------------------------------------------
def api(view):
    """JSON API wrapper: 401 instead of redirect, uniform error mapping."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "authentication required"}, status=401)
        try:
            return view(request, *args, **kwargs)
        except controls.ControlError as exc:
            return JsonResponse({"error": str(exc)}, status=409)
        except (security.ValidationError, ValueError) as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        except Http404:
            return JsonResponse({"error": "not found"}, status=404)
    return wrapper


def _body(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except ValueError:
        raise ValueError("invalid JSON body") from None
    if not isinstance(data, dict):
        raise ValueError("JSON object expected")
    return data


def _scope(request):
    qs = Download.objects.all()
    return qs if request.user.is_staff else qs.filter(owner=request.user)


def _get(request, pk):
    obj = _scope(request).filter(pk=pk).first()
    if obj is None:
        raise Http404
    return obj


def _iso(dt):
    return dt.astimezone(dt_tz.utc).isoformat() if dt else None


def serialize(d, lv, now, show_owner):
    live_ok = d.status == Download.RUNNING and lv
    downloaded = lv["d"] if live_ok else d.downloaded_bytes
    total = (lv.get("t") if live_ok else None) or d.total_bytes
    if d.status == Download.COMPLETED:
        pct = 100.0
    else:
        pct = round(min(100.0, 100.0 * downloaded / total), 1) if total else None
    nxt = None
    if d.status == Download.RETRYING and d.next_retry_at:
        nxt = max(0, int((d.next_retry_at - now).total_seconds()))
    return {
        "id": d.pk, "url": d.url, "title": d.title, "status": d.status,
        "format": d.format, "backend": d.backend,
        "downloaded": downloaded, "total": total, "percent": pct,
        "speed": lv["s"] if live_ok else 0, "eta": lv.get("e") if live_ok else None,
        "attempt": d.attempt, "max_retries": d.max_retries,
        "retries_label": "%d/%s" % (d.attempt, retry.retries_label(d.max_retries)),
        "next_retry_in": nxt, "error": (d.last_error or "")[:500],
        "filename": os.path.basename(d.filepath) if d.filepath else "", "filesize": d.filesize,
        "owner": d.owner.username if show_owner else None,
        "created_at": _iso(d.created_at), "finished_at": _iso(d.finished_at),
        "start_at": _iso(d.start_at),
    }


# ---- pages --------------------------------------------------------------------------------
@login_required
def dashboard(request):
    app = AppSettings.get_solo()
    config = {
        "presets": list(formats.PRESETS), "default_format": app.default_format,
        "default_retries": app.default_retries, "dirs": [str(p) for p in security.allowed_dirs(app)],
        "is_staff": request.user.is_staff,
    }
    return render(request, "core/dashboard.html", {"config": config})


@login_required
def settings_page(request):
    if not request.user.is_staff:
        raise PermissionDenied
    app = AppSettings.get_solo()
    form = AppSettingsForm(request.POST or None, instance=app)
    saved = False
    if request.method == "POST" and form.is_valid():
        form.save()
        saved = True
    versions = {}
    for label, dist in (("yt-dlp", "yt-dlp"), ("youtube-dl", "youtube_dl")):
        try:
            versions[label] = importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            versions[label] = None
    return render(request, "core/settings.html", {
        "form": form, "saved": saved, "versions": versions, "ffmpeg": formats.has_ffmpeg()})


# ---- API ----------------------------------------------------------------------------------
@api
@require_GET
def snapshot(request):
    now = timezone.now()
    scope = _scope(request)
    rows = list(scope.select_related("owner").order_by("-id")[:settings.YTP_MAX_LIST])
    lv = live.get_live_many([r.pk for r in rows if r.status == Download.RUNNING])
    counts = {s: 0 for s in STATUS_ORDER}
    for item in scope.values("status").annotate(n=Count("id")):
        counts[item["status"]] = item["n"]
    speed = sum((lv.get(r.pk) or {}).get("s", 0) for r in rows if r.status == Download.RUNNING)
    series_name = "total" if request.user.is_staff else "user:%s" % request.user.pk
    return JsonResponse({
        "now": now.timestamp(),
        "downloads": [serialize(r, lv.get(r.pk), now, request.user.is_staff) for r in rows],
        "counts": counts, "speed": speed, "series": live.get_series(series_name),
        "truncated": scope.count() > len(rows),
    })


@api
@require_POST
def add(request):
    data = _body(request)
    app = AppSettings.get_solo()
    raw = data.get("urls", data.get("url", ""))
    urls = [u.strip() for u in (raw if isinstance(raw, list) else str(raw).splitlines()) if str(u).strip()]
    if not urls:
        raise ValueError("no URL given")
    if len(urls) > 200:
        raise ValueError("at most 200 URLs per request")
    fmt = formats.validate_format(data.get("format") or app.default_format)
    max_retries = retry.parse_retries(data["max_retries"]) if data.get("max_retries") not in (None, "") \
        else app.default_retries
    try:
        rate = int(data.get("ratelimit_kib") or 0)
    except (TypeError, ValueError):
        raise ValueError("ratelimit_kib must be a number") from None
    if rate < 0:
        raise ValueError("ratelimit_kib must be >= 0")
    out_dir = security.resolve_output_dir(data.get("output_dir"), app)
    start_at = None
    if data.get("start_at"):
        start_at = parse_datetime(str(data["start_at"]))
        if start_at is None:
            raise ValueError("invalid start_at")
        if timezone.is_naive(start_at):
            start_at = timezone.make_aware(start_at, dt_tz.utc)

    created, errors = [], []
    with transaction.atomic():
        active = Download.objects.filter(owner=request.user, status__in=Download.ACTIVE).count()
        base = Download.next_position()
        for i, url in enumerate(urls):
            if app.per_user_queue_cap and active + len(created) >= app.per_user_queue_cap:
                errors.append({"url": url, "error": "queue limit reached"})
                continue
            try:
                security.validate_url(url, guard=app.ssrf_guard)
            except security.ValidationError as exc:
                errors.append({"url": url, "error": str(exc)})
                continue
            d = Download.objects.create(
                owner=request.user, url=url, format=fmt, backend=app.backend, output_dir=out_dir,
                ratelimit_kib=rate, max_retries=max_retries, position=base + i, start_at=start_at)
            created.append(d.pk)
    return JsonResponse({"created": created, "errors": errors}, status=201 if created else 400)


_SIMPLE = {"pause": controls.pause, "resume": controls.resume, "cancel": controls.cancel,
           "retry": controls.retry_now}


@api
@require_POST
def action(request, pk, name):
    dl = _get(request, pk)
    if name in _SIMPLE:
        _SIMPLE[name](dl.pk)
    elif name == "retries":
        controls.set_retries(dl.pk, _body(request).get("value"))
    elif name == "move":
        controls.move(dl.pk, _body(request).get("direction"))
    elif name == "remove":
        controls.remove(dl.pk, delete_files=bool(_body(request).get("delete_files")))
    else:
        raise Http404
    return JsonResponse({"ok": True})


@api
@require_POST
def purge(request):
    data = _body(request)
    days = data.get("older_than_days")
    if days in (None, ""):
        days = None
    else:
        days = retry_int(days, "older_than_days")
    statuses = data.get("statuses") or None
    if statuses is not None and not isinstance(statuses, list):
        raise ValueError("statuses must be a list")
    user = None if (request.user.is_staff and data.get("all")) else request.user
    result = purge_service.purge(
        user=user, statuses=statuses, older_than_days=days,
        delete_files=bool(data.get("delete_files")), dry_run=bool(data.get("dry_run")))
    return JsonResponse(result)


def retry_int(value, name):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError("%s must be a number" % name) from None
    if n < 0:
        raise ValueError("%s must be >= 0" % name)
    return n


@api
@require_GET
def file(request, pk):
    dl = _get(request, pk)
    if dl.status != Download.COMPLETED or not dl.filepath:
        raise Http404
    app = AppSettings.get_solo()
    real = os.path.realpath(dl.filepath)
    roots = [str(r) for r in security.allowed_dirs(app)]
    if not any(real == r or real.startswith(r.rstrip(os.sep) + os.sep) for r in roots) or not os.path.isfile(real):
        raise Http404
    return FileResponse(open(real, "rb"), as_attachment=True, filename=os.path.basename(real))
