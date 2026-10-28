"""Live (non-durable) metrics in the Django cache (Redis in production)."""
import time

from django.core.cache import cache

LIVE_TTL = 120
SERIES_LEN = 150


def _k(dl_id):
    return "live:%s" % dl_id


def set_live(dl_id, downloaded, total, speed, eta):
    cache.set(_k(dl_id), {"d": downloaded, "t": total, "s": speed or 0, "e": eta, "ts": time.time()}, LIVE_TTL)


def clear_live(dl_id):
    cache.delete(_k(dl_id))


def get_live_many(ids):
    found = cache.get_many([_k(i) for i in ids])
    return {i: found.get(_k(i)) for i in ids}


def push_series(name, value, now=None):
    key = "series:%s" % name
    series = cache.get(key) or []
    series.append([round(now or time.time(), 1), int(value)])
    cache.set(key, series[-SERIES_LEN:], 3600)


def get_series(name):
    return cache.get("series:%s" % name) or []
