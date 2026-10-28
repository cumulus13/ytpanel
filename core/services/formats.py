"""Format presets -> (format selector, postprocessors). Presets never need ffmpeg unless it exists."""
import re
import shutil

PRESETS = ("best", "audio", "1080p", "720p", "480p")
_CUSTOM_RE = re.compile(r"^[A-Za-z0-9_\[\]()<>=!*+/,.\-:&|^$~@ ]{1,200}$")


def validate_format(value):
    value = (value or "").strip()
    if value in PRESETS:
        return value
    if not _CUSTOM_RE.match(value):
        raise ValueError("invalid format selector")
    return value


def has_ffmpeg():
    return shutil.which("ffmpeg") is not None


def resolve(fmt, ffmpeg=None):
    """Return (format_selector, postprocessors) for a preset or custom selector."""
    ffmpeg = has_ffmpeg() if ffmpeg is None else ffmpeg
    if fmt == "best":
        return ("bv*+ba/b" if ffmpeg else "b"), []
    if fmt == "audio":
        if ffmpeg:
            return "ba/b", [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
        return "ba/b", []
    if fmt in ("1080p", "720p", "480p"):
        h = int(fmt[:-1])
        if ffmpeg:
            return "bv*[height<=%d]+ba/b[height<=%d]" % (h, h), []
        return "b[height<=%d]" % h, []
    return fmt, []
