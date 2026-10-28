import os
import shutil
import tempfile
import threading
import time

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connections
from django.test import TransactionTestCase, override_settings

from core.models import AppSettings, Download
from core.services import scheduler, worker

from .fixture_server import FixtureServer

User = get_user_model()


class DownloadTestCase(TransactionTestCase):
    """Real yt-dlp subprocesses against a local HTTP server, real database, real cache."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = FixtureServer().start()
        cls.root = tempfile.mkdtemp(prefix="ytp-test-")
        cls._override = override_settings(YTP_DOWNLOAD_ROOT=cls.root, YTP_INNER_RETRIES=0,
                                          YTP_CHECKPOINT_INTERVAL=0.5)
        cls._override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._override.disable()
        cls.server.stop()
        shutil.rmtree(cls.root, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        cache.clear()
        self.threads = []
        self.srv = self.server.httpd
        for d in (self.srv.files, self.srv.hits, self.srv.fail_first, self.srv.chunk_delay,
                  self.srv.drop_after, self.srv.drop_hits, self.srv.body_hits):
            d.clear()
        self.srv.no_range.clear()
        self.srv.ranges.clear()
        shutil.rmtree(self.root, ignore_errors=True)
        os.makedirs(self.root)
        self.app = AppSettings.get_solo()
        self.app.ssrf_guard = False
        self.app.retry_delay_base = 1
        self.app.retry_delay_max = 2
        self.app.max_simultaneous = 3
        self.app.save()
        self.user = User.objects.create_user("alice", password="pw-alice-12345")

    def tearDown(self):
        for t in self.threads:
            t.join(timeout=30)
        connections.close_all()

    # -- helpers --
    def make(self, path, **kw):
        kw.setdefault("owner", self.user)
        kw.setdefault("format", "best")
        kw.setdefault("output_dir", self.root)
        kw.setdefault("max_retries", 5)
        kw.setdefault("backend", "yt_dlp")
        kw.setdefault("position", Download.next_position())
        return Download.objects.create(url=self.server.base + path, **kw)

    def _run(self, dl_id, token):
        try:
            worker.run_download(dl_id, token)
        finally:
            connections.close_all()

    def dispatch(self, dl_id, token):
        t = threading.Thread(target=self._run, args=(dl_id, token), daemon=True)
        t.start()
        self.threads.append(t)

    def drive(self, cond, timeout=90, step=0.2):
        """Act as celery beat + workers: tick the scheduler until cond() is true."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            scheduler.tick(dispatch=self.dispatch)
            if cond():
                return True
            time.sleep(step)
        return False

    def wait(self, cond, timeout=60, step=0.1):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return True
            time.sleep(step)
        return False

    def status(self, dl):
        return Download.objects.get(pk=dl.pk).status

    def reload(self, dl):
        return Download.objects.get(pk=dl.pk)

    def sha(self, path):
        import hashlib
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
