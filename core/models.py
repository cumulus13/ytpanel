from django.conf import settings as dj_settings
from django.db import models
from django.db.models import Max

INFINITE = -1  # max_retries value meaning "retry forever"


class AppSettings(models.Model):
    """Singleton row (pk=1); everything configurable at runtime lives here."""

    BACKENDS = [("yt_dlp", "yt-dlp"), ("youtube_dl", "youtube-dl")]
    JS_RUNTIMES = [("deno", "deno"), ("node", "node"), ("bun", "bun"), ("quickjs", "quickjs")]

    max_simultaneous = models.PositiveSmallIntegerField(default=3)
    per_user_queue_cap = models.PositiveIntegerField(default=200, help_text="0 = unlimited")

    backend = models.CharField(max_length=20, choices=BACKENDS, default="yt_dlp")
    js_runtime = models.CharField(max_length=10, choices=JS_RUNTIMES, default="deno",
                                  help_text="JS runtime yt-dlp uses for YouTube (must be installed)")
    default_format = models.CharField(max_length=200, default="best")
    default_ratelimit_kib = models.PositiveIntegerField(default=0, help_text="KiB/s per download, 0 = unlimited")
    socket_timeout = models.PositiveSmallIntegerField(default=30)
    cookies_file = models.CharField(max_length=500, blank=True,
                                    help_text="Optional Netscape cookies.txt readable by the worker")

    default_retries = models.IntegerField(default=5, help_text="-1 = retry forever")
    retry_delay_base = models.PositiveIntegerField(default=10, help_text="seconds; doubles each attempt")
    retry_delay_max = models.PositiveIntegerField(default=900, help_text="seconds; backoff cap")
    retry_permanent = models.BooleanField(
        default=False,
        help_text="Also retry errors that look permanent (private/removed/unsupported/404)")

    allowed_dirs = models.TextField(blank=True, help_text="One absolute directory per line; empty = download root")
    ssrf_guard = models.BooleanField(default=True)

    retention_days = models.PositiveIntegerField(default=0, help_text="Auto-purge finished entries older than N days; 0 = off")
    retention_delete_files = models.BooleanField(default=False, help_text="Auto-purge also deletes completed files")

    class Meta:
        verbose_name_plural = "app settings"

    def __str__(self):
        return "settings"

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class Download(models.Model):
    QUEUED, RUNNING, RETRYING, PAUSED = "queued", "running", "retrying", "paused"
    COMPLETED, FAILED, CANCELLED = "completed", "failed", "cancelled"
    STATUS_CHOICES = [(s, s) for s in (QUEUED, RUNNING, RETRYING, PAUSED, COMPLETED, FAILED, CANCELLED)]
    ACTIVE = (QUEUED, RUNNING, RETRYING, PAUSED)
    TERMINAL = (COMPLETED, FAILED, CANCELLED)

    CTL_PAUSE, CTL_CANCEL = "pause", "cancel"

    owner = models.ForeignKey(dj_settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="downloads")
    url = models.CharField(max_length=2048)
    title = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=QUEUED, db_index=True)

    format = models.CharField(max_length=200, default="best")
    backend = models.CharField(max_length=20, default="yt_dlp")
    output_dir = models.CharField(max_length=1000)
    ratelimit_kib = models.PositiveIntegerField(default=0)

    # retry policy / state: attempt = number of retries already made
    max_retries = models.IntegerField(default=5)
    attempt = models.PositiveIntegerField(default=0)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    position = models.BigIntegerField(default=0)
    start_at = models.DateTimeField(null=True, blank=True)

    # worker coordination
    ctl = models.CharField(max_length=10, blank=True, default="")
    run_token = models.UUIDField(null=True, blank=True)
    runner_pid = models.IntegerField(null=True, blank=True)
    heartbeat = models.DateTimeField(null=True, blank=True)

    # progress (live values are in the cache; these are checkpoints)
    downloaded_bytes = models.BigIntegerField(default=0)
    total_bytes = models.BigIntegerField(null=True, blank=True)
    filepath = models.CharField(max_length=4096, blank=True)
    filesize = models.BigIntegerField(null=True, blank=True)
    partial_files = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["status", "position"])]
        ordering = ["position", "id"]

    def __str__(self):
        return "#%s %s [%s]" % (self.pk, self.title or self.url, self.status)

    @staticmethod
    def next_position():
        return (Download.objects.aggregate(m=Max("position"))["m"] or 0) + 1
