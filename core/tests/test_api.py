import json
import os

from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from core.models import Download

from .base import DownloadTestCase

User = get_user_model()


class ApiTests(DownloadTestCase):
    def setUp(self):
        super().setUp()
        self.bob = User.objects.create_user("bob", password="pw-bob-12345")
        self.admin = User.objects.create_user("root", password="pw-root-12345", is_staff=True)
        self.ca = Client()
        self.ca.force_login(self.user)
        self.cb = Client()
        self.cb.force_login(self.bob)
        self.cs = Client()
        self.cs.force_login(self.admin)

    def post(self, client, url, data=None):
        return client.post(url, json.dumps(data or {}), content_type="application/json")

    def add(self, client=None, **data):
        data.setdefault("urls", self.server.base + "/a.mp4")
        return self.post(client or self.ca, "/api/downloads/", data)

    def test_authentication_required(self):
        c = Client()
        for method, url in (("get", "/api/snapshot/"), ("post", "/api/downloads/"), ("post", "/api/purge/"),
                            ("post", "/api/downloads/1/pause/"), ("get", "/api/downloads/1/file/")):
            r = getattr(c, method)(url)
            self.assertEqual(r.status_code, 401, url)
            self.assertIn("error", r.json())
        self.assertEqual(c.get("/").status_code, 302)

    def test_csrf_enforced(self):
        c = Client(enforce_csrf_checks=True)
        c.force_login(self.user)
        r = c.post("/api/downloads/", json.dumps({"urls": self.server.base + "/a.mp4"}), content_type="application/json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(Download.objects.count(), 0)

    def test_add_defaults_and_retry_words(self):
        self.app.default_retries = 7
        self.app.save()
        r = self.add()
        self.assertEqual(r.status_code, 201, r.content)
        d = Download.objects.get(pk=r.json()["created"][0])
        self.assertEqual((d.max_retries, d.format, d.owner_id, d.backend), (7, "best", self.user.pk, "yt_dlp"))
        self.assertEqual(d.output_dir, os.path.realpath(self.root))
        for given, want in (("always", -1), ("inf", -1), (-1, -1), ("3", 3), (0, 0), (12, 12)):
            r = self.add(max_retries=given)
            self.assertEqual(Download.objects.get(pk=r.json()["created"][0]).max_retries, want, given)

    def test_add_validation(self):
        for bad in ({"max_retries": "abc"}, {"max_retries": -5}, {"max_retries": 10 ** 9}, {"format": "a;b"},
                    {"output_dir": "../../etc"}, {"output_dir": "/etc"}, {"ratelimit_kib": "x"},
                    {"ratelimit_kib": -1}, {"start_at": "tomorrow-ish"}, {"urls": ""}):
            r = self.add(**bad)
            self.assertEqual(r.status_code, 400, (bad, r.content))
        self.assertEqual(Download.objects.count(), 0)
        self.assertEqual(self.ca.post("/api/downloads/", "{not json", content_type="application/json").status_code, 400)
        self.assertEqual(self.ca.post("/api/downloads/", "[1]", content_type="application/json").status_code, 400)

    def test_add_bad_urls_reported_per_url_good_ones_kept(self):
        self.app.ssrf_guard = True
        self.app.save()
        r = self.add(urls="http://127.0.0.1/x\nftp://93.184.216.34/x\nhttps://93.184.216.34/watch?v=1")
        body = r.json()
        self.assertEqual(r.status_code, 201)
        self.assertEqual(len(body["created"]), 1)
        self.assertEqual(len(body["errors"]), 2)
        self.assertTrue(Download.objects.get(pk=body["created"][0]).url.endswith("watch?v=1"))
        self.assertEqual(self.add(urls="http://127.0.0.1/x").status_code, 400)

    def test_queue_cap(self):
        self.app.per_user_queue_cap = 2
        self.app.save()
        r = self.add(urls="\n".join(self.server.base + "/%d.mp4" % i for i in range(4)))
        self.assertEqual((len(r.json()["created"]), len(r.json()["errors"])), (2, 2))
        self.assertEqual(self.add(client=self.cb).status_code, 201)   # per user, not global

    def test_isolation_between_users(self):
        mine = self.make("/a.mp4")
        theirs = self.make("/b.mp4", owner=self.bob)
        ids = [d["id"] for d in self.ca.get("/api/snapshot/").json()["downloads"]]
        self.assertEqual(ids, [mine.pk])
        for name in ("pause", "resume", "cancel", "retry", "remove", "retries", "move"):
            r = self.post(self.ca, "/api/downloads/%d/%s/" % (theirs.pk, name), {"value": 1, "direction": "top"})
            self.assertEqual(r.status_code, 404, name)
        self.assertEqual(self.ca.get("/api/downloads/%d/file/" % theirs.pk).status_code, 404)
        self.assertEqual(self.status(theirs), Download.QUEUED)
        admin_ids = {d["id"] for d in self.cs.get("/api/snapshot/").json()["downloads"]}
        self.assertEqual(admin_ids, {mine.pk, theirs.pk})
        self.assertEqual(self.post(self.cs, "/api/downloads/%d/pause/" % theirs.pk).status_code, 200)
        self.assertEqual(self.status(theirs), Download.PAUSED)

    def test_snapshot_shape_and_error_truncation(self):
        d = self.make("/a.mp4", status=Download.RETRYING, attempt=2, max_retries=-1,
                      next_retry_at=timezone.now() + timezone.timedelta(seconds=30), last_error="e" * 900)
        snap = self.ca.get("/api/snapshot/").json()
        row = snap["downloads"][0]
        self.assertEqual((row["retries_label"], row["status"]), ("2/\u221e", "retrying"))
        self.assertTrue(28 <= row["next_retry_in"] <= 30)
        self.assertEqual(len(row["error"]), 500)
        self.assertIsNone(row["owner"])
        self.assertEqual(snap["counts"]["retrying"], 1)
        self.assertEqual(self.cs.get("/api/snapshot/").json()["downloads"][0]["owner"], "alice")

    def test_actions_and_conflicts(self):
        d = self.make("/a.mp4")
        self.assertEqual(self.post(self.ca, "/api/downloads/%d/pause/" % d.pk).status_code, 200)
        self.assertEqual(self.post(self.ca, "/api/downloads/%d/pause/" % d.pk).status_code, 409)
        self.assertEqual(self.post(self.ca, "/api/downloads/%d/resume/" % d.pk).status_code, 200)
        r = self.post(self.ca, "/api/downloads/%d/retries/" % d.pk, {"value": "always"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.reload(d).max_retries, -1)
        self.assertEqual(self.post(self.ca, "/api/downloads/%d/retries/" % d.pk, {"value": "zzz"}).status_code, 409)
        self.assertEqual(self.post(self.ca, "/api/downloads/%d/bogus/" % d.pk).status_code, 404)
        self.assertEqual(self.ca.get("/api/downloads/%d/pause/" % d.pk).status_code, 405)
        self.assertEqual(self.post(self.ca, "/api/downloads/%d/cancel/" % d.pk).status_code, 200)
        self.assertEqual(self.post(self.ca, "/api/downloads/%d/remove/" % d.pk).status_code, 200)
        self.assertEqual(self.post(self.ca, "/api/downloads/%d/remove/" % d.pk).status_code, 404)

    def test_purge_api(self):
        mine = self.make("/a.mp4", status=Download.FAILED, finished_at=timezone.now())
        theirs = self.make("/b.mp4", status=Download.FAILED, owner=self.bob, finished_at=timezone.now())
        live = self.make("/c.mp4")
        r = self.post(self.ca, "/api/purge/", {"dry_run": True})
        self.assertEqual((r.status_code, r.json()["entries"], r.json()["dry_run"]), (200, 1, True))
        self.assertEqual(Download.objects.count(), 3)
        self.assertEqual(self.post(self.ca, "/api/purge/", {"statuses": ["queued"]}).status_code, 400)
        self.assertEqual(self.post(self.ca, "/api/purge/", {"statuses": "failed"}).status_code, 400)
        self.assertEqual(self.post(self.ca, "/api/purge/", {"older_than_days": "x"}).status_code, 400)
        self.assertEqual(self.post(self.ca, "/api/purge/", {"all": True}).json()["entries"], 0 + 1)  # all ignored for non-staff
        self.assertTrue(Download.objects.filter(pk=theirs.pk).exists())
        self.assertEqual(self.post(self.cs, "/api/purge/", {"all": True}).json()["entries"], 1)
        self.assertEqual(list(Download.objects.values_list("pk", flat=True)), [live.pk])

    def test_file_download_owner_only_and_confined(self):
        sha = self.server.add("/a.mp4", size=200_000, seed=71)
        dl = self.make("/a.mp4")
        self.assertTrue(self.drive(lambda: self.status(dl) == Download.COMPLETED))
        r = self.ca.get("/api/downloads/%d/file/" % dl.pk)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(b"".join(r.streaming_content)), 200_000)
        self.assertIn("attachment", r["Content-Disposition"])
        self.assertEqual(self.cb.get("/api/downloads/%d/file/" % dl.pk).status_code, 404)
        Download.objects.filter(pk=dl.pk).update(filepath="/etc/passwd")
        self.assertEqual(self.ca.get("/api/downloads/%d/file/" % dl.pk).status_code, 404)

    def test_settings_page_staff_only_and_validation(self):
        self.assertEqual(self.ca.get("/settings/").status_code, 403)
        r = self.cs.get("/settings/")
        self.assertEqual(r.status_code, 200)
        form = r.context["form"].initial
        data = {k: v for k, v in form.items() if v is not None and not isinstance(v, bool)}
        data.update({k: "on" for k, v in form.items() if v is True})
        for k in list(data):
            data[k] = data[k]
        ok = dict(data, max_simultaneous=4, default_retries=-1)
        self.assertEqual(self.cs.post("/settings/", ok).status_code, 200)
        self.app.refresh_from_db()
        self.assertEqual((self.app.max_simultaneous, self.app.default_retries), (4, -1))
        for bad in ({"max_simultaneous": 0}, {"default_retries": -9}, {"allowed_dirs": "relative/dir"},
                    {"retry_delay_base": 100, "retry_delay_max": 5}, {"default_format": "a;b"},
                    {"socket_timeout": 1}, {"allowed_dirs": "/"}):
            r = self.cs.post("/settings/", dict(ok, **bad))
            self.assertTrue(r.context["form"].errors, bad)
        self.app.refresh_from_db()
        self.assertEqual(self.app.max_simultaneous, 4)
        self.assertEqual(self.post(self.ca, "/settings/", ok).status_code, 403)

    def test_pages_render(self):
        r = self.ca.get("/")
        self.assertContains(r, 'name="csrf-token"')
        self.assertContains(r, "ytpanel")
        self.assertEqual(Client().get("/login/").status_code, 200)
