"""Runs one download: spawns core/runner.py, relays progress, honours pause/cancel, and decides
what happens on failure (retry with backoff / fail)."""
import json
import logging
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..models import AppSettings, Download
from . import cleanup, formats, live, process, retry

log = logging.getLogger(__name__)

_EOF = object()
_ENV_KEEP = (
    "PATH", "HOME", "USERPROFILE", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "SYSTEMDRIVE",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "DENO_DIR", "DENO_INSTALL",
)


def runner_env():
    """Minimal environment: the runner never sees DJANGO_SECRET_KEY, DATABASE_URL, ..."""
    env = {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def build_spec(dl, app):
    fmt, postprocessors = formats.resolve(dl.format)
    rate = (dl.ratelimit_kib or app.default_ratelimit_kib) * 1024
    cookies = app.cookies_file if app.cookies_file and os.path.isfile(app.cookies_file) else None
    return {
        "module": dl.backend,
        "url": dl.url,
        "format": fmt,
        "postprocessors": postprocessors,
        # '%' in the directory must not be read as a template field
        "outtmpl": os.path.join(dl.output_dir.replace("%", "%%"), "%(title).150B [%(id)s].%(ext)s"),
        "ratelimit": rate or None,
        "socket_timeout": app.socket_timeout,
        "cookiefile": cookies,
        "inner_retries": settings.YTP_INNER_RETRIES,
        "js_runtime": app.js_runtime,
    }


def _write_spec(dl, spec):
    d = tempfile.mkdtemp(prefix="ytp-")
    path = os.path.join(d, process.spec_name(dl.pk, dl.run_token))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(spec, fh)
    return d, path


def _pump_stdout(proc, q):
    try:
        for line in proc.stdout:
            try:
                q.put(json.loads(line))
            except ValueError:
                pass
    finally:
        q.put(_EOF)


def _pump_stderr(proc, tail):
    total = 0
    for line in proc.stderr:
        line = line.rstrip()
        if line and total < 20000:
            tail.append(line[:500])
            total += len(line)


def run_download(dl_id, token):
    """Execute one claimed download. Returns the outcome string (used by the tests)."""
    now = timezone.now()
    if not Download.objects.filter(pk=dl_id, status=Download.RUNNING, run_token=token).update(heartbeat=now):
        return "stale"  # reaped/requeued/cancelled meanwhile; a newer claim owns it
    dl = Download.objects.get(pk=dl_id)
    app = AppSettings.get_solo()

    if dl.ctl:  # paused/cancelled between claim and start
        return _finalize(dl_id, token, app, done=None, error=None, rc=None, stderr_tail=[], partials=set(), title="")

    tmpdir, spec_path = _write_spec(dl, build_spec(dl, app))
    q = queue.Queue()
    tail = deque(maxlen=30)
    popen_kw = {"start_new_session": True} if process.IS_POSIX else {}
    try:
        proc = subprocess.Popen(
            [settings.YTP_RUNNER_PYTHON, str(process.RUNNER_PATH), spec_path],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            env=runner_env(), cwd=tmpdir, **popen_kw)
    except OSError as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return _finalize(dl_id, token, app, done=None, rc=None, stderr_tail=[], partials=set(), title="",
                         error={"message": "cannot start runner: %s" % exc})

    Download.objects.filter(pk=dl_id, run_token=token).update(runner_pid=proc.pid)
    threading.Thread(target=_pump_stdout, args=(proc, q), daemon=True).start()
    threading.Thread(target=_pump_stderr, args=(proc, tail), daemon=True).start()

    done = error = None
    partials, title = set(), ""
    downloaded, total = 0, None
    killed = False
    last_ctl = last_ckpt = time.monotonic()
    try:
        while True:
            try:
                msg = q.get(timeout=0.5)
            except queue.Empty:
                msg = None
            if msg is _EOF:
                break
            if msg:
                kind = msg.get("type")
                if kind == "progress":
                    downloaded = msg.get("downloaded") or downloaded
                    total = msg.get("total") or total
                    for key in ("filename",):
                        if msg.get(key):
                            partials.add(msg[key])
                    live.set_live(dl_id, downloaded, total, msg.get("speed"), msg.get("eta"))
                elif kind == "info":
                    title = (msg.get("title") or "")[:500]
                    Download.objects.filter(pk=dl_id, run_token=token).update(title=title)
                elif kind == "done":
                    done = msg
                elif kind == "error":
                    error = msg
            now_m = time.monotonic()
            if now_m - last_ctl >= 1.0 and not killed:
                last_ctl = now_m
                ctl = Download.objects.filter(pk=dl_id, run_token=token).values_list("ctl", flat=True).first()
                if ctl is None:  # row no longer ours (reaped / deleted)
                    killed = True
                    process.terminate(proc)
                elif ctl:
                    killed = True
                    process.terminate(proc)
            if now_m - last_ckpt >= settings.YTP_CHECKPOINT_INTERVAL:
                last_ckpt = now_m
                n = Download.objects.filter(pk=dl_id, status=Download.RUNNING, run_token=token).update(
                    heartbeat=timezone.now(), downloaded_bytes=downloaded, total_bytes=total,
                    partial_files=sorted(partials))
                if not n and not killed:
                    killed = True
                    process.terminate(proc)
    finally:
        try:
            rc = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.terminate(proc, grace=1.0)
            proc.kill()
            rc = proc.wait()
        shutil.rmtree(tmpdir, ignore_errors=True)
        live.clear_live(dl_id)
    return _finalize(dl_id, token, app, done=done, error=error, rc=rc,
                     stderr_tail=list(tail), partials=partials, title=title,
                     downloaded=downloaded, total=total)


def _finalize(dl_id, token, app, *, done, error, rc, stderr_tail, partials, title,
              downloaded=0, total=None):
    now = timezone.now()
    post_cancel = None
    with transaction.atomic():
        dl = Download.objects.select_for_update().filter(pk=dl_id, status=Download.RUNNING, run_token=token).first()
        if dl is None:
            return "lost"
        dl.run_token = None
        dl.runner_pid = None
        dl.heartbeat = None
        dl.partial_files = sorted(set(dl.partial_files or []) | set(partials))
        if downloaded:
            dl.downloaded_bytes = downloaded
        if total:
            dl.total_bytes = total
        if title and not dl.title:
            dl.title = title
        ctl, dl.ctl = dl.ctl, ""

        if done is not None:
            fp = done.get("filepath") or ""
            dl.status = Download.COMPLETED
            dl.filepath = fp
            try:
                dl.filesize = os.path.getsize(fp) if fp else None
            except OSError:
                dl.filesize = None
            if dl.filesize:
                dl.downloaded_bytes = dl.total_bytes = dl.filesize
            if done.get("title"):
                dl.title = done["title"][:500]
            dl.last_error = ""
            dl.next_retry_at = None
            dl.finished_at = now
            outcome = "completed"
        elif ctl == Download.CTL_CANCEL:
            dl.status = Download.CANCELLED
            dl.finished_at = now
            post_cancel = dl
            outcome = "cancelled"
        elif ctl == Download.CTL_PAUSE:
            dl.status = Download.PAUSED
            outcome = "paused"
        else:
            msg = (error or {}).get("message") or ""
            if not msg:
                if rc is not None and rc < 0:
                    msg = "runner terminated by signal %d" % -rc
                elif stderr_tail:
                    msg = " | ".join(stderr_tail[-3:])
                else:
                    msg = "runner exited with code %s without a result" % rc
            msg = msg.replace("\r", " ")
            if msg.startswith("ERROR: "):
                msg = msg[7:]
            dl.last_error = msg[:2000]
            permanent = bool((error or {}).get("permanent")) or retry.is_permanent(msg)
            if permanent and not app.retry_permanent:
                dl.status = Download.FAILED
                dl.finished_at = now
                outcome = "failed-permanent"
            elif retry.should_retry(dl.max_retries, dl.attempt):
                dl.attempt += 1
                delay = retry.backoff_delay(dl.attempt, app.retry_delay_base, app.retry_delay_max)
                dl.status = Download.RETRYING
                dl.next_retry_at = now + timedelta(seconds=delay)
                outcome = "retrying"
            else:
                dl.status = Download.FAILED
                dl.finished_at = now
                outcome = "failed"
        dl.save()
    if post_cancel is not None:
        cleanup.remove_partials(post_cancel, app)
    return outcome
