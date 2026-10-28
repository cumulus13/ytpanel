import os
import tempfile
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.utils import timezone

from core.models import Download
from core.services import purge

from .base import DownloadTestCase


class PurgeTests(DownloadTestCase):
    def _finished(self, path, status, **kw):
        return self.make(path, status=status, finished_at=kw.pop("finished_at", timezone.now()), **kw)

    def _touch(self, name, data=b"x" * 1000):
        p = os.path.join(self.root, name)
        with open(p, "wb") as fh:
            fh.write(data)
        return p

    def test_purges_all_finished_and_cleans_partials_but_keeps_files_by_default(self):
        done_file = self._touch("done.mp4")
        part = self._touch("half.mp4.part")
        ytdl = self._touch("half.mp4.ytdl")
        frag = self._touch("half.mp4.part-Frag3")
        inter = self._touch("v.f137.mp4")
        completed = self._finished("/a", Download.COMPLETED, filepath=done_file)
        failed = self._finished("/b", Download.FAILED, partial_files=[os.path.join(self.root, "half.mp4")])
        cancelled = self._finished("/c", Download.CANCELLED, partial_files=[os.path.join(self.root, "v.f137.mp4")])
        queued = self.make("/d")
        running = self.make("/e", status=Download.RUNNING)
        paused = self.make("/f", status=Download.PAUSED)

        res = purge.purge()
        self.assertEqual(res["entries"], 3)
        self.assertEqual(res["files_deleted"], 0)
        self.assertEqual(res["partials_deleted"], 4)
        self.assertEqual(res["bytes_freed"], 4000)
        self.assertFalse(Download.objects.filter(pk__in=[completed.pk, failed.pk, cancelled.pk]).exists())
        self.assertEqual(set(Download.objects.values_list("pk", flat=True)), {queued.pk, running.pk, paused.pk})
        self.assertTrue(os.path.exists(done_file), "downloaded files survive unless delete_files is set")
        for p in (part, ytdl, frag, inter):
            self.assertFalse(os.path.exists(p), p)

    def test_delete_files_flag(self):
        f = self._touch("gone.mp4", b"y" * 500)
        self._finished("/a", Download.COMPLETED, filepath=f)
        res = purge.purge(delete_files=True)
        self.assertEqual((res["files_deleted"], res["bytes_freed"]), (1, 500))
        self.assertFalse(os.path.exists(f))

    def test_status_filter_and_rejection_of_active_states(self):
        self._finished("/a", Download.COMPLETED)
        self._finished("/b", Download.FAILED)
        purge.purge(statuses=["failed"])
        self.assertEqual(list(Download.objects.values_list("status", flat=True)), [Download.COMPLETED])
        for bad in (["running"], ["queued"], ["failed", "paused"], ["retrying"]):
            with self.assertRaises(ValueError):
                purge.purge(statuses=bad)

    def test_older_than_days(self):
        old = self._finished("/old", Download.COMPLETED, finished_at=timezone.now() - timedelta(days=10))
        new = self._finished("/new", Download.COMPLETED, finished_at=timezone.now() - timedelta(days=1))
        res = purge.purge(older_than_days=7)
        self.assertEqual(res["entries"], 1)
        self.assertEqual(list(Download.objects.values_list("pk", flat=True)), [new.pk])
        self.assertFalse(Download.objects.filter(pk=old.pk).exists())

    def test_dry_run_changes_nothing(self):
        f = self._touch("keep.mp4", b"z" * 700)
        part = self._touch("k.mp4.part", b"p" * 300)
        self._finished("/a", Download.COMPLETED, filepath=f)
        self._finished("/b", Download.FAILED, partial_files=[os.path.join(self.root, "k.mp4")])
        res = purge.purge(delete_files=True, dry_run=True)
        self.assertEqual((res["entries"], res["files_deleted"], res["partials_deleted"], res["bytes_freed"]),
                         (2, 1, 1, 1000))
        self.assertEqual(Download.objects.count(), 2)
        self.assertTrue(os.path.exists(f) and os.path.exists(part))

    def test_never_deletes_outside_allowed_dirs(self):
        outside = os.path.join(tempfile.mkdtemp(), "precious.mp4")
        open(outside, "w").write("data")
        self._finished("/a", Download.COMPLETED, filepath=outside)
        res = purge.purge(delete_files=True)
        self.assertEqual(res["entries"], 1)
        self.assertEqual(res["files_deleted"], 0)
        self.assertTrue(os.path.exists(outside))

    def test_scoped_to_user(self):
        from django.contrib.auth import get_user_model
        bob = get_user_model().objects.create_user("bob", password="pw-bob-12345")
        mine = self._finished("/a", Download.FAILED)
        theirs = self._finished("/b", Download.FAILED, owner=bob)
        purge.purge(user=self.user)
        self.assertFalse(Download.objects.filter(pk=mine.pk).exists())
        self.assertTrue(Download.objects.filter(pk=theirs.pk).exists())

    def test_auto_purge_retention(self):
        self._finished("/old", Download.FAILED, finished_at=timezone.now() - timedelta(days=40))
        keep = self._finished("/new", Download.FAILED)
        self.assertIsNone(purge.auto_purge())            # retention 0 = off
        self.assertEqual(Download.objects.count(), 2)
        self.app.retention_days = 30
        self.app.save()
        res = purge.auto_purge()
        self.assertEqual(res["entries"], 1)
        self.assertEqual(list(Download.objects.values_list("pk", flat=True)), [keep.pk])

    def test_real_failed_download_then_purge_end_to_end(self):
        self.server.add("/slowfail.mp4", size=2 * 1024 * 1024, seed=61, drop_after=700_000, drop_hits=100)
        dl = self.make("/slowfail.mp4", max_retries=0)
        self.assertTrue(self.drive(lambda: self.status(dl) == Download.FAILED, timeout=60))
        d = self.reload(dl)
        self.assertTrue(d.partial_files, "partial file names are recorded for later cleanup")
        left = [f for f in os.listdir(self.root) if f.endswith(".part")]
        self.assertTrue(left, "a failed download leaves a .part file")
        res = purge.purge()
        self.assertEqual(res["entries"], 1)
        self.assertEqual([f for f in os.listdir(self.root)], [])

    def test_management_command(self):
        self._finished("/a", Download.FAILED)
        out = StringIO()
        call_command("purge", "--dry-run", stdout=out)
        self.assertIn("'entries': 1", out.getvalue())
        self.assertEqual(Download.objects.count(), 1)
        call_command("purge", "--status", "failed", stdout=out)
        self.assertEqual(Download.objects.count(), 0)
