"""Removal of partial / final files belonging to a download (always confined to allowed dirs)."""
import glob
import re

from .security import safe_unlink

_INTERMEDIATE = re.compile(r"\.f[\w-]+\.\w+$")  # yt-dlp per-stream file: "<name>.f137.mp4"


def partial_paths(dl):
    out = []
    for f in dl.partial_files or []:
        out += [f + ".part", f + ".ytdl"]
        out += glob.glob(glob.escape(f) + ".part-Frag*")
        if _INTERMEDIATE.search(f):
            out.append(f)
    return out


def remove_partials(dl, app_settings):
    n = 0
    for p in partial_paths(dl):
        if safe_unlink(p, app_settings):
            n += 1
    return n


def remove_final(dl, app_settings):
    return safe_unlink(dl.filepath, app_settings) if dl.filepath else False
