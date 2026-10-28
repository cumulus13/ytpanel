"""Runner process helpers (POSIX: one process group per download; Windows: plain terminate)."""
import os
import signal
import time
from pathlib import Path

RUNNER_PATH = Path(__file__).resolve().parent.parent / "runner.py"
IS_POSIX = os.name == "posix"


def spec_name(dl_id, token):
    return "ytp-%d-%s.json" % (dl_id, str(token)[:8])


def is_our_runner(pid, dl_id):
    """True only if /proc says `pid` is a runner.py process for this download (Linux only)."""
    if not pid or not os.path.isdir("/proc/self"):
        return False
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            data = fh.read()
    except OSError:
        return False
    return b"runner.py" in data and ("ytp-%d-" % dl_id).encode() in data


def terminate(proc_or_pid, grace=5.0):
    """SIGTERM the whole process group, SIGKILL after `grace` seconds. Accepts a Popen or a pid."""
    pid = getattr(proc_or_pid, "pid", proc_or_pid)
    if not IS_POSIX:
        if hasattr(proc_or_pid, "terminate"):
            proc_or_pid.terminate()
        return
    try:
        pgid = os.getpgid(pid)
    except OSError:
        return
    if pgid == os.getpgrp():  # never signal our own group
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        try:
            if hasattr(proc_or_pid, "poll"):
                if proc_or_pid.poll() is not None:
                    return
            else:
                os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.1)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass
