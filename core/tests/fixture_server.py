"""Local HTTP server used by the tests: Range support, throttling and failure injection.

Serves byte blobs as `video/mp4` so yt-dlp's generic extractor treats them as direct media.
"""
import hashlib
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence
        pass

    def _serve(self, head_only):
        srv = self.server
        path = self.path.split("?")[0]
        with srv.lock:
            srv.hits[path] = srv.hits.get(path, 0) + 1
            if self.headers.get("Range"):
                srv.ranges.append((path, self.headers.get("Range")))
            hit = srv.hits[path]
        blob = srv.files.get(path)
        if blob is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if hit <= srv.fail_first.get(path, 0):
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        start = 0
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes=") and path not in srv.no_range:
            start = int(rng[6:].split("-")[0] or 0)
        total = len(blob)
        if start:
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, total - 1, total))
        else:
            self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "none" if path in srv.no_range else "bytes")
        self.send_header("Content-Length", str(total - start))
        self.end_headers()
        if head_only:
            return
        delay = srv.chunk_delay.get(path, 0)
        drop_after = srv.drop_after.get(path)
        with srv.lock:
            srv.body_hits[path] = srv.body_hits.get(path, 0) + 1
            body_hit = srv.body_hits[path]
        # cut the first N body-sending GETs (extractor probes count too); Range resumes are never cut
        if drop_after is not None and body_hit <= srv.drop_hits.get(path, 1) and not start:
            self.wfile.write(blob[start:drop_after])
            self.wfile.flush()
            self.close_connection = True
            return
        pos = start
        step = 64 * 1024
        try:
            while pos < total:
                self.wfile.write(blob[pos:pos + step])
                pos += step
                if delay:
                    self.wfile.flush()
                    time.sleep(delay)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        self._serve(False)

    def do_HEAD(self):
        self._serve(True)


class FixtureServer:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        h = self.httpd
        h.daemon_threads = True
        h.lock = threading.Lock()
        h.files, h.hits, h.fail_first, h.chunk_delay = {}, {}, {}, {}
        h.drop_after, h.drop_hits, h.no_range = {}, {}, set()
        h.ranges = []
        h.body_hits = {}
        self.thread = threading.Thread(target=h.serve_forever, daemon=True)

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def add(self, path, size=2 * 1024 * 1024, seed=1, **kw):
        blob = hashlib.sha256(str(seed).encode()).digest() * (size // 32 + 1)
        blob = blob[:size]
        self.httpd.files[path] = blob
        for k, v in kw.items():
            getattr(self.httpd, k)[path] = v
        return hashlib.sha256(blob).hexdigest()

    def hits(self, path):
        return self.httpd.hits.get(path, 0)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":  # dev helper: python -m core.tests.fixture_server  (used by deploy/smoke_test.py)
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    srv = FixtureServer()
    srv.httpd.server_close()
    srv.httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    h = srv.httpd
    h.daemon_threads = True
    h.lock = threading.Lock()
    h.files, h.hits, h.fail_first, h.chunk_delay = {}, {}, {}, {}
    h.drop_after, h.drop_hits, h.no_range, h.ranges, h.body_hits = {}, {}, set(), [], {}
    print("/ok.mp4 sha256", srv.add("/ok.mp4", size=2 * 1024 * 1024, seed=1), flush=True)
    print("/flaky.mp4 sha256", srv.add("/flaky.mp4", size=1024 * 1024, seed=2, fail_first=6), flush=True)
    print("/slow.mp4 sha256", srv.add("/slow.mp4", size=8 * 1024 * 1024, seed=3, chunk_delay=0.05), flush=True)
    print("serving on http://127.0.0.1:%d" % port, flush=True)
    h.serve_forever()
