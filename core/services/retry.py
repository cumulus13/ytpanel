"""Retry policy: parsing the user's value, backoff, and permanent-error detection."""
import random

INFINITE = -1
MAX_FINITE = 1_000_000
_INF_WORDS = {"inf", "infinite", "infinity", "always", "forever", "unlimited", "\u221e", "-1"}


def parse_retries(value):
    """Return an int >= 0, or -1 for infinite. Accepts ints, numeric strings and words like
    'inf' / 'always'. Raises ValueError for anything else."""
    if isinstance(value, bool):
        raise ValueError("invalid retries value")
    if isinstance(value, int):
        n = value
    else:
        text = str(value).strip().lower()
        if text in _INF_WORDS:
            return INFINITE
        try:
            n = int(text)
        except ValueError:
            raise ValueError("retries must be a number or 'inf'/'always'") from None
    if n == INFINITE:
        return INFINITE
    if n < 0 or n > MAX_FINITE:
        raise ValueError("retries must be between 0 and %d, or -1 for infinite" % MAX_FINITE)
    return n


def should_retry(max_retries, attempt):
    """attempt = retries already made. max_retries=3 -> 1 initial try + 3 retries."""
    return max_retries == INFINITE or attempt < max_retries


def backoff_delay(attempt, base, cap, rng=random):
    """Delay in seconds before retry number `attempt` (1-based): base * 2^(attempt-1), capped,
    with up to +10% jitter (never above the cap)."""
    if attempt < 1:
        attempt = 1
    delay = min(cap, base * (2 ** min(attempt - 1, 30)))
    return min(cap, delay * (1 + rng.uniform(0, 0.10)))


# Substrings (lower case) of errors that retrying will not fix.
PERMANENT_MARKERS = (
    "unsupported url",
    "is not a valid url",
    "video unavailable",
    "private video",
    "this video is private",
    "has been removed",
    "account associated with this video has been terminated",
    "requested format is not available",
    "playlist urls are not supported",
    "http error 404",
    "http error 410",
)


def is_permanent(message):
    low = (message or "").lower()
    return any(m in low for m in PERMANENT_MARKERS)


def retries_label(max_retries):
    return "\u221e" if max_retries == INFINITE else str(max_retries)
