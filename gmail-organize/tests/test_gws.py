import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import gws  # noqa: E402


def proc(code=0, out="", err=""):
    return SimpleNamespace(returncode=code, stdout=out, stderr=err)


class Recorder:
    """Subprocess stand-in that returns queued results and records argv."""

    def __init__(self, *results):
        self.results = list(results)
        self.argvs = []

    def __call__(self, argv, **kwargs):
        self.argvs.append(argv)
        return self.results.pop(0)


class TestBuildArgv(unittest.TestCase):
    def test_splits_path_and_encodes_params(self):
        argv = gws.build_argv("gws", "gmail.users.messages", "list", {"userId": "me", "q": "a b"})
        self.assertEqual(argv[:5], ["gws", "gmail", "users", "messages", "list"])
        self.assertEqual(argv[5], "--params")
        self.assertEqual(json.loads(argv[6]), {"userId": "me", "q": "a b"})
        self.assertNotIn("--json", argv)

    def test_body_becomes_json_flag(self):
        argv = gws.build_argv("gws", "gmail.users.labels", "patch", {"id": "L"}, {"name": "X"})
        self.assertEqual(json.loads(argv[argv.index("--json") + 1]), {"name": "X"})

    def test_non_ascii_is_escaped(self):
        argv = gws.build_argv("gws", "gmail.users.labels", "patch", {}, {"name": "Café"})
        self.assertTrue(all(ord(c) < 128 for c in argv[-1]))


class TestResolve(unittest.TestCase):
    def test_override_wins(self):
        exe = gws.resolve_gws(env={"GMAIL_ORGANIZE_GWS": "/x/gws"}, which=lambda n: "/y/gws")
        self.assertEqual(exe, "/x/gws")

    def test_cmd_shim_swapped_for_real_exe(self):
        exe = gws.resolve_gws(env={}, which=lambda n: r"C:\npm\gws.CMD", exists=lambda p: True)
        self.assertTrue(exe.endswith("gws.exe"))
        self.assertIn("@googleworkspace", exe)

    def test_cmd_shim_kept_when_no_exe(self):
        exe = gws.resolve_gws(env={}, which=lambda n: r"C:\npm\gws.cmd", exists=lambda p: False)
        self.assertEqual(exe, r"C:\npm\gws.cmd")

    def test_missing_gws_raises(self):
        with self.assertRaises(gws.GwsError) as cm:
            gws.resolve_gws(env={}, which=lambda n: None)
        self.assertEqual(cm.exception.code, gws.EXIT_NOT_FOUND)


class TestCmdShimGuard(unittest.TestCase):
    def test_metachar_through_shim_refused_before_running(self):
        rec = Recorder()
        with self.assertRaises(gws.GwsError) as cm:
            gws.run_gws(r"C:\npm\gws.cmd", "gmail.users.labels", "create", {},
                        {"name": "R&D"}, runner=rec)
        self.assertEqual(cm.exception.code, gws.EXIT_VALIDATION)
        self.assertEqual(rec.argvs, [])

    def test_long_command_through_shim_refused(self):
        with self.assertRaises(gws.GwsError):
            gws.check_cmd_shim(["gws.cmd", "--json", "x" * 9000])

    def test_real_exe_allows_metachars(self):
        rec = Recorder(proc(out='{"id": "L1"}'))
        out = gws.run_gws("gws.exe", "gmail.users.labels", "create", {}, {"name": "R&D"}, runner=rec)
        self.assertEqual(out, {"id": "L1"})


class TestRun(unittest.TestCase):
    def test_parses_json_after_stray_lines(self):
        rec = Recorder(proc(out='Using keyring backend: keyring\n{"a": 1}'))
        self.assertEqual(gws.run_gws("gws", "gmail.users", "getProfile", {}, runner=rec), {"a": 1})

    def test_empty_output_is_empty_dict(self):
        self.assertEqual(gws.run_gws("gws", "gmail.users.labels", "x", {}, runner=Recorder(proc())), {})

    def test_rate_limit_retried_with_backoff(self):
        sleeps = []
        rec = Recorder(proc(1, err="429 rateLimitExceeded"), proc(1, err="429"), proc(out='{"ok": true}'))
        out = gws.run_gws("gws", "p", "get", {}, runner=rec, sleep=sleeps.append)
        self.assertEqual(out, {"ok": True})
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_rate_limit_gives_up_after_retries(self):
        rec = Recorder(*[proc(1, err="429")] * (gws.RETRIES + 1))
        with self.assertRaises(gws.GwsError) as cm:
            gws.run_gws("gws", "p", "get", {}, runner=rec, sleep=lambda s: None)
        self.assertTrue(cm.exception.rate_limited)
        self.assertEqual(len(rec.argvs), gws.RETRIES + 1)

    def test_backoff_outlasts_a_per_minute_quota_window(self):
        sleeps = []
        quota = "error[api]: Quota exceeded for quota metric 'Total Query Cost' and limit 'Units per minute per user'"
        rec = Recorder(*[proc(1, err=quota)] * (gws.RETRIES + 1))
        with self.assertRaises(gws.GwsError):
            gws.run_gws("gws", "p", "get", {}, runner=rec, sleep=sleeps.append)
        self.assertGreater(sum(sleeps), 60)

    def test_other_errors_not_retried(self):
        rec = Recorder(proc(2, err="auth"))
        with self.assertRaises(gws.GwsError) as cm:
            gws.run_gws("gws", "p", "get", {}, runner=rec, sleep=lambda s: self.fail("slept"))
        self.assertEqual(cm.exception.code, gws.EXIT_AUTH)

    def test_not_found_detected(self):
        self.assertTrue(gws.GwsError(1, "404 Requested entity was not found.").not_found)
        self.assertTrue(gws.GwsError(1, "400 Invalid id value").not_found)
        self.assertFalse(gws.GwsError(2, "not found").not_found)
        self.assertFalse(gws.GwsError(1, "500 backend error").not_found)

    def test_not_found_ignores_404_inside_ids_and_loose_phrases(self):
        self.assertFalse(gws.GwsError(1, "500 Internal error on messages/18f4041ab").not_found)
        self.assertFalse(gws.GwsError(1, "500 config not found in cache").not_found)
        self.assertTrue(gws.GwsError(1, '{"error": {"code": 404, "message": "x"}}').not_found)


if __name__ == "__main__":
    unittest.main()
