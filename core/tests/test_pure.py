import os
import random
import tempfile

from django.test import SimpleTestCase, TestCase

from core.models import AppSettings
from core.services import formats, retry, security
from core.services.worker import runner_env


class RetryPolicyTests(SimpleTestCase):
    def test_parse(self):
        for v, want in [(5, 5), ("5", 5), (" 12 ", 12), (0, 0), (-1, -1), ("-1", -1), ("inf", -1), ("Infinity", -1),
                        ("ALWAYS", -1), ("forever", -1), ("unlimited", -1), ("\u221e", -1)]:
            self.assertEqual(retry.parse_retries(v), want, v)

    def test_parse_rejects(self):
        for v in ("x", "", None, True, 1.5, -2, 10 ** 9, "3.5", "1e3"):
            with self.assertRaises(ValueError, msg=repr(v)):
                retry.parse_retries(v)

    def test_should_retry_semantics(self):
        # max_retries=3 -> initial try + exactly 3 retries
        self.assertEqual([retry.should_retry(3, a) for a in range(5)], [True, True, True, False, False])
        self.assertFalse(retry.should_retry(0, 0))
        self.assertTrue(retry.should_retry(-1, 10 ** 9))

    def test_backoff(self):
        rng = random.Random(7)
        seq = [retry.backoff_delay(a, 10, 900, rng) for a in range(1, 9)]
        self.assertTrue(all(b >= a for a, b in zip(seq, seq[1:])))
        self.assertTrue(10 <= seq[0] <= 11 and seq[-1] <= 900)
        self.assertEqual(retry.backoff_delay(10 ** 6, 10, 900), 900)  # no overflow, capped
        self.assertLessEqual(retry.backoff_delay(1, 10, 5), 5)  # cap smaller than base

    def test_permanent(self):
        self.assertTrue(retry.is_permanent("ERROR: [generic] x: HTTP Error 404: Not Found"))
        self.assertTrue(retry.is_permanent("ERROR: [youtube] abc: Video unavailable"))
        self.assertTrue(retry.is_permanent("Unsupported URL: http://x"))
        self.assertFalse(retry.is_permanent("HTTP Error 503: Service Unavailable"))
        self.assertFalse(retry.is_permanent("HTTP Error 403: Forbidden"))  # often transient on YouTube
        self.assertFalse(retry.is_permanent("Connection reset by peer"))


class FormatTests(SimpleTestCase):
    def test_resolve(self):
        self.assertEqual(formats.resolve("best", True)[0], "bv*+ba/b")
        self.assertEqual(formats.resolve("best", False)[0], "b")
        self.assertEqual(formats.resolve("720p", False)[0], "b[height<=720]")
        sel, pp = formats.resolve("audio", True)
        self.assertEqual(pp[0]["key"], "FFmpegExtractAudio")
        self.assertEqual(formats.resolve("audio", False)[1], [])
        self.assertEqual(formats.resolve("bv*[height<=480]+ba", True), ("bv*[height<=480]+ba", []))

    def test_validate(self):
        self.assertEqual(formats.validate_format("bv*[height<=720]+ba/b"), "bv*[height<=720]+ba/b")
        # string filters use ^= $= *= ~= ; the selector is parsed by yt-dlp, never by a shell
        self.assertEqual(formats.validate_format("b[ext$=mp4]"), "b[ext$=mp4]")
        for bad in ("", "a;b", "x" * 201, "a\nb", "`x`", "a\"b", "a'b", "{x}", "a\\b"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                formats.validate_format(bad)


class UrlGuardTests(SimpleTestCase):
    def test_blocks_non_public(self):
        for u in ("http://127.0.0.1/x", "http://localhost/x", "http://10.0.0.5/x", "http://192.168.1.1/",
                  "http://169.254.169.254/latest/meta-data", "http://[::1]/", "http://0.0.0.0/",
                  "http://172.16.0.1/", "http://[fe80::1]/", "http://224.0.0.1/"):
            with self.assertRaises(security.ValidationError, msg=u):
                security.validate_url(u, guard=True)

    def test_allows_public_literal_and_scheme_rules(self):
        self.assertEqual(security.validate_url("https://93.184.216.34/v", guard=True), "https://93.184.216.34/v")
        for u in ("ftp://93.184.216.34/x", "file:///etc/passwd", "javascript:alert(1)", "", "http://", "http://a b/",
                  "http://x/\r\nHost: evil", "x" * 3000):
            with self.assertRaises(security.ValidationError, msg=u[:40]):
                security.validate_url(u, guard=False)
        # guard off allows loopback (used by the tests only)
        security.validate_url("http://127.0.0.1:8000/x", guard=False)


class PathConfinementTests(TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.outside = os.path.realpath(tempfile.mkdtemp())
        self.app = AppSettings.get_solo()
        self.app.allowed_dirs = self.root
        self.app.save()

    def test_resolve(self):
        self.assertEqual(security.resolve_output_dir("", self.app), self.root)
        self.assertEqual(security.resolve_output_dir("sub/dir", self.app), os.path.join(self.root, "sub", "dir"))
        self.assertTrue(os.path.isdir(os.path.join(self.root, "sub", "dir")))
        self.assertEqual(security.resolve_output_dir(os.path.join(self.root, "abs"), self.app),
                         os.path.join(self.root, "abs"))

    def test_rejects_escape(self):
        for v in ("../x", "a/../../x", self.outside, "/etc", "x\0y"):
            with self.assertRaises(security.ValidationError, msg=v):
                security.resolve_output_dir(v, self.app)

    def test_rejects_symlink_escape(self):
        os.symlink(self.outside, os.path.join(self.root, "link"))
        with self.assertRaises(security.ValidationError):
            security.resolve_output_dir("link/inner", self.app)
        self.assertFalse(os.path.exists(os.path.join(self.outside, "inner")))

    def test_safe_unlink(self):
        inside = os.path.join(self.root, "a.part")
        open(inside, "w").close()
        out = os.path.join(self.outside, "keep.txt")
        open(out, "w").close()
        os.symlink(out, os.path.join(self.root, "sym.part"))
        self.assertFalse(security.safe_unlink(out, self.app))                       # outside root
        self.assertFalse(security.safe_unlink(os.path.join(self.root, "sym.part"), self.app))  # symlink
        self.assertTrue(os.path.exists(out))
        self.assertFalse(security.safe_unlink(os.path.join(self.root, "..", os.path.basename(self.outside), "keep.txt"), self.app))
        self.assertTrue(security.safe_unlink(inside, self.app))
        self.assertFalse(os.path.exists(inside))
        self.assertFalse(security.safe_unlink(self.root, self.app))                 # directory


class RunnerEnvTests(SimpleTestCase):
    def test_secrets_not_passed(self):
        os.environ["DJANGO_SECRET_KEY"] = "s3cret"
        os.environ["DATABASE_URL"] = "postgres://u:p@h/d"
        try:
            env = runner_env()
        finally:
            os.environ.pop("DJANGO_SECRET_KEY")
            os.environ.pop("DATABASE_URL")
        self.assertNotIn("DJANGO_SECRET_KEY", env)
        self.assertNotIn("DATABASE_URL", env)
        self.assertIn("PATH", env)
