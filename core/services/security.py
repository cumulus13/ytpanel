"""URL validation (SSRF guard) and filesystem confinement."""
import ipaddress
import os
import socket
from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings


class ValidationError(ValueError):
    pass


def validate_url(url, guard=True):
    url = (url or "").strip()
    if not url or len(url) > 2048 or any(c.isspace() or ord(c) < 32 for c in url):
        raise ValidationError("invalid URL")
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValidationError("only http(s) URLs are allowed")
    if guard:
        try:
            infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80),
                                       type=socket.SOCK_STREAM)
        except socket.gaierror:
            raise ValidationError("host does not resolve") from None
        for info in infos:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                    or ip.is_multicast or ip.is_unspecified):
                raise ValidationError("URL resolves to a non-public address")
    return url


def allowed_dirs(app_settings):
    lines = [x.strip() for x in (app_settings.allowed_dirs or "").splitlines() if x.strip()]
    if not lines:
        lines = [str(settings.YTP_DOWNLOAD_ROOT)]
    return [Path(os.path.realpath(x)) for x in lines]


def _inside(path, roots):
    return any(path == r or r in path.parents for r in roots)


def resolve_output_dir(value, app_settings):
    """Map user input to an absolute directory inside an allowed root (created if missing).
    Empty -> first root; relative -> relative to first root; absolute -> must be inside a root."""
    roots = allowed_dirs(app_settings)
    value = (value or "").strip()
    if "\0" in value:
        raise ValidationError("invalid directory")
    if not value:
        target = roots[0]
    elif os.path.isabs(value):
        target = Path(os.path.realpath(value))
    else:
        target = Path(os.path.realpath(os.path.join(roots[0], value)))
    if not _inside(target, roots):
        raise ValidationError("directory is outside the allowed download directories")
    target.mkdir(parents=True, exist_ok=True)
    # re-resolve after mkdir to catch symlink tricks
    target = Path(os.path.realpath(target))
    if not _inside(target, roots):
        raise ValidationError("directory is outside the allowed download directories")
    return str(target)


def safe_unlink(path, app_settings):
    """Delete a regular (non-symlink) file only if it lives inside an allowed root."""
    if not path:
        return False
    roots = allowed_dirs(app_settings)
    parent = Path(os.path.realpath(os.path.dirname(path)))
    full = parent / os.path.basename(path)
    if not _inside(full, roots) or os.path.islink(full) or not os.path.isfile(full):
        return False
    try:
        os.unlink(full)
        return True
    except OSError:
        return False
