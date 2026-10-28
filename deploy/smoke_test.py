#!/usr/bin/env python3
"""End-to-end check of a RUNNING stack (web + workers + beat + redis + postgres) over plain HTTP.

    python deploy/smoke_test.py http://localhost:8000 admin 'password' \
        --ok-url http://host/video.mp4 [--flaky-url ...] [--missing-url ...]

Needs only the standard library. Exit code 0 = everything passed.
"""
import argparse
import hashlib
import http.cookiejar
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


class Client:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf = ""

    def req(self, path, data=None, headers=None, raw=False):
        h = dict(headers or {})
        body = None
        if data is not None:
            body = data if isinstance(data, bytes) else json.dumps(data).encode()
            h.setdefault("Content-Type", "application/json")
            h["X-CSRFToken"] = self.csrf
            h["Referer"] = self.base + "/"
        r = urllib.request.Request(self.base + path, data=body, headers=h, method="POST" if body is not None else "GET")
        try:
            with self.op.open(r, timeout=60) as resp:
                content = resp.read()
                return resp.status, content if raw else json.loads(content or b"{}")
        except urllib.error.HTTPError as e:
            content = e.read()
            try:
                return e.code, json.loads(content)
            except ValueError:
                return e.code, {"raw": content[:200].decode("utf-8", "replace")}

    def login(self, user, password):
        _, html = self.req("/login/", raw=True)
        token = re.search(rb'name="csrfmiddlewaretoken" value="([^"]+)"', html).group(1).decode()
        form = urllib.parse.urlencode({"username": user, "password": password, "csrfmiddlewaretoken": token}).encode()
        r = urllib.request.Request(self.base + "/login/", data=form, headers={"Referer": self.base + "/login/"})
        self.op.open(r, timeout=30).read()
        _, page = self.req("/", raw=True)
        m = re.search(rb'name="csrf-token" content="([^"]+)"', page)
        if not m:
            raise SystemExit("login failed (no dashboard)")
        self.csrf = m.group(1).decode()


def check(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg, flush=True)
    if not cond:
        sys.exit(1)


def wait_for(c, did, want, timeout=120):
    end, seen = time.time() + timeout, []
    while time.time() < end:
        _, snap = c.req("/api/snapshot/")
        row = next((d for d in snap["downloads"] if d["id"] == did), None)
        if row:
            if not seen or seen[-1] != row["status"]:
                seen.append(row["status"])
            if row["status"] in want:
                return row, seen
        time.sleep(0.5)
    return None, seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("user")
    ap.add_argument("password")
    ap.add_argument("--ok-url")
    ap.add_argument("--ok-sha256", help="expected sha256 of the --ok-url file")
    ap.add_argument("--flaky-url", help="URL that fails a few times, then works")
    ap.add_argument("--missing-url", help="URL answering 404")
    a = ap.parse_args()

    c = Client(a.base)
    st, _ = c.req("/api/snapshot/")
    check(st == 401, "API refuses anonymous access")
    c.login(a.user, a.password)
    st, snap = c.req("/api/snapshot/")
    check(st == 200 and "downloads" in snap, "login + snapshot")

    if a.ok_url:
        st, j = c.req("/api/downloads/", {"urls": a.ok_url, "max_retries": 3})
        check(st == 201, "add download: %s" % j)
        row, seen = wait_for(c, j["created"][0], {"completed", "failed"})
        check(row and row["status"] == "completed", "download completed (states seen: %s)" % seen)
        st, data = c.req("/api/downloads/%d/file/" % row["id"], raw=True)
        check(st == 200 and len(data) == row["filesize"], "file served, %d bytes" % len(data))
        if a.ok_sha256:
            check(hashlib.sha256(data).hexdigest() == a.ok_sha256, "sha256 matches")
    if a.missing_url:
        st, j = c.req("/api/downloads/", {"urls": a.missing_url, "max_retries": "always"})
        row, seen = wait_for(c, j["created"][0], {"failed"})
        check(row and row["attempt"] == 0, "404 with infinite retries fails immediately (permanent error)")
    if a.flaky_url:
        st, j = c.req("/api/downloads/", {"urls": a.flaky_url, "max_retries": "always"})
        row, seen = wait_for(c, j["created"][0], {"completed", "failed"}, timeout=240)
        check(row and row["status"] == "completed" and row["attempt"] >= 1 and "retrying" in seen,
              "flaky download retried and completed (states: %s, retries: %s)" % (seen, row and row["retries_label"]))

    st, res = c.req("/api/purge/", {"dry_run": True})
    check(st == 200 and "entries" in res, "purge preview: %s" % res)
    st, res = c.req("/api/purge/", {"delete_files": True})
    check(st == 200, "purge: %s" % res)
    _, snap = c.req("/api/snapshot/")
    check(not [d for d in snap["downloads"] if d["status"] in ("completed", "failed", "cancelled")],
          "no finished entries left after purge")
    print("ALL OK")


if __name__ == "__main__":
    main()
