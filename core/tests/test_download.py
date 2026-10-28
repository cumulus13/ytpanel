import os
import subprocess
import sys
import threading
import time
import unittest
import uuid
from datetime import timedelta

from django.db import connection, connections
from django.utils import timezone

from core.models import Download
from core.services import controls, process, scheduler, worker

from .base import DownloadTestCase


class CompletionTests(DownloadTestCase):
    def test_completes_with_correct_content(self):
        sha = self.server.add("/ok.mp4", size=2 * 1024 * 1024, seed=11)
        dl = self.make("/ok.mp4")
        self.assertTrue(self.drive(lambda: self.status(dl) == Download.COMPLETED))
        d = self.reload(dl)
        self.assertEqual(self.sha(d.filepath), sha)
        self.assertEqual(d.filesize, 2 * 1024 * 1024)
        self.assertEqual(d.title, "ok")
        self.assertEqual(d.attempt, 0)
        self.assertEqual(d.last_error, "")
        self.assertIsNone(d.run_token)
        self.assertTrue(d.finished_at)
        self.assertFalse(any(f.endswith(".part") for f in os.listdir(self.root)))

    def test_percent_in_directory_is_not_a_template(self):
        sha = self.server.add("/p.mp4", size=300_000, seed=12)
        out = os.path.join(self.root, "100%(title)s")
        os.makedirs(out)
        dl = self.make("/p.mp4", output_dir=out)
        self.assertTrue(self.drive(lambda: self.status(dl) == Download.COMPLETED))
        d = self.reload(dl)
        self.assertEqual(os.path.dirname(d.filepath), out)
        self.assertEqual(self.sha(d.filepath), sha)

    def test_rate_limit_is_applied(self):
        self.server.add("/slow.mp4", size=400_000, seed=13)
        dl = self.make("/slow.mp4", ratelimit_kib=100)  # 100 KiB/s -> >= ~2.5s for 400 kB
        t0 = time.monotonic()
        self.assertTrue(self.drive(lambda: self.status(dl) == Download.COMPLETED))
        self.assertGreater(time.monotonic() - t0, 2.5)

    def test_stale_token_does_not_run(self):
        self.server.add("/s.mp4", size=100_000)
        dl = self.make("/s.mp4")
        self.assertEqual(worker.run_download(dl.pk, uuid.uuid4()), "stale")      # not claimed
        scheduler.tick(dispatch=lambda *a: None)
        self.assertEqual(worker.run_download(dl.pk, uuid.uuid4()), "stale")      # wrong token
        self.assertEqual(self.status(dl), Download.RUNNING)                      # untouched


class RetryTests(DownloadTestCase):
    def test_permanent_error_fails_at_once_even_with_infinite_retries(self):
        dl = self.make("/missing.mp4", max_retries=-1)
        self.assertTrue(self.drive(lambda: self.status(dl) == Download.FAILED))
        d = self.reload(dl)
        self.assertEqual(d.attempt, 0)
        self.assertIn("404", d.last_error)
        self.assertFalse(d.last_error.startswith("ERROR"))

    def test_permanent_errors_retried_when_setting_enabled(self):
        self.app.retry_permanent = True
        self.app.save()
        dl = self.make("/missing.mp4", max_retries=2)
        self.assertTrue(self.drive(lambda: self.status(dl) == Download.FAILED, timeout=120))
        d = self.reload(dl)
        self.assertEqual(d.attempt, 2)  # 1 initial try + exactly 2 retries

    def test_finite_retries_then_failed(self):
        self.server.add("/down.mp4", size=100_000, fail_first=10 ** 6)
        dl = self.make("/down.mp4", max_retries=2)
        seen = set()
        ok = self.drive(lambda: (seen.add(self.status(dl)), self.status(dl) == Download.FAILED)[1], timeout=120)
        self.assertTrue(ok)
        d = self.reload(dl)
        self.assertEqual(d.attempt, 2)
        self.assertIn("503", d.last_error)
        self.assertIn(Download.RETRYING, seen)

    def test_zero_retries_means_single_try(self):
        self.server.add("/z.mp4", size=100_000, fail_first=10 ** 6)
        dl = self.make("/z.mp4", max_retries=0)
        self.assertTrue(self.drive(lambda: self.status(dl) == Download.FAILED))
        self.assertEqual(self.reload(dl).attempt, 0)

    def test_transient_failure_recovers_and_content_is_intact(self):
        sha = self.server.add("/flaky.mp4", size=1024 * 1024, seed=21, fail_first=4)
        dl = self.make("/flaky.mp4", max_retries=10)
        self.assertTrue(self.drive(lambda: self.status(dl) == Download.COMPLETED, timeout=120))
        d = self.reload(dl)
        self.assertGreaterEqual(d.attempt, 1)
        self.assertEqual(self.sha(d.filepath), sha)
        self.assertEqual(d.last_error, "")  # cleared on success

    def test_infinite_retries_keep_going_until_it_works(self):
        sha = self.server.add("/inf.mp4", size=500_000, seed=22, fail_first=10 ** 6)
        dl = self.make("/inf.mp4", max_retries=-1)
        self.assertTrue(self.drive(lambda: self.reload(dl).attempt >= 4, timeout=150))
        self.assertIn(self.status(dl), (Download.RETRYING, Download.RUNNING))   # never gave up
        self.srv.fail_first["/inf.mp4"] = 0                                       # the server recovers
        self.assertTrue(self.drive(lambda: self.status(dl) == Download.COMPLETED, timeout=60))
        self.assertEqual(self.sha(self.reload(dl).filepath), sha)

    def test_backoff_is_honoured(self):
        self.app.retry_delay_base = 30
        self.app.retry_delay_max = 60
        self.app.save()
        self.server.add("/b.mp4", size=100_000, fail_first=10 ** 6)
        dl = self.make("/b.mp4", max_retries=3)
        self.assertTrue(self.drive(lambda: self.status(dl) == Download.RETRYING))
        d = self.reload(dl)
        delta = (d.next_retry_at - timezone.now()).total_seconds()
        self.assertTrue(25 < delta <= 34, delta)
        for _ in range(5):                      # not due yet: ticks must not start it
            scheduler.tick(dispatch=self.dispatch)
            time.sleep(0.2)
        self.assertEqual(self.status(dl), Download.RETRYING)
        controls.retry_now(dl.pk)               # "retry now" makes it due
        self.assertTrue(self.drive(lambda: self.reload(dl).attempt >= 2, timeout=60))

    def test_midstream_drop_resumes_with_range(self):
        sha = self.server.add("/drop.mp4", size=3 * 1024 * 1024, seed=23, drop_after=1024 * 1024, drop_hits=2)
        dl = self.make("/drop.mp4", max_retries=5)
        self.assertTrue(self.drive(lambda: self.status(dl) == Download.COMPLETED, timeout=120))
        d = self.reload(dl)
        self.assertGreaterEqual(d.attempt, 1)
        self.assertEqual(self.sha(d.filepath), sha)
        self.assertTrue(any(p == "/drop.mp4" for p, _ in self.srv.ranges), "retry should resume via Range")

    def test_set_retries_changes_policy_midway(self):
        self.server.add("/c.mp4", size=100_000, fail_first=10 ** 6)
        dl = self.make("/c.mp4", max_retries=1)
        self.assertTrue(self.drive(lambda: self.status(dl) == Download.RETRYING))
        controls.set_retries(dl.pk, "always")
        self.assertEqual(self.reload(dl).max_retries, -1)
        with self.assertRaises(controls.ControlError):
            controls.set_retries(dl.pk, "nope")


class ControlTests(DownloadTestCase):
    def _start_slow(self, path="/slow.mp4", size=6 * 1024 * 1024, seed=31, **kw):
        sha = self.server.add(path, size=size, seed=seed, chunk_delay=0.05)
        dl = self.make(path, **kw)
        self.assertTrue(self.drive(lambda: self.reload(dl).downloaded_bytes > 0 or
                                   any(f.endswith(".part") for f in os.listdir(self.root)), timeout=60))
        self.assertTrue(self.wait(lambda: self.status(dl) == Download.RUNNING))
        return dl, sha

    def test_pause_then_resume_completes_via_range(self):
        dl, sha = self._start_slow()
        controls.pause(dl.pk)
        self.assertTrue(self.wait(lambda: self.status(dl) == Download.PAUSED, timeout=30))
        d = self.reload(dl)
        self.assertIsNone(d.run_token)
        self.assertEqual(d.ctl, "")
        self.assertTrue(any(f.endswith(".part") for f in os.listdir(self.root)), "partial kept for resume")
        for _ in range(4):  # paused must stay paused
            scheduler.tick(dispatch=self.dispatch)
            time.sleep(0.2)
        self.assertEqual(self.status(dl), Download.PAUSED)
        controls.resume(dl.pk)
        self.assertTrue(self.drive(lambda: self.status(dl) == Download.COMPLETED, timeout=90))
        self.assertEqual(self.sha(self.reload(dl).filepath), sha)
        self.assertTrue(any(p == "/slow.mp4" for p, _ in self.srv.ranges))

    def test_cancel_running_removes_partials(self):
        dl, _ = self._start_slow(path="/cancel.mp4", seed=32)
        controls.cancel(dl.pk)
        self.assertTrue(self.wait(lambda: self.status(dl) == Download.CANCELLED, timeout=30))
        self.assertTrue(self.wait(lambda: not any(".part" in f or f.endswith(".ytdl") for f in os.listdir(self.root))))
        self.assertTrue(self.reload(dl).finished_at)
        self.assertFalse([f for f in os.listdir(self.root) if f.endswith(".mp4")])

    def test_cancel_before_start_and_paused_queued(self):
        self.server.add("/q.mp4", size=100_000)
        a, b = self.make("/q.mp4"), self.make("/q.mp4")
        controls.pause(a.pk)
        self.assertEqual(self.status(a), Download.PAUSED)
        controls.cancel(a.pk)
        controls.cancel(b.pk)
        self.assertEqual({self.status(a), self.status(b)}, {Download.CANCELLED})
        scheduler.tick(dispatch=lambda *x: self.fail("cancelled items must never be dispatched"))
        with self.assertRaises(controls.ControlError):
            controls.cancel(a.pk)
        with self.assertRaises(controls.ControlError):
            controls.pause(a.pk)

    def test_retry_after_failure_resets_budget(self):
        dl = self.make("/gone.mp4")
        self.assertTrue(self.drive(lambda: self.status(dl) == Download.FAILED))
        self.server.add("/gone.mp4", size=100_000)   # now it exists
        controls.retry_now(dl.pk)
        d = self.reload(dl)
        self.assertEqual((d.status, d.attempt, d.last_error), (Download.QUEUED, 0, ""))
        self.assertTrue(self.drive(lambda: self.status(dl) == Download.COMPLETED))

    def test_pause_a_retrying_download(self):
        self.app.retry_delay_base = 60
        self.app.retry_delay_max = 60
        self.app.save()
        self.server.add("/r.mp4", size=100_000, fail_first=10 ** 6)
        dl = self.make("/r.mp4", max_retries=3)
        self.assertTrue(self.drive(lambda: self.status(dl) == Download.RETRYING))
        controls.pause(dl.pk)
        d = self.reload(dl)
        self.assertEqual((d.status, d.next_retry_at), (Download.PAUSED, None))
        controls.resume(dl.pk)
        self.assertEqual(self.status(dl), Download.QUEUED)

    def test_remove_rules(self):
        self.server.add("/m.mp4", size=100_000)
        dl = self.make("/m.mp4")
        self.assertTrue(self.drive(lambda: self.status(dl) == Download.COMPLETED))
        path = self.reload(dl).filepath
        controls.remove(dl.pk, delete_files=False)
        self.assertTrue(os.path.exists(path))
        self.assertFalse(Download.objects.filter(pk=dl.pk).exists())
        run = self.make("/m.mp4")
        Download.objects.filter(pk=run.pk).update(status=Download.RUNNING)
        with self.assertRaises(controls.ControlError):
            controls.remove(run.pk)

    def test_remove_with_files(self):
        self.server.add("/m2.mp4", size=100_000)
        dl = self.make("/m2.mp4")
        self.assertTrue(self.drive(lambda: self.status(dl) == Download.COMPLETED))
        path = self.reload(dl).filepath
        controls.remove(dl.pk, delete_files=True)
        self.assertFalse(os.path.exists(path))

    def test_move_reorders_only_queued_of_same_owner(self):
        from django.contrib.auth import get_user_model
        bob = get_user_model().objects.create_user("bob", password="pw-bob-12345")
        a, b, c = (self.make("/x.mp4") for _ in range(3))
        other = self.make("/x.mp4", owner=bob)
        order = lambda: [d.pk for d in Download.objects.filter(owner=self.user, status="queued").order_by("position", "id")]
        self.assertEqual(order(), [a.pk, b.pk, c.pk])
        controls.move(c.pk, "top")
        self.assertEqual(order(), [c.pk, a.pk, b.pk])
        controls.move(c.pk, "down")
        self.assertEqual(order(), [a.pk, c.pk, b.pk])
        controls.move(a.pk, "bottom")
        self.assertEqual(order(), [c.pk, b.pk, a.pk])
        controls.move(a.pk, "up")
        self.assertEqual(order(), [c.pk, a.pk, b.pk])
        self.assertEqual(self.reload(other).position, other.position)
        with self.assertRaises(controls.ControlError):
            controls.move(a.pk, "sideways")


class SchedulerTests(DownloadTestCase):
    def test_never_exceeds_max_simultaneous_and_all_finish(self):
        self.app.max_simultaneous = 2
        self.app.save()
        shas = {}
        dls = []
        for i in range(5):
            shas[i] = self.server.add("/m%d.mp4" % i, size=600_000, seed=40 + i, chunk_delay=0.03)
            dls.append(self.make("/m%d.mp4" % i))
        peak = [0]

        def cond():
            peak[0] = max(peak[0], Download.objects.filter(status=Download.RUNNING).count())
            return all(self.status(d) == Download.COMPLETED for d in dls)

        self.assertTrue(self.drive(cond, timeout=150, step=0.05))
        self.assertEqual(peak[0], 2)
        for i, d in enumerate(dls):
            self.assertEqual(self.sha(self.reload(d).filepath), shas[i])

    def test_fifo_order_and_start_at(self):
        self.app.max_simultaneous = 1
        self.app.save()
        self.server.add("/f.mp4", size=50_000)
        a = self.make("/f.mp4")
        later = self.make("/f.mp4", start_at=timezone.now() + timedelta(hours=1))
        b = self.make("/f.mp4")
        claimed = scheduler.tick(dispatch=lambda *x: None)
        self.assertEqual(claimed, [a.pk])
        Download.objects.filter(pk=a.pk).update(status=Download.COMPLETED)
        claimed = scheduler.tick(dispatch=lambda *x: None)
        self.assertEqual(claimed, [b.pk])            # the future start_at item is skipped, not blocking
        self.assertEqual(self.status(later), Download.QUEUED)

    @unittest.skipUnless(connection.vendor == "postgresql", "row locking needs PostgreSQL")
    def test_concurrent_ticks_cannot_exceed_limit(self):
        self.app.max_simultaneous = 3
        self.app.save()
        for _ in range(30):
            self.make("/x.mp4")
        barrier = threading.Barrier(8)
        errors = []

        def go():
            try:
                barrier.wait()
                for _ in range(5):
                    scheduler.tick(dispatch=lambda *a: None)
            except Exception as exc:  # noqa
                errors.append(exc)
            finally:
                connections.close_all()

        ts = [threading.Thread(target=go) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])
        self.assertEqual(Download.objects.filter(status=Download.RUNNING).count(), 3)

    def test_dispatch_failure_gives_slot_back(self):
        self.server.add("/d.mp4", size=50_000)
        dl = self.make("/d.mp4")

        def boom(*a):
            raise RuntimeError("broker down")

        scheduler.tick(dispatch=boom)
        d = self.reload(dl)
        self.assertEqual((d.status, d.run_token), (Download.QUEUED, None))


class ReaperTests(DownloadTestCase):
    def _stale(self, dl, **extra):
        Download.objects.filter(pk=dl.pk).update(
            status=Download.RUNNING, run_token=uuid.uuid4(), runner_pid=None,
            heartbeat=timezone.now() - timedelta(seconds=3600), **extra)

    def test_dead_worker_requeued_without_burning_a_retry(self):
        dl = self.make("/r.mp4", attempt=1)
        self._stale(dl)
        self.assertEqual(scheduler.reap(), [dl.pk])
        d = self.reload(dl)
        self.assertEqual((d.status, d.attempt, d.run_token), (Download.QUEUED, 1, None))

    def test_pending_ctl_is_honoured(self):
        a, b = self.make("/r.mp4"), self.make("/r.mp4")
        self._stale(a, ctl="cancel")
        self._stale(b, ctl="pause")
        scheduler.reap()
        self.assertEqual(self.status(a), Download.CANCELLED)
        self.assertEqual(self.status(b), Download.PAUSED)

    def test_fresh_heartbeat_is_left_alone(self):
        dl = self.make("/r.mp4")
        Download.objects.filter(pk=dl.pk).update(status=Download.RUNNING, run_token=uuid.uuid4(),
                                                 heartbeat=timezone.now())
        self.assertEqual(scheduler.reap(), [])

    @unittest.skipUnless(os.path.isdir("/proc/self"), "needs /proc")
    def test_orphan_runner_killed_only_if_it_is_ours(self):
        dl = self.make("/r.mp4")
        code = "import time; time.sleep(120)"
        ours = subprocess.Popen([sys.executable, "-c", code, "runner.py", "ytp-%d-abcd1234.json" % dl.pk],
                                start_new_session=True)
        stranger = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
        try:
            self.assertTrue(process.is_our_runner(ours.pid, dl.pk))
            self.assertFalse(process.is_our_runner(stranger.pid, dl.pk))
            self.assertFalse(process.is_our_runner(ours.pid, dl.pk + 1))
            self._stale(dl)
            Download.objects.filter(pk=dl.pk).update(runner_pid=stranger.pid)
            scheduler.reap()
            time.sleep(0.5)
            self.assertIsNone(stranger.poll(), "a foreign pid must never be killed")
            self._stale(dl)
            Download.objects.filter(pk=dl.pk).update(runner_pid=ours.pid)
            scheduler.reap()
            self.assertTrue(self.wait(lambda: ours.poll() is not None, timeout=10), "our orphan must be killed")
        finally:
            for p in (ours, stranger):
                if p.poll() is None:
                    p.kill()
                p.wait()


class LiveMetricsTests(DownloadTestCase):
    def test_live_speed_and_series_while_running(self):
        from core.services import live
        self.server.add("/live.mp4", size=3 * 1024 * 1024, seed=51, chunk_delay=0.05)
        dl = self.make("/live.mp4")
        self.assertTrue(self.drive(lambda: (live.get_live_many([dl.pk])[dl.pk] or {}).get("s", 0) > 0, timeout=60))
        scheduler.record_speed()
        self.assertGreater(live.get_series("total")[-1][1], 0)
        self.assertGreater(live.get_series("user:%s" % self.user.pk)[-1][1], 0)
        self.assertTrue(self.drive(lambda: self.status(dl) == Download.COMPLETED, timeout=60))
        self.assertIsNone(live.get_live_many([dl.pk])[dl.pk])    # cleared on finish
        scheduler.record_speed()
        self.assertEqual(live.get_series("total")[-1][1], 0)
