import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import scan  # noqa: E402

DF = """Filesystem        Size    Used   Avail Capacity iused ifree %iused  Mounted on
/dev/disk3s1s1   228Gi    12Gi    64Gi    16%    459k  675M    0%   /
/dev/disk3s6     228Gi    10Gi    64Gi    14%      10  675M    0%   /System/Volumes/VM
/dev/disk3s2     228Gi   8.5Gi    64Gi    12%    1.7k  675M    0%   /System/Volumes/Preboot
/dev/disk3s5     228Gi   132Gi    64Gi    68%    2.4M  675M    0%   /System/Volumes/Data
/dev/disk5s1     238Gi   160Gi    79Gi    67%    1.8M  825M    0%   /Volumes/External
devfs           200Ki   200Ki     0Bi   100%     693     0  100%   /dev
map auto_home     0Bi     0Bi     0Bi   100%       0     0     -   /System/Volumes/Data/home
"""


class TestParseDf(unittest.TestCase):
    def test_extracts_every_mount(self):
        mounts = scan.parse_df(DF)
        self.assertEqual(len(mounts), 7)

    def test_converts_gibibytes_to_bytes(self):
        data = [m for m in scan.parse_df(DF) if m["mount"] == "/System/Volumes/Data"][0]
        self.assertEqual(data["used_bytes"], int(132 * 1024 ** 3))


class TestChooseRoots(unittest.TestCase):
    def setUp(self):
        self.roots = scan.choose_roots(scan.parse_df(DF))

    def test_data_volume_is_a_root(self):
        self.assertIn("/System/Volumes/Data", self.roots)

    def test_external_volume_is_a_root(self):
        self.assertIn("/Volumes/External", self.roots)

    def test_sealed_system_volume_is_not_a_root(self):
        self.assertNotIn("/", self.roots)

    def test_os_managed_volumes_are_not_roots(self):
        self.assertNotIn("/System/Volumes/VM", self.roots)
        self.assertNotIn("/System/Volumes/Preboot", self.roots)

    def test_home_is_not_a_separate_root(self):
        # /Users is a firmlink into the Data volume; listing both double-counts.
        self.assertFalse(any(r.startswith("/Users") for r in self.roots))

    def test_pseudo_filesystems_are_not_roots(self):
        self.assertNotIn("/dev", self.roots)

    def test_autofs_home_is_not_a_root(self):
        self.assertNotIn("/System/Volumes/Data/home", self.roots)

    def test_only_the_two_real_roots_remain(self):
        self.assertEqual(sorted(self.roots),
                         ["/System/Volumes/Data", "/Volumes/External"])


DU = """8388608\t/Users/me/Library/Caches/BraveSoftware
6920602\t/Users/me/.cache/uv
48234496\t/Volumes/External/GameLibrary
"""


class TestParseDu(unittest.TestCase):
    def test_converts_kilobyte_blocks_to_bytes(self):
        rows = scan.parse_du(DU)
        self.assertEqual(rows[0]["bytes"], 8589934592)
        self.assertEqual(rows[0]["path"], "/Users/me/Library/Caches/BraveSoftware")

    def test_handles_paths_containing_spaces(self):
        rows = scan.parse_du("1\t/Users/me/Library/Application Support/Claude\n")
        self.assertEqual(rows[0]["path"], "/Users/me/Library/Application Support/Claude")
        self.assertEqual(rows[0]["bytes"], 1024)

    def test_ignores_blank_lines(self):
        self.assertEqual(len(scan.parse_du(DU + "\n\n")), 3)

    def test_block_size_is_overridable(self):
        self.assertEqual(scan.parse_du("2\t/x\n", block_size=512)[0]["bytes"], 1024)


class TestCacheStaleness(unittest.TestCase):
    def test_fresh_cache_is_not_stale(self):
        fresh = {"scanned_at": 1000.0, "rows": [], "files": [], "projects": []}
        self.assertFalse(scan.is_stale(fresh, now_ts=1000.0 + 3600))

    def test_cache_older_than_a_day_is_stale(self):
        aged = {"scanned_at": 1000.0, "rows": [], "files": [], "projects": []}
        self.assertTrue(scan.is_stale(aged, now_ts=1000.0 + 90000))

    def test_missing_timestamp_is_stale(self):
        self.assertTrue(scan.is_stale({}, now_ts=1000.0))

    def test_payload_missing_a_pass_is_stale_regardless_of_age(self):
        # A cache written before the "projects" pass existed must not be
        # mistaken for a fresh, complete scan just because it is recent.
        fresh = {"scanned_at": 1000.0, "rows": [], "files": []}   # no "projects"
        self.assertTrue(scan.is_stale(fresh, now_ts=1000.0))

    def test_complete_fresh_payload_is_not_stale(self):
        fresh = {"scanned_at": 1000.0, "rows": [], "files": [], "projects": []}
        self.assertFalse(scan.is_stale(fresh, now_ts=1000.0 + 3600))


class TestCachePath(unittest.TestCase):
    def test_root_is_encoded_into_a_flat_filename(self):
        p = scan.cache_path("/System/Volumes/Data", Path("/tmp/cache"))
        self.assertEqual(p.parent, Path("/tmp/cache"))
        self.assertNotIn("/", p.name[1:])


class TestStderrClassification(unittest.TestCase):
    def test_permission_denied_counts_as_unreadable(self):
        self.assertEqual(scan._classify_stderr("du: /x: Permission denied\n"), (1, 0))

    def test_operation_not_permitted_counts_as_unreadable(self):
        # macOS's TCC wording for the same denial (e.g. ~/.Trash without Full
        # Disk Access) never says "Permission denied" - it must count too.
        self.assertEqual(scan._classify_stderr("du: /x: Operation not permitted\n"), (1, 0))

    def test_vanished_path_counts_as_transient(self):
        self.assertEqual(scan._classify_stderr("du: /x: No such file or directory\n"), (0, 1))

    def test_mixed_stderr_is_split(self):
        err = ("du: /a: Permission denied\n"
               "du: /b: No such file or directory\n"
               "du: /c: Permission denied\n")
        self.assertEqual(scan._classify_stderr(err), (2, 1))

    def test_empty_stderr(self):
        self.assertEqual(scan._classify_stderr(""), (0, 0))
        self.assertEqual(scan._classify_stderr(None), (0, 0))


class TestCacheRoundTrip(unittest.TestCase):
    def test_write_then_read_returns_the_payload(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        payload = {"rows": [{"path": "/x", "bytes": 1024}], "unreadable_count": 2}
        scan.write_cache("/System/Volumes/Data", tmp, payload)
        back = scan.read_cache("/System/Volumes/Data", tmp)
        self.assertEqual(back["rows"], payload["rows"])
        self.assertEqual(back["root"], "/System/Volumes/Data")
        self.assertIn("scanned_at", back)

    def test_read_cache_returns_none_when_absent(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.assertIsNone(scan.read_cache("/nope", tmp))


class TestSparseDetection(unittest.TestCase):
    def test_file_is_sparse_when_apparent_exceeds_allocated(self):
        self.assertTrue(scan.is_sparse(allocated=9_961_472, apparent=68_719_476_736))

    def test_ordinary_file_is_not_sparse(self):
        self.assertFalse(scan.is_sparse(allocated=2_900_000_000, apparent=2_900_000_000))

    def test_small_difference_is_not_sparse(self):
        # Block rounding alone must not be reported as sparse.
        self.assertFalse(scan.is_sparse(allocated=4096, apparent=3000))


class TestFileRowShape(unittest.TestCase):
    def test_row_carries_both_sizes(self):
        row = scan.file_row("/tmp/x.img", allocated=1024, apparent=4096)
        self.assertEqual(row["bytes"], 1024)
        self.assertEqual(row["apparent_bytes"], 4096)
        self.assertTrue(row["sparse"])


class TestWithMtime(unittest.TestCase):
    def test_real_path_gets_a_float_mtime(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        real = os.path.join(tmp, "x")
        open(real, "w").write("x")
        rows = scan._with_mtime([{"path": real}])
        self.assertIsInstance(rows[0]["mtime"], float)

    def test_nonexistent_path_gets_none(self):
        rows = scan._with_mtime([{"path": "/nonexistent/path/does-not-exist-xyz"}])
        self.assertIsNone(rows[0]["mtime"])


class TestCanonicalPath(unittest.TestCase):
    def test_data_volume_prefix_is_stripped(self):
        self.assertEqual(
            scan.canonical_path("/System/Volumes/Data/Users/me/.npm"),
            "/Users/me/.npm")

    def test_bare_data_volume_becomes_root(self):
        self.assertEqual(scan.canonical_path("/System/Volumes/Data"), "/")

    def test_other_volumes_are_untouched(self):
        self.assertEqual(
            scan.canonical_path("/Volumes/External/proj/node_modules"),
            "/Volumes/External/proj/node_modules")

    def test_already_canonical_path_is_untouched(self):
        self.assertEqual(scan.canonical_path("/Users/me/.cache/uv"), "/Users/me/.cache/uv")

    def test_similar_prefix_is_not_stripped(self):
        self.assertEqual(
            scan.canonical_path("/System/Volumes/DataBackup/x"),
            "/System/Volumes/DataBackup/x")


class TestParseLastUsed(unittest.TestCase):
    def test_parses_a_real_timestamp(self):
        self.assertEqual(scan.parse_last_used("2026-06-13 20:28:17 +0000"), "2026-06-13")

    def test_null_becomes_unknown_not_never(self):
        self.assertIsNone(scan.parse_last_used("(null)"))

    def test_empty_becomes_unknown(self):
        self.assertIsNone(scan.parse_last_used(""))


class TestParseSnapshots(unittest.TestCase):
    def test_no_snapshots(self):
        self.assertEqual(scan.parse_snapshots("Snapshots for disk /:\n"), [])

    def test_lists_snapshot_names(self):
        out = ("Snapshots for disk /:\n"
               "com.apple.TimeMachine.2026-09-16-120000.local\n"
               "com.apple.TimeMachine.2026-09-17-120000.local\n")
        self.assertEqual(len(scan.parse_snapshots(out)), 2)


class TestCliArgs(unittest.TestCase):
    def test_root_flag_overrides_discovery(self):
        args = scan.parse_args(["--root", "/tmp/a", "--root", "/tmp/b"])
        self.assertEqual(args.root, ["/tmp/a", "/tmp/b"])

    def test_refresh_defaults_off(self):
        self.assertFalse(scan.parse_args([]).refresh)

    def test_depth_defaults_to_six(self):
        self.assertEqual(scan.parse_args([]).depth, 6)


class TestArtifactManifests(unittest.TestCase):
    def test_node_modules_is_proven_by_package_json(self):
        self.assertIn("package.json", scan.ARTIFACT_MANIFESTS["node_modules"])

    def test_cargo_target_is_proven_by_cargo_toml(self):
        self.assertIn("Cargo.toml", scan.ARTIFACT_MANIFESTS["target"])

    def test_venv_is_proven_by_a_python_manifest(self):
        self.assertTrue(
            {"pyproject.toml", "requirements.txt"} & set(scan.ARTIFACT_MANIFESTS[".venv"]))


class TestGitDirty(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_non_repo_is_reported_clean_not_unknown(self):
        # Nothing to lose: no version control means no uncommitted work.
        self.assertIs(scan.git_dirty(self.tmp), False)

    def test_clean_repo_is_false(self):
        subprocess.run(["git", "init", "-q", self.tmp], check=True)
        self.assertIs(scan.git_dirty(self.tmp), False)

    def test_repo_with_untracked_changes_is_true(self):
        subprocess.run(["git", "init", "-q", self.tmp], check=True)
        open(os.path.join(self.tmp, "new.txt"), "w").write("x")
        self.assertIs(scan.git_dirty(self.tmp), True)

    def test_repo_config_cannot_run_a_command(self):
        # Scanned repos are untrusted (an extracted archive can carry .git/config):
        # a repo-local fsmonitor hook must not execute.
        subprocess.run(["git", "init", "-q", self.tmp], check=True)
        marker = os.path.join(self.tmp, "ran")
        subprocess.run(["git", "-C", self.tmp, "config", "core.fsmonitor", "touch '%s'" % marker], check=True)
        scan.git_dirty(self.tmp)
        self.assertFalse(os.path.exists(marker))

    def commit_one_file(self):
        subprocess.run(["git", "init", "-q", self.tmp], check=True)
        open(os.path.join(self.tmp, "f.txt"), "w").write("a")
        subprocess.run(["git", "-C", self.tmp, "add", "f.txt"], check=True)
        subprocess.run(["git", "-C", self.tmp, "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "init"], check=True)

    def test_repo_clean_filter_cannot_run_a_command(self):
        # `git status` re-hashes a stat-changed file through the repo's clean filter.
        self.commit_one_file()
        marker = os.path.join(self.tmp, "..", os.path.basename(self.tmp) + "-ran")
        self.addCleanup(lambda: os.path.exists(marker) and os.remove(marker))
        open(os.path.join(self.tmp, ".git", "info", "attributes"), "w").write("* filter=x\n")
        subprocess.run(["git", "-C", self.tmp, "config", "filter.x.clean", "touch '%s'; cat" % marker], check=True)
        os.utime(os.path.join(self.tmp, "f.txt"), (0, 0))
        scan.git_dirty(self.tmp)
        self.assertFalse(os.path.exists(marker))

    def test_committed_clean_repo_is_false(self):
        self.commit_one_file()
        subprocess.run(["git", "-C", self.tmp, "update-index", "--refresh", "-q"])
        self.assertIs(scan.git_dirty(self.tmp), False)

    def test_modified_tracked_file_is_true(self):
        self.commit_one_file()
        open(os.path.join(self.tmp, "f.txt"), "w").write("changed")
        self.assertIs(scan.git_dirty(self.tmp), True)

    def test_staged_change_is_true(self):
        self.commit_one_file()
        open(os.path.join(self.tmp, "g.txt"), "w").write("new")
        subprocess.run(["git", "-C", self.tmp, "add", "g.txt"], check=True)
        self.assertIs(scan.git_dirty(self.tmp), True)

    def test_staged_file_before_first_commit_is_true(self):
        subprocess.run(["git", "init", "-q", self.tmp], check=True)
        open(os.path.join(self.tmp, "g.txt"), "w").write("new")
        subprocess.run(["git", "-C", self.tmp, "add", "g.txt"], check=True)
        self.assertIs(scan.git_dirty(self.tmp), True)


class TestProjectFacts(unittest.TestCase):
    def setUp(self):
        # Created under HOME, not the OS temp dir: test_facts_feed_straight_into_
        # the_classifier exercises classify.is_project_artifact, which now
        # protects everything under /tmp and /var/folders (the OS temp prefix,
        # see classify._DENY) unconditionally. A fixture that lives there
        # would always be protected regardless of the facts it carries, which
        # defeats the point of that test - a real project lives under HOME.
        self.tmp = tempfile.mkdtemp(dir=os.path.expanduser("~"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.artifact = os.path.join(self.tmp, "node_modules")
        os.mkdir(self.artifact)

    def test_missing_sibling_manifest_is_reported(self):
        f = scan.project_facts(self.artifact, now_ts=time.time())
        self.assertFalse(f["has_sibling_manifest"])

    def test_sibling_manifest_is_detected(self):
        open(os.path.join(self.tmp, "package.json"), "w").write("{}")
        f = scan.project_facts(self.artifact, now_ts=time.time())
        self.assertTrue(f["has_sibling_manifest"])

    def test_age_is_measured_from_the_project_not_the_artifact(self):
        manifest = os.path.join(self.tmp, "package.json")
        open(manifest, "w").write("{}")
        old = time.time() - 60 * 86400
        os.utime(manifest, (old, old))
        os.utime(self.tmp, (old, old))
        f = scan.project_facts(self.artifact, now_ts=time.time())
        self.assertGreaterEqual(f["age_days"], 59)

    def test_facts_feed_straight_into_the_classifier(self):
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import classify
        open(os.path.join(self.tmp, "package.json"), "w").write("{}")
        old = time.time() - 60 * 86400
        os.utime(os.path.join(self.tmp, "package.json"), (old, old))
        os.utime(self.tmp, (old, old))
        f = scan.project_facts(self.artifact, now_ts=time.time())
        self.assertTrue(classify.is_project_artifact(self.artifact, f))


DOCKER_DF_REFERENCE = """TYPE            TOTAL     ACTIVE    SIZE      RECLAIMABLE
Images          12        3         8.2GB     5.1GB (62%)
Containers      5         1         1.2MB     1.1MB (91%)
Local Volumes   3         1         2.0GB     1.5GB (75%)
Build Cache     0         0         0B        0B
"""

# Images is deliberately the largest figure here (1.9TB) so a bug that
# let it leak into the total would be impossible to miss.
DOCKER_DF_BIG_BUILD_CACHE = """TYPE            TOTAL     ACTIVE    SIZE      RECLAIMABLE
Images          400       2         2.0TB     1.9TB (95%)
Containers      2         0         500MB     0B
Local Volumes   1         0         10GB      10GB (100%)
Build Cache     12        0         40GB      38GB
"""


class TestParseDockerDf(unittest.TestCase):
    def test_reference_table_counts_only_containers_and_build_cache(self):
        # 1.1MB (Containers) + 0B (Build Cache); not the 5.1GB Images figure
        # and not the 1.5GB Local Volumes figure.
        expected = int(1.1 * 1000 ** 2)
        self.assertEqual(scan.parse_docker_df(DOCKER_DF_REFERENCE), expected)

    def test_images_column_is_excluded(self):
        # If Images' 5.1GB leaked in, the total would blow far past this.
        self.assertLess(scan.parse_docker_df(DOCKER_DF_REFERENCE), 10 * 1000 ** 2)

    def test_local_volumes_alone_contributes_nothing(self):
        text = ("TYPE            TOTAL     ACTIVE    SIZE      RECLAIMABLE\n"
                "Local Volumes   3         1         2.0GB     1.5GB (75%)\n")
        self.assertEqual(scan.parse_docker_df(text), 0)

    def test_huge_images_figure_excluded_next_to_a_real_build_cache(self):
        # Only Build Cache's 38GB should count, not the 1.9TB Images figure
        # nor the 10GB Local Volumes figure.
        self.assertEqual(scan.parse_docker_df(DOCKER_DF_BIG_BUILD_CACHE), 38 * 1000 ** 3)

    def test_percentage_suffix_does_not_change_the_result(self):
        with_pct = "Containers      1         0         5MB       3MB (60%)\n"
        without_pct = "Containers      1         0         5MB       3MB\n"
        self.assertEqual(scan.parse_docker_df(with_pct), scan.parse_docker_df(without_pct))
        self.assertEqual(scan.parse_docker_df(with_pct), 3 * 1000 ** 2)

    def test_empty_text_returns_zero(self):
        self.assertEqual(scan.parse_docker_df(""), 0)

    def test_none_returns_zero_not_raise(self):
        self.assertEqual(scan.parse_docker_df(None), 0)

    def test_malformed_text_returns_zero_not_raise(self):
        self.assertEqual(scan.parse_docker_df("not a docker table\njust\tsome\tnoise\n"), 0)

    def test_header_only_returns_zero(self):
        header = "TYPE            TOTAL     ACTIVE    SIZE      RECLAIMABLE\n"
        self.assertEqual(scan.parse_docker_df(header), 0)

    def test_bare_b_unit_is_handled(self):
        self.assertEqual(scan.parse_docker_df("Containers 1 0 10B 7B\n"), 7)

    def test_kb_unit_is_handled(self):
        self.assertEqual(scan.parse_docker_df("Build Cache 1 0 5kB 2kB\n"), 2000)

    def test_tb_unit_is_handled(self):
        self.assertEqual(scan.parse_docker_df("Build Cache 1 0 5TB 2TB\n"), 2 * 1000 ** 4)


class TestDockerPass(unittest.TestCase):
    def setUp(self):
        self._old_path = os.environ.get("PATH", "")
        self.addCleanup(self._restore_path)

    def _restore_path(self):
        os.environ["PATH"] = self._old_path

    def _fake_docker(self, stdout_text=None, stderr_text=None, exit_code=0):
        """Put a fake `docker` script on PATH so tests never depend on
        whether real Docker is installed or its daemon is running.

        bindir is PREPENDED to the real PATH, not a full replacement: the
        script's "cat" calls need /bin still reachable, and prepending
        guarantees this fake `docker` shadows any real one.
        """
        bindir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, bindir, ignore_errors=True)
        lines = ["#!/bin/sh"]
        if stdout_text is not None:
            Path(bindir, "stdout.txt").write_text(stdout_text)
            lines.append('cat "%s/stdout.txt"' % bindir)
        if stderr_text is not None:
            Path(bindir, "stderr.txt").write_text(stderr_text)
            lines.append('cat "%s/stderr.txt" >&2' % bindir)
        lines.append("exit %d" % exit_code)
        script = Path(bindir, "docker")
        script.write_text("\n".join(lines) + "\n")
        os.chmod(str(script), 0o755)
        os.environ["PATH"] = bindir + os.pathsep + self._old_path

    def test_missing_binary_is_unavailable_and_does_not_raise(self):
        empty_bindir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty_bindir, ignore_errors=True)
        os.environ["PATH"] = empty_bindir
        result = scan.docker_pass()
        self.assertEqual(result, {"available": False,
                                   "reason": "docker not found on PATH", "bytes": 0})

    def test_failing_daemon_is_unavailable_with_a_reason_and_zero_bytes(self):
        self._fake_docker(stderr_text="Cannot connect to the Docker daemon\n", exit_code=1)
        result = scan.docker_pass()
        self.assertFalse(result["available"])
        self.assertEqual(result["bytes"], 0)
        self.assertEqual(result["reason"], "Cannot connect to the Docker daemon")

    def test_successful_call_is_available_and_parses_stdout(self):
        self._fake_docker(stdout_text=DOCKER_DF_REFERENCE, exit_code=0)
        result = scan.docker_pass()
        self.assertEqual(result, {"available": True, "reason": "",
                                   "bytes": scan.parse_docker_df(DOCKER_DF_REFERENCE)})

    def test_never_raises_regardless_of_environment(self):
        # This machine's real docker (if any) is exercised here, daemon
        # up or down - docker_pass must not raise either way.
        try:
            result = scan.docker_pass()
        except Exception as exc:  # noqa: BLE001 - the point is that nothing escapes
            self.fail("docker_pass() raised %r" % exc)
        self.assertIn("available", result)
        self.assertIn("bytes", result)
        if not result["available"]:
            self.assertEqual(result["bytes"], 0)


# ---------------------------------------------------------------------------
# TCC - full_disk_access() probes Safari and Trash, never raises, never
# guesses which ~/Library sources will actually fail (TCC overrides
# os.access, so there is no POSIX pre-flight test for that - see scan.py's
# own docstring). It only answers whether the grant exists at all.
# ---------------------------------------------------------------------------

class TestFullDiskAccess(unittest.TestCase):
    def test_returns_a_well_formed_dict(self):
        result = scan.full_disk_access()
        self.assertIsInstance(result, dict)
        self.assertIn("granted", result)
        self.assertIn("probed", result)
        self.assertIsInstance(result["granted"], bool)
        self.assertIsInstance(result["probed"], list)
        self.assertEqual(len(result["probed"]), 2)
        for entry in result["probed"]:
            self.assertIn("path", entry)
            self.assertIn("readable", entry)

    def test_probes_safari_and_trash_specifically(self):
        result = scan.full_disk_access()
        paths = " ".join(p["path"] for p in result["probed"])
        self.assertIn("Safari", paths)
        self.assertIn(".Trash", paths)

    def test_never_raises_on_a_real_machine(self):
        # Exercised for real, granted or not - the point is it never raises.
        try:
            result = scan.full_disk_access()
        except Exception as exc:  # noqa: BLE001 - the point is nothing escapes
            self.fail("full_disk_access() raised %r" % exc)
        self.assertIn(result["granted"], (True, False))

    def test_never_raises_when_both_probes_are_permission_denied(self):
        with mock.patch("os.listdir", side_effect=PermissionError("Operation not permitted")):
            try:
                result = scan.full_disk_access()
            except Exception as exc:  # noqa: BLE001
                self.fail("full_disk_access() raised %r" % exc)
        self.assertFalse(result["granted"])
        self.assertEqual(len(result["probed"]), 2)
        self.assertTrue(all(not p["readable"] for p in result["probed"]))

    def test_never_raises_when_a_probe_path_is_missing(self):
        with mock.patch("os.listdir", side_effect=FileNotFoundError("no such file")):
            try:
                result = scan.full_disk_access()
            except Exception as exc:  # noqa: BLE001
                self.fail("full_disk_access() raised %r" % exc)
        self.assertFalse(result["granted"])

    def test_granted_true_when_every_probe_succeeds(self):
        with mock.patch("os.listdir", return_value=[]):
            result = scan.full_disk_access()
        self.assertTrue(result["granted"])

    def test_granted_false_when_only_one_of_two_probes_succeeds(self):
        calls = {"n": 0}

        def flaky(path):
            calls["n"] += 1
            if calls["n"] == 1:
                return []
            raise PermissionError("nope")

        with mock.patch("os.listdir", side_effect=flaky):
            result = scan.full_disk_access()
        self.assertFalse(result["granted"])

    def test_failed_probe_carries_the_error_text(self):
        with mock.patch("os.listdir", side_effect=PermissionError("Operation not permitted")):
            result = scan.full_disk_access()
        self.assertIn("error", result["probed"][0])
        self.assertIn("Operation not permitted", result["probed"][0]["error"])
