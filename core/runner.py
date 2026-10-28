"""Standalone download runner. One process per download, started by core.services.worker.

Deliberately has NO Django imports: it only needs the downloader library. It speaks a
JSON-lines protocol on the real stdout (everything the library prints is redirected to
stderr so it can never corrupt the protocol):

    {"type":"info","title":...,"extractor":...}
    {"type":"progress","status":...,"downloaded":int,"total":int|null,"speed":float|null,
     "eta":int|null,"filename":str|null,"tmpfilename":str|null}
    {"type":"done","filepath":str|null,"title":str|null}
    {"type":"error","message":str,"permanent":bool}

Usage: python runner.py <spec.json>
"""
import importlib
import json
import re
import sys
import time

ALLOWED_MODULES = {"yt_dlp", "youtube_dl"}
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _clean(text):
    return _ANSI.sub("", str(text)).strip()


def main(argv):
    proto = sys.stdout
    sys.stdout = sys.stderr  # protect the protocol stream

    def emit(obj):
        proto.write(json.dumps(obj, ensure_ascii=False) + "\n")
        proto.flush()

    try:
        with open(argv[1], "r", encoding="utf-8") as fh:
            spec = json.load(fh)
        module_name = spec["module"]
        if module_name not in ALLOWED_MODULES:
            raise ValueError("backend module not allowed: %r" % module_name)
        mod = importlib.import_module(module_name)
    except Exception as exc:  # bad spec / backend not installed
        emit({"type": "error", "message": "runner setup failed: " + _clean(exc), "permanent": False})
        return 3

    last = [0.0]

    def hook(d):
        status = d.get("status")
        now = time.monotonic()
        if status == "downloading" and now - last[0] < 0.5:
            return
        last[0] = now
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        emit({
            "type": "progress",
            "status": status,
            "downloaded": int(d.get("downloaded_bytes") or 0),
            "total": int(total) if total else None,
            "speed": d.get("speed"),
            "eta": d.get("eta"),
            "filename": d.get("filename"),
            "tmpfilename": d.get("tmpfilename"),
        })

    class Logger:
        # Everything the library logs goes to stderr; the parent keeps the tail for diagnostics.
        def debug(self, msg):
            pass

        def info(self, msg):
            pass

        def warning(self, msg):
            print("WARNING: " + _clean(msg), file=sys.stderr, flush=True)

        def error(self, msg):
            print("ERROR: " + _clean(msg), file=sys.stderr, flush=True)

    opts = {
        "format": spec["format"],
        "outtmpl": {"default": spec["outtmpl"]},
        "noplaylist": True,
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "logger": Logger(),
        "progress_hooks": [hook],
        "windowsfilenames": True,
        "continuedl": True,
        "retries": int(spec.get("inner_retries", 3)),
        "fragment_retries": int(spec.get("inner_retries", 3)),
        "socket_timeout": int(spec.get("socket_timeout", 30)),
    }
    if spec.get("ratelimit"):
        opts["ratelimit"] = int(spec["ratelimit"])
    if spec.get("cookiefile"):
        opts["cookiefile"] = spec["cookiefile"]
    if spec.get("postprocessors"):
        opts["postprocessors"] = spec["postprocessors"]
    if spec.get("js_runtime") and module_name == "yt_dlp":
        opts["js_runtimes"] = {spec["js_runtime"]: {}}

    try:
        with mod.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(spec["url"], download=False)
            if info is None:
                raise RuntimeError("no information extracted")
            if info.get("_type") in ("playlist", "multi_video") or info.get("entries") is not None:
                emit({"type": "error", "permanent": True,
                      "message": "playlist URLs are not supported; add the individual video URLs"})
                return 4
            emit({"type": "info", "title": info.get("title"), "extractor": info.get("extractor_key")})
            result = ydl.process_ie_result(info, download=True)
            filepath = None
            downloads = (result or {}).get("requested_downloads") or []
            if downloads:
                filepath = downloads[0].get("filepath")
            if not filepath and result:
                filepath = result.get("filepath") or result.get("_filename")
            emit({"type": "done", "filepath": filepath, "title": (result or {}).get("title")})
        return 0
    except BaseException as exc:  # noqa: BLE001 - report everything, the parent decides
        emit({"type": "error", "message": _clean(exc) or exc.__class__.__name__, "permanent": False})
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
