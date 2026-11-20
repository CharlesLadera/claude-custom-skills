"""Windows support: path handling, the scandir walk, the apps pass, the
Windows manifest, and the report's Windows wording.

Pure functions are tested on every OS. Tests that need a real NTFS volume
(hard links, junctions, cluster sizes) are skipped elsewhere.
"""

import codecs
import contextlib
import io
import json
import os
import struct
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import classify  # noqa: E402
import scan  # noqa: E402

ON_WINDOWS = sys.platform.startswith("win")


def _st(size, attrs=0):
    return types.SimpleNamespace(st_size=size, st_file_attributes=attrs)


class TestWinPath(unittest.TestCase):
    def test_backslashes_become_forward_slashes(self):
        self.assertEqual(scan.win_path(r"C:\Users\me\AppData"), "C:/Users/me/AppData")

    def test_bare_drive_becomes_its_root(self):
        self.assertEqual(scan.win_path("C:"), "C:/")
        self.assertEqual(scan.win_path("D:\\"), "D:/")

    def test_already_forward_slashed_path_is_unchanged(self):
        self.assertEqual(scan.win_path("C:/Program Files"), "C:/Program Files")


class TestCachePathOnWindowsRoots(unittest.TestCase):
    def test_drive_root_slug_has_no_colon(self):
        # ":" is illegal in a Windows file name; "du_C:.json" cannot be written.
        p = scan.cache_path("C:/", "/tmp/cache")
        self.assertEqual(p.name, "du_C.json")

    def test_nested_windows_root_slug_has_no_colon(self):
        self.assertNotIn(":", scan.cache_path("D:/Games", "/tmp/cache").name)


class TestWinAllocatedSize(unittest.TestCase):
    def test_plain_file_rounds_up_to_whole_clusters(self):
        self.assertEqual(scan.win_allocated_size("x", _st(1), cluster=4096), 4096)
        self.assertEqual(scan.win_allocated_size("x", _st(4097), cluster=4096), 8192)

    def test_exact_multiple_is_not_rounded_further(self):
        self.assertEqual(scan.win_allocated_size("x", _st(8192), cluster=4096), 8192)

    def test_empty_file_occupies_nothing(self):
        self.assertEqual(scan.win_allocated_size("x", _st(0)), 0)

    def test_plain_file_never_calls_the_probe(self):
        def boom(path):
            raise AssertionError("probe called for a plain file")
        scan.win_allocated_size("x", _st(100), measure=boom)

    def test_compressed_file_uses_the_measured_size(self):
        size = scan.win_allocated_size("x", _st(10_000_000, scan._FA_COMPRESSED),
                                       measure=lambda p: 3_000_000, cluster=4096)
        self.assertEqual(size, 3_002_368)  # 3,000,000 rounded up to 4K

    def test_cloud_placeholder_that_is_not_downloaded_counts_zero(self):
        size = scan.win_allocated_size("x", _st(5_000_000_000, scan._FA_RECALL_ON_DATA_ACCESS),
                                       measure=lambda p: 0)
        self.assertEqual(size, 0)

    def test_failed_probe_falls_back_to_the_length(self):
        size = scan.win_allocated_size("x", _st(4096, scan._FA_SPARSE),
                                       measure=lambda p: None, cluster=4096)
        self.assertEqual(size, 4096)


@unittest.skipUnless(ON_WINDOWS, "needs a real NTFS volume")
class TestWinWalk(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)
        self.cluster = scan.win_cluster_size(str(self.root))

    def _cleanup(self):
        import shutil
        junction = self.root / "link"
        if junction.exists():
            os.rmdir(junction)  # removes the junction itself, never its target
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, rel, size):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
        return p

    def _rows(self, result):
        return {r["path"]: r["bytes"] for r in result["rows"]}

    def _root(self):
        return scan.win_path(str(self.root))

    def test_paths_are_forward_slashed_and_root_row_totals_the_tree(self):
        self._write("a/one.bin", 10_000)
        self._write("b/two.bin", 20_000)
        rows = self._rows(scan.win_walk(str(self.root), depth=3))
        root = self._root()
        self.assertIn(root, rows)
        self.assertIn(root + "/a", rows)
        self.assertTrue(all("\\" not in p for p in rows))
        expected = scan.win_allocated_size("", _st(10_000), cluster=self.cluster) + \
            scan.win_allocated_size("", _st(20_000), cluster=self.cluster)
        self.assertEqual(rows[root], expected)

    def test_rows_stop_at_depth_but_deeper_bytes_roll_up(self):
        self._write("a/b/c/deep.bin", 50_000)
        rows = self._rows(scan.win_walk(str(self.root), depth=1))
        root = self._root()
        self.assertNotIn(root + "/a/b", rows)
        self.assertGreaterEqual(rows[root + "/a"], 50_000)

    def test_hard_linked_file_is_counted_once(self):
        original = self._write("a/big.bin", 100_000)
        (self.root / "b").mkdir()
        os.link(str(original), str(self.root / "b" / "big-link.bin"))
        rows = self._rows(scan.win_walk(str(self.root), depth=2))
        once = scan.win_allocated_size("", _st(100_000), cluster=self.cluster)
        self.assertEqual(rows[self._root()], once)

    def test_junction_is_not_followed(self):
        import _winapi
        target = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(target, ignore_errors=True))
        (target / "elsewhere.bin").write_bytes(b"x" * 200_000)
        _winapi.CreateJunction(str(target), str(self.root / "link"))
        rows = self._rows(scan.win_walk(str(self.root), depth=2))
        self.assertNotIn(self._root() + "/link", rows)
        self.assertEqual(rows[self._root()], 0)

    def test_large_files_are_listed_with_allocated_and_apparent_sizes(self):
        self._write("a/large.bin", 300_000)
        self._write("a/small.bin", 10)
        result = scan.win_walk(str(self.root), depth=2, min_file_bytes=200_000)
        self.assertEqual([f["path"] for f in result["files"]], [self._root() + "/a/large.bin"])
        f = result["files"][0]
        self.assertEqual(f["apparent_bytes"], 300_000)
        self.assertIn("mtime", f)

    def test_outermost_artifact_found_even_below_depth_nested_one_is_not(self):
        self._write("p/q/r/proj/node_modules/x/node_modules/y/index.js", 5_000)
        result = scan.win_walk(str(self.root), depth=1)
        paths = [a["path"] for a in result["artifacts"]]
        self.assertEqual(paths, [self._root() + "/p/q/r/proj/node_modules"])
        self.assertGreater(result["artifacts"][0]["bytes"], 0)

    def test_paths_past_max_path_are_walked_not_dropped(self):
        # 285-char paths were miscounted as "vanished mid-scan" before the
        # extended-length prefix; they must be measured like any other.
        # Built through the extended-length prefix too: without long-path
        # support enabled, plain mkdir fails past MAX_PATH just as the walk did.
        deep = "\\\\?\\" + str(self.root / "\\".join(["d" * 60] * 5))
        os.makedirs(deep)
        with open(deep + "\\f.bin", "wb") as fh:
            fh.write(b"x" * 70_000)
        self.addCleanup(__import__("shutil").rmtree, "\\\\?\\" + str(self.root / ("d" * 60)),
                        ignore_errors=True)
        result = scan.win_walk(str(self.root), depth=1)
        self.assertEqual(result["transient_count"], 0)
        self.assertGreaterEqual(self._rows(result)[self._root()], 70_000)

    def test_missing_root_counts_as_transient_not_a_crash(self):
        result = scan.win_walk(str(self.root / "nope"), depth=1)
        self.assertEqual(result["transient_count"], 1)


class TestProjectPassFromWalk(unittest.TestCase):
    def test_found_artifacts_are_judged_without_a_second_walk(self):
        proj = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(proj, ignore_errors=True))
        (proj / "package.json").write_text("{}")
        (proj / "node_modules").mkdir()
        old = time.time() - 90 * 86400
        os.utime(proj / "package.json", (old, old))
        os.utime(proj, (old, old))
        path = str(proj / "node_modules").replace("\\", "/")
        rows = scan.project_pass("unused", found=[{"path": path, "bytes": 1234}])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bytes"], 1234)
        self.assertGreaterEqual(rows[0]["age_days"], 89)
        # Old, clean, with a manifest: it qualifies - unless the temp dir sits
        # under a denylisted tree, as Windows' %TEMP% (under AppData) does.
        self.assertEqual(rows[0]["qualifies"], classify.is_protected(path) is None)


def _userassist_record(unix_ts):
    data = bytearray(72)
    filetime = int((unix_ts + scan._FILETIME_UNIX_OFFSET) * 1e7)
    struct.pack_into("<Q", data, 60, filetime)
    return bytes(data)


class TestUserAssist(unittest.TestCase):
    ENV = {"ProgramW6432": "C:\\Program Files", "ProgramFiles(x86)": "C:\\Program Files (x86)",
           "windir": "C:\\Windows"}

    def test_rot13_name_decodes_and_known_folder_expands(self):
        raw = codecs.encode("{6D809377-6AF0-444B-8957-A3773F02200E}\\Docker\\Docker Desktop.exe",
                            "rot_13")
        self.assertEqual(scan.parse_userassist_name(raw, self.ENV),
                         "C:/Program Files/Docker/Docker Desktop.exe")

    def test_plain_path_name_decodes(self):
        raw = codecs.encode("C:\\Users\\me\\AppData\\Local\\Programs\\App\\app.exe", "rot_13")
        self.assertEqual(scan.parse_userassist_name(raw, self.ENV),
                         "C:/Users/me/AppData/Local/Programs/App/app.exe")

    def test_unknown_folder_variable_leaves_the_guid(self):
        raw = codecs.encode("{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}\\x.exe", "rot_13")
        self.assertTrue(scan.parse_userassist_name(raw, {}).startswith("{7C5A40EF"))

    def test_record_yields_its_last_run_time(self):
        ts = 1_790_000_000
        self.assertAlmostEqual(scan.parse_userassist_data(_userassist_record(ts)), ts, places=3)

    def test_short_or_empty_record_is_no_launch(self):
        self.assertIsNone(scan.parse_userassist_data(b"\x00" * 16))
        self.assertIsNone(scan.parse_userassist_data(None))
        self.assertIsNone(scan.parse_userassist_data(bytes(72)))


class TestWinAppRows(unittest.TestCase):
    def _entry(self, name, location=None, est=None, **extra):
        e = {"DisplayName": name, "InstallLocation": location, "EstimatedSize": est,
             "SystemComponent": None, "ParentKeyName": None, "ReleaseType": None}
        e.update(extra)
        return e

    def test_measured_install_directory_beats_the_estimate(self):
        rows = scan.win_app_rows(
            [self._entry("Docker Desktop", "C:\\Program Files\\Docker\\Docker", est=10)],
            {"c:/program files/docker/docker": 3_000_000_000}, {})
        self.assertEqual(rows[0]["bytes"], 3_000_000_000)
        self.assertEqual(rows[0]["size_source"], "measured")
        self.assertEqual(rows[0]["path"], "C:/Program Files/Docker/Docker")

    def test_estimate_in_kilobytes_is_the_fallback(self):
        rows = scan.win_app_rows([self._entry("SDK", est=2048)], {}, {})
        self.assertEqual(rows[0]["bytes"], 2048 * 1024)
        self.assertEqual(rows[0]["size_source"], "estimated")
        self.assertEqual(rows[0]["path"], "SDK")

    def test_entries_with_no_size_no_name_or_that_are_updates_are_dropped(self):
        rows = scan.win_app_rows([
            self._entry("No Size"),
            self._entry("", est=100),
            self._entry("Hidden", est=100, SystemComponent=1),
            self._entry("KB123 Update", est=100, ParentKeyName="Office"),
        ], {}, {})
        self.assertEqual(rows, [])

    def test_last_used_is_the_newest_launch_inside_the_install_directory(self):
        launches = {"c:/games/foo/foo.exe": 1_700_000_000,
                    "c:/games/foo/bin/helper.exe": 1_790_000_000,
                    "c:/games/foobar/foobar.exe": 1_795_000_000}  # sibling, not inside
        rows = scan.win_app_rows([self._entry("Foo", "C:/Games/Foo", est=1)], {}, launches)
        expected = time.strftime("%Y-%m-%d", time.localtime(1_790_000_000))
        self.assertEqual(rows[0]["last_used"], expected)

    def test_display_icon_supplies_a_missing_install_location(self):
        rows = scan.win_app_rows(
            [self._entry("Brave", None, est=1,
                         DisplayIcon='"C:\\Program Files\\BraveSoftware\\Brave-Browser\\Application\\brave.exe",0')],
            {"c:/program files/bravesoftware/brave-browser/application": 600_000_000}, {})
        self.assertEqual(rows[0]["path"], "C:/Program Files/BraveSoftware/Brave-Browser/Application")
        self.assertEqual(rows[0]["size_source"], "measured")

    def test_icon_that_is_not_an_exe_path_is_ignored(self):
        rows = scan.win_app_rows([self._entry("X", None, est=1, DisplayIcon="C:\\x\\app.ico")], {}, {})
        self.assertEqual(rows[0]["path"], "X")

    def test_taskbar_shortcut_name_counts_as_a_launch(self):
        launches = {"{9e3995ab-1f9c-4f13-b827-48b24b6c7174}/taskbar/visual studio code.lnk": 1_790_000_000}
        rows = scan.win_app_rows([self._entry("Microsoft Visual Studio Code (User)", est=1)],
                                 {}, launches)
        self.assertEqual(rows[0]["last_used"],
                         time.strftime("%Y-%m-%d", time.localtime(1_790_000_000)))

    def test_shortcut_must_match_whole_words(self):
        launches = {"x/taskbar/code.lnk": 1_790_000_000, "x/ride.lnk": 1_790_000_000}
        rows = scan.win_app_rows([self._entry("Unicode Tools", est=1),
                                  self._entry("RIDE 4", est=1)], {}, launches)
        by = {r["name"]: r["last_used"] for r in rows}
        self.assertIsNone(by["Unicode Tools"])  # "code" is not a word in "unicode"
        self.assertIsNotNone(by["RIDE 4"])

    def test_no_launch_is_none_never_a_guess(self):
        rows = scan.win_app_rows([self._entry("Foo", "C:/Games/Foo", est=1)], {}, {})
        self.assertIsNone(rows[0]["last_used"])

    def test_same_app_registered_twice_appears_once_at_the_larger_size(self):
        rows = scan.win_app_rows([self._entry("Git", est=10), self._entry("git", est=50)], {}, {})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bytes"], 50 * 1024)


class TestWindowsProtectedPaths(unittest.TestCase):
    def test_system_paths_are_protected_in_any_case(self):
        for p in ("C:/Windows", "C:/Windows/WinSxS/x", "c:/windows/system32",
                  "C:/pagefile.sys", "C:/hiberfil.sys", "C:/System Volume Information/x",
                  "C:/Program Files/WindowsApps/Some.App"):
            self.assertIsNotNone(classify.is_protected(p), p)

    def test_vm_disk_images_are_protected(self):
        for p in ("C:/Users/me/AppData/Local/Docker/wsl/disk/docker_data.vhdx",
                  "D:/VMs/old.VHD"):
            self.assertIsNotNone(classify.is_protected(p), p)

    def test_tool_and_app_dependencies_are_protected(self):
        for p in ("C:/Program Files/Some App/resources/app/node_modules",
                  "C:/Users/me/AppData/Roaming/npm/node_modules",
                  "C:/Users/me/AppData/Local/Programs/Thing/resources/node_modules",
                  "C:/Users/me/.vscode/extensions/ext/node_modules",
                  "C:/Users/me/.local/share/tool/node_modules",
                  "C:/Users/me/scoop/apps/nodejs/current"):
            self.assertIsNotNone(classify.is_protected(p), p)

    def test_cloud_synced_folders_are_protected(self):
        for p in ("C:/Users/me/OneDrive/Photos", "C:/Users/me/OneDrive - Work/x"):
            self.assertIsNotNone(classify.is_protected(p), p)

    def test_a_users_own_project_artifact_is_not_protected(self):
        self.assertIsNone(classify.is_protected(
            "C:/Users/me/Documents/coding_projects/app/node_modules"))

    def test_manifest_cache_roots_are_not_protected(self):
        for p in ("C:/Users/me/AppData/Local/npm-cache", "C:/Users/me/AppData/Local/Temp",
                  "C:/Users/me/AppData/Local/NVIDIA/DXCache"):
            self.assertIsNone(classify.is_protected(p), p)


class TestWindowsClassifyRow(unittest.TestCase):
    def setUp(self):
        self._home = classify.HOME
        classify.HOME = "C:/Users/me"
        self.addCleanup(setattr, classify, "HOME", self._home)
        self.manifest = [{"id": "npm-cache", "path": "~/AppData/Local/npm-cache", "tier": "cheap",
                          "why": "w", "regen": "r", "command": "npm cache clean --force"}]

    def test_drive_paths_match_the_manifest_case_insensitively(self):
        row = classify.classify_row("C:/Users/me/AppData/Local/NPM-Cache/_cacache", self.manifest)
        self.assertEqual(row["tier"], "cheap")

    def test_posix_paths_still_match_case_sensitively(self):
        classify.HOME = "/Users/me"
        row = classify.classify_row("/Users/me/AppData/Local/NPM-Cache", self.manifest)
        self.assertEqual(row["action"], "review only")

    def test_sibling_with_a_shared_prefix_does_not_match(self):
        row = classify.classify_row("C:/Users/me/AppData/Local/npm-cache-old", self.manifest)
        self.assertEqual(row["action"], "review only")


class TestDriveRootNeverSwallowsTheTree(unittest.TestCase):
    def test_drive_root_row_does_not_hide_its_children(self):
        rows = [{"path": "C:/", "bytes": 100}, {"path": "C:/Users", "bytes": 60},
                {"path": "C:/Windows", "bytes": 30}]
        kept = {r["path"] for r in classify.drop_nested(rows)}
        self.assertEqual(kept, {"C:/", "C:/Users", "C:/Windows"})

    def test_ordinary_windows_parent_still_absorbs_its_child(self):
        rows = [{"path": "C:/Users", "bytes": 60}, {"path": "C:/Users/me", "bytes": 50}]
        self.assertEqual([r["path"] for r in classify.drop_nested(rows)], ["C:/Users"])


class TestWindowsRowMethod(unittest.TestCase):
    def test_recycle_bin_is_emptied_by_the_user(self):
        self.assertEqual(classify.row_method("proposed", "free", "recycle-bin", None, "windows"),
                         "empty Recycle Bin yourself")

    def test_manifest_command_wins(self):
        self.assertEqual(classify.row_method("proposed", "cheap", "npm-cache",
                                             "npm cache clean --force", "windows"),
                         "npm cache clean --force")

    def test_cheap_without_command_is_a_permanent_delete_by_the_user(self):
        self.assertEqual(classify.row_method("proposed", "cheap", "x", None, "windows"),
                         "delete yourself (permanent)")

    def test_free_and_project_rows_go_to_the_recycle_bin_by_the_user(self):
        for tier, action in (("free", "proposed"), ("project", "ask"), ("costly", "ask")):
            self.assertEqual(classify.row_method(action, tier, "x", None, "windows"),
                             "Recycle Bin yourself")

    def test_review_only_rows_carry_no_method(self):
        self.assertIsNone(classify.row_method("review only", None, None, None, "windows"))

    def test_default_os_keeps_the_macos_vocabulary(self):
        self.assertEqual(classify.row_method("proposed", "free", "x", None), "tra" + "sh")


class TestWindowsRenderApps(unittest.TestCase):
    def test_display_name_is_shown_and_estimates_are_marked(self):
        out = classify.render_apps([
            {"name": "Docker Desktop", "path": "C:/Program Files/Docker/Docker",
             "bytes": 3 * 1024 ** 3, "size_source": "measured", "last_used": None},
            {"name": "Windows SDK", "path": "Windows SDK", "bytes": 2 * 1024 ** 3,
             "size_source": "estimated", "last_used": "2026-01-02"},
        ])
        self.assertIn("Docker Desktop", out)
        self.assertIn("~2.0G", out)
        self.assertIn("unknown", out)


class TestFullDiskAccessNotApplicable(unittest.TestCase):
    def test_not_applicable_is_silent_even_with_library_rows(self):
        rows = [{"path": classify.HOME + "/Library/Logs", "bytes": 1, "action": "proposed",
                 "root": "/"}]
        data = {"full_disk_access": {"applicable": False, "granted": None, "probed": []}}
        self.assertEqual(classify.full_disk_access_note(data, rows), "")


class TestWindowsMainIntegration(unittest.TestCase):
    def setUp(self):
        self._home = classify.HOME
        classify.HOME = "C:/Users/me"
        self.addCleanup(setattr, classify, "HOME", self._home)

    def _run(self):
        now = time.time()
        data = {
            "roots": [{"root": "C:/", "scanned_at": now - 60,
                       "rows": [{"path": "C:/", "bytes": 170 * 1024 ** 3, "mtime": now},
                                {"path": "C:/Users/me/AppData/Local/NVIDIA/DXCache",
                                 "bytes": 7 * 1024 ** 3, "mtime": now},
                                {"path": "C:/Users/me/AppData/Local/npm-cache",
                                 "bytes": 4 * 1024 ** 3, "mtime": now},
                                {"path": "C:/Windows", "bytes": 28 * 1024 ** 3, "mtime": now}],
                       "files": [{"path": "C:/pagefile.sys", "bytes": 18 * 1024 ** 3,
                                  "apparent_bytes": 18 * 1024 ** 3, "sparse": False,
                                  "mtime": now}],
                       "projects": [], "unreadable_count": 300, "transient_count": 2}],
            "apps": [{"name": "Steam", "path": "C:/Program Files (x86)/Steam",
                      "bytes": 2 * 1024 ** 3, "size_source": "measured", "last_used": None}],
            "snapshots": None, "snapshots_note": scan.WINDOWS_SNAPSHOT_NOTE,
            "mounts": [{"device": "C:", "mount": "C:/", "used_bytes": 200 * 1024 ** 3}],
            "docker": {"available": False, "reason": "daemon down", "bytes": 0},
            "full_disk_access": {"applicable": False, "granted": None, "probed": []},
        }
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(data, tmp)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            classify.main([tmp.name, "--os", "windows"])
        return buf.getvalue()

    def test_windows_manifest_rows_get_windows_methods(self):
        out = self._run()
        dx = next(l for l in out.splitlines() if "DXCache" in l)
        self.assertIn("Recycle Bin yourself", dx)
        npm = next(l for l in out.splitlines() if "npm-cache" in l)
        self.assertIn("npm cache clean --force", npm)

    def test_system_rows_are_protected_not_offered(self):
        out = self._run()
        for line in out.splitlines():
            if "pagefile.sys" in line or line.split()[2:3] == ["C:/Windows"]:
                self.assertNotIn("proposed", line)

    def test_footer_speaks_windows(self):
        out = self._run()
        self.assertIn("unreadable (need admin): 30G", out)  # 200G used - 170G seen
        self.assertIn("local snapshots: not checked - restore points need an administrator", out)
        self.assertIn("UserAssist", out)
        self.assertNotIn("Full Disk Access", out)


class TestWindowsManifestContent(unittest.TestCase):
    def setUp(self):
        data = json.loads((SCRIPTS / "manifests" / "windows.json").read_text())
        self.assertEqual(data["os"], "windows")
        self.entries = data["entries"]
        self.by_id = {e["id"]: e for e in self.entries}

    def test_every_entry_is_well_formed(self):
        for e in self.entries:
            for field in ("id", "path", "tier", "why", "regen"):
                self.assertIn(field, e, "entry %r missing %s" % (e.get("id"), field))
            self.assertIn(e["tier"], {"free", "cheap", "costly", "project"})
            self.assertTrue(e["why"].strip())

    def test_no_entry_targets_a_denylisted_path(self):
        for e in self.entries:
            expanded = e["path"].replace("~", "C:/Users/me")
            self.assertIsNone(classify.is_protected(expanded),
                              "manifest entry %s targets a protected path" % e["id"])

    def test_npm_cache_points_at_the_real_windows_location(self):
        # npm's Windows cache is %LocalAppData%\npm-cache, not Roaming.
        self.assertEqual(self.by_id["npm-cache"]["path"], "~/AppData/Local/npm-cache")

    def test_expensive_downloads_are_costly(self):
        for eid in ("whisper-models", "playwright-browsers", "uv-cache", "docker-images"):
            self.assertEqual(self.by_id[eid]["tier"], "costly", eid)

    def test_whisper_entry_names_the_dependent_skill(self):
        self.assertIn("transcribe-video", self.by_id["whisper-models"]["why"])

    def test_entries_with_a_command_are_never_batch_only_tiers(self):
        for e in self.entries:
            if e.get("command"):
                self.assertIn(e["tier"], ("cheap", "costly"))


if __name__ == "__main__":
    unittest.main()
