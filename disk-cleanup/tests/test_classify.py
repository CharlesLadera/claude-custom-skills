import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import classify  # noqa: E402


class TestIsProtected(unittest.TestCase):
    def test_editor_extension_dependencies_are_protected(self):
        p = "/Users/me/.vscode/extensions/esbenp.prettier-vscode-12.4.0/node_modules"
        self.assertIsNotNone(classify.is_protected(p))

    def test_mcp_server_dependencies_are_protected(self):
        p = "/Users/me/.config/claude/mcp-servers/monday/node_modules"
        self.assertIsNotNone(classify.is_protected(p))

    def test_npx_cache_is_protected(self):
        self.assertIsNotNone(classify.is_protected("/Users/me/.npm/_npx/b4c8/node_modules"))

    def test_vm_disk_images_are_protected(self):
        p = "/Users/me/Library/Application Support/Claude/vm_bundles/claudevm.bundle/rootfs.img"
        self.assertIsNotNone(classify.is_protected(p))

    def test_photo_and_mail_libraries_are_protected(self):
        self.assertIsNotNone(classify.is_protected("/Users/me/Pictures/Photos Library.photoslibrary"))
        self.assertIsNotNone(classify.is_protected("/Users/me/Library/Mail"))

    def test_system_volumes_are_protected(self):
        self.assertIsNotNone(classify.is_protected("/System/Volumes/VM/sleepimage"))

    def test_ordinary_cache_is_not_protected(self):
        self.assertIsNone(classify.is_protected("/Users/me/Library/Caches/BraveSoftware"))

    def test_protection_reason_is_human_readable(self):
        reason = classify.is_protected("/Users/me/.vscode/extensions/foo/node_modules")
        self.assertIn("extension", reason.lower())

    def test_project_own_config_node_modules_is_not_protected(self):
        self.assertIsNone(classify.is_protected(
            "/Users/me/projects/big-app/.config/webpack/node_modules"))

    def test_tool_config_node_modules_is_still_protected(self):
        self.assertIsNotNone(classify.is_protected(
            "/Users/me/.config/claude/mcp-servers/monday/node_modules"))

    def test_flat_config_node_modules_is_protected(self):
        self.assertIsNotNone(classify.is_protected("/Users/me/.config/node_modules"))

    def test_linux_home_config_node_modules_is_protected(self):
        self.assertIsNotNone(classify.is_protected("/home/me/.config/tool/node_modules"))

    def test_docker_raw_is_still_protected(self):
        self.assertIsNotNone(classify.is_protected(
            "/Users/me/Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw"))

    def test_media_raw_is_protected_without_calling_it_a_vm(self):
        reason = classify.is_protected("/Users/me/Pictures/shoot/IMG_0001.raw")
        self.assertIsNotNone(reason)
        self.assertNotIn("virtual machine", reason.lower())

    def test_traversal_into_a_protected_path_is_still_protected(self):
        self.assertIsNotNone(classify.is_protected(
            "/Users/me/Documents/../.vscode/extensions/foo/node_modules"))

    def test_temp_directory_is_not_a_cleanup_target(self):
        self.assertIsNotNone(classify.is_protected(
            "/private/var/folders/xy/abc123/T/tmp9kd0/node_modules"))

    def test_short_tmp_path_is_also_protected(self):
        self.assertIsNotNone(classify.is_protected("/tmp/scratch/node_modules"))

    def test_a_real_path_containing_tmp_later_is_not_protected(self):
        # Only a LEADING temp path counts; "tmp" inside a project name must not.
        self.assertIsNone(classify.is_protected("/Users/me/projects/tmp-tools/src"))


MANIFEST = [
    {"id": "user-trash", "path": "~/.Trash", "tier": "free",
     "why": "already discarded", "regen": "nothing", "command": None},
    {"id": "uv-cache", "path": "~/.cache/uv", "tier": "costly",
     "why": "python wheel cache", "regen": "re-downloads several GB", "command": None},
    {"id": "npm-cache", "path": "~/.npm", "tier": "cheap",
     "why": "npm package cache", "regen": "re-downloads per package",
     "command": "npm cache clean --force"},
]

# docker-images has no real filesystem path (its manifest path is the
# placeholder __command_only__docker), so it is kept out of the plain
# MANIFEST above - none of MANIFEST's path-lookup tests should be able
# to match it by accident. Tests of the docker row use this instead.
DOCKER_ENTRY = {
    "id": "docker-images", "path": "__command_only__docker", "tier": "costly",
    "why": "Dangling Docker images and stopped containers.",
    "regen": "Stopped containers are deleted outright and cannot be regenerated.",
    "command": "docker system prune"}
MANIFEST_WITH_DOCKER = MANIFEST + [DOCKER_ENTRY]


ROWS = [
    {"path": "/Users/me/Library/Caches/BraveSoftware", "bytes": 8_589_934_592,
     "age_days": 2, "tier": "free", "action": "proposed"},
    {"path": "/Users/me/.cache/uv", "bytes": 7_086_696_038,
     "age_days": 5, "tier": "costly", "action": "ask"},
    {"path": "/Volumes/External/GameLibrary", "bytes": 49_392_123_904,
     "age_days": 60, "tier": None, "action": "review only"},
]


def facts(sib=True, age=90, dirty=False):
    return {"has_sibling_manifest": sib, "age_days": age, "git_dirty": dirty}


class TestClassifyRow(unittest.TestCase):
    def setUp(self):
        classify.HOME = "/Users/me"

    def test_free_tier_entry_is_proposed(self):
        row = classify.classify_row("/Users/me/.Trash", MANIFEST)
        self.assertEqual(row["tier"], "free")
        self.assertEqual(row["action"], "proposed")

    def test_cheap_tier_entry_is_proposed(self):
        row = classify.classify_row("/Users/me/.npm", MANIFEST)
        self.assertEqual(row["action"], "proposed")

    def test_costly_tier_entry_must_be_asked_individually(self):
        row = classify.classify_row("/Users/me/.cache/uv", MANIFEST)
        self.assertEqual(row["tier"], "costly")
        self.assertEqual(row["action"], "ask")
        self.assertIn("GB", row["regen"])

    def test_unmatched_path_is_review_only(self):
        row = classify.classify_row("/Volumes/External/GameLibrary", MANIFEST)
        self.assertEqual(row["action"], "review only")
        self.assertIsNone(row["tier"])

    def test_protected_beats_manifest_match(self):
        manifest = MANIFEST + [{"id": "bad", "path": "~/Library/Mail", "tier": "free",
                                "why": "x", "regen": "y", "command": None}]
        row = classify.classify_row("/Users/me/Library/Mail", manifest)
        self.assertEqual(row["action"], "protected")

    def test_child_of_manifest_path_inherits_tier(self):
        row = classify.classify_row("/Users/me/.cache/uv/archive-v0", MANIFEST)
        self.assertEqual(row["tier"], "costly")

    def test_traversal_path_does_not_match_a_manifest_entry(self):
        row = classify.classify_row("/Users/me/.cache/uv/../../etc/passwd", MANIFEST)
        self.assertEqual(row["action"], "review only")


class TestBuildRows(unittest.TestCase):
    def setUp(self):
        classify.HOME = "/Users/me"
        self.now = 1_700_000_000.0

    def test_row_with_mtime_gets_sensible_age_days(self):
        scan_data = {"roots": [{
            "rows": [{"path": "/Users/me/.Trash", "bytes": 100,
                      "mtime": self.now - (5 * 86400)}],
            "files": [],
        }]}
        rows = classify.build_rows(scan_data, MANIFEST, self.now)
        self.assertEqual(rows[0]["age_days"], 5)

    def test_row_with_none_mtime_gets_negative_one(self):
        scan_data = {"roots": [{
            "rows": [{"path": "/Users/me/.Trash", "bytes": 100, "mtime": None}],
            "files": [],
        }]}
        rows = classify.build_rows(scan_data, MANIFEST, self.now)
        self.assertEqual(rows[0]["age_days"], -1)

    def test_rows_from_both_rows_and_files_are_included(self):
        scan_data = {"roots": [{
            "rows": [{"path": "/Users/me/.Trash", "bytes": 100, "mtime": self.now}],
            "files": [{"path": "/Users/me/big.img", "bytes": 200, "mtime": self.now}],
        }]}
        rows = classify.build_rows(scan_data, MANIFEST, self.now)
        paths = {r["path"] for r in rows}
        self.assertEqual(paths, {"/Users/me/.Trash", "/Users/me/big.img"})

    def test_unmatched_path_comes_through_as_review_only(self):
        scan_data = {"roots": [{
            "rows": [{"path": "/Volumes/External/GameLibrary", "bytes": 100,
                      "mtime": self.now}],
            "files": [],
        }]}
        rows = classify.build_rows(scan_data, MANIFEST, self.now)
        self.assertEqual(rows[0]["action"], "review only")


class TestManifestEntry(unittest.TestCase):
    def test_returns_the_matching_entry(self):
        entry = classify.manifest_entry(MANIFEST, "uv-cache")
        self.assertEqual(entry["tier"], "costly")

    def test_returns_none_for_an_unknown_id(self):
        self.assertIsNone(classify.manifest_entry(MANIFEST, "does-not-exist"))

    def test_returns_none_for_an_empty_manifest(self):
        self.assertIsNone(classify.manifest_entry([], "uv-cache"))


class TestBuildRowsDockerRow(unittest.TestCase):
    """docker-images has no real filesystem path, so build_rows splices its
    row in separately from scan.py's docker_pass() output rather than
    finding it by walking scan_data["roots"] like every other entry."""

    def setUp(self):
        classify.HOME = "/Users/me"
        self.now = 1_700_000_000.0
        self.empty_scan = {"roots": []}

    def test_adds_exactly_one_row_when_available_with_bytes(self):
        scan_data = dict(self.empty_scan,
                          docker={"available": True, "bytes": 12_000_000, "reason": ""})
        rows = classify.build_rows(scan_data, MANIFEST_WITH_DOCKER, self.now)
        docker_rows = [r for r in rows if r["path"] == classify.DOCKER_ROW_PATH]
        self.assertEqual(len(docker_rows), 1)
        self.assertEqual(docker_rows[0]["bytes"], 12_000_000)

    def test_adds_none_when_unavailable(self):
        scan_data = dict(self.empty_scan,
                          docker={"available": False, "bytes": 0, "reason": "daemon down"})
        rows = classify.build_rows(scan_data, MANIFEST_WITH_DOCKER, self.now)
        self.assertEqual(rows, [])

    def test_adds_none_when_bytes_are_zero(self):
        scan_data = dict(self.empty_scan,
                          docker={"available": True, "bytes": 0, "reason": ""})
        rows = classify.build_rows(scan_data, MANIFEST_WITH_DOCKER, self.now)
        self.assertEqual(rows, [])

    def test_adds_none_when_docker_key_is_missing(self):
        rows = classify.build_rows(self.empty_scan, MANIFEST_WITH_DOCKER, self.now)
        self.assertEqual(rows, [])

    def test_adds_none_when_manifest_has_no_docker_images_entry(self):
        # Mirrors the linux/windows starter manifests, which don't define one.
        scan_data = dict(self.empty_scan,
                          docker={"available": True, "bytes": 5_000_000, "reason": ""})
        rows = classify.build_rows(scan_data, MANIFEST, self.now)
        self.assertEqual(rows, [])

    def test_docker_row_action_is_ask_never_batched(self):
        scan_data = dict(self.empty_scan,
                          docker={"available": True, "bytes": 12_000_000, "reason": ""})
        row = classify.build_rows(scan_data, MANIFEST_WITH_DOCKER, self.now)[0]
        self.assertEqual(row["tier"], "costly")
        self.assertEqual(row["action"], "ask")

    def test_docker_row_carries_why_and_regen_from_the_manifest_entry(self):
        scan_data = dict(self.empty_scan,
                          docker={"available": True, "bytes": 12_000_000, "reason": ""})
        row = classify.build_rows(scan_data, MANIFEST_WITH_DOCKER, self.now)[0]
        self.assertEqual(row["why"], DOCKER_ENTRY["why"])
        self.assertEqual(row["regen"], DOCKER_ENTRY["regen"])

    def test_docker_row_flows_through_render_table_and_ask_notes(self):
        scan_data = dict(self.empty_scan,
                          docker={"available": True, "bytes": 12_000_000, "reason": ""})
        rows = classify.build_rows(scan_data, MANIFEST_WITH_DOCKER, self.now)
        table = classify.render_table(rows, root_totals={"docker": 12_000_000})
        self.assertIn(classify.DOCKER_ROW_PATH, table)
        self.assertIn("ask", table)
        notes = classify.render_ask_notes(rows)
        self.assertIn(classify.DOCKER_ROW_PATH, notes)
        self.assertIn(DOCKER_ENTRY["regen"], notes)

    def test_docker_row_counts_toward_the_reclaimable_total(self):
        scan_data = dict(self.empty_scan,
                          docker={"available": True, "bytes": 12_000_000, "reason": ""})
        rows = classify.build_rows(scan_data, MANIFEST_WITH_DOCKER, self.now)
        self.assertEqual(classify.totals(rows, 0, 0)["reclaimable"], 12_000_000)

    def test_docker_row_joins_ordinary_rows_in_the_same_sort(self):
        scan_data = dict(self.empty_scan, docker={"available": True, "bytes": 999, "reason": ""})
        scan_data["roots"] = [{
            "rows": [{"path": "/Users/me/.cache/uv", "bytes": 7_000_000_000,
                      "mtime": self.now}],
            "files": [],
        }]
        rows = classify.build_rows(scan_data, MANIFEST_WITH_DOCKER, self.now)
        ordered = sorted(rows, key=lambda r: r["bytes"], reverse=True)
        self.assertEqual(ordered[-1]["path"], classify.DOCKER_ROW_PATH)


class TestIsProjectArtifact(unittest.TestCase):
    def test_stale_project_with_manifest_qualifies(self):
        self.assertTrue(classify.is_project_artifact(
            "/Volumes/External/proj/node_modules", facts()))

    def test_tool_directory_never_qualifies(self):
        self.assertFalse(classify.is_project_artifact(
            "/Users/me/.vscode/extensions/foo/node_modules", facts()))

    def test_missing_sibling_manifest_disqualifies(self):
        self.assertFalse(classify.is_project_artifact(
            "/Volumes/External/proj/node_modules", facts(sib=False)))

    def test_recently_touched_project_disqualifies(self):
        self.assertFalse(classify.is_project_artifact(
            "/Volumes/External/proj/node_modules", facts(age=3)))

    def test_dirty_git_worktree_disqualifies(self):
        self.assertFalse(classify.is_project_artifact(
            "/Volumes/External/proj/node_modules", facts(dirty=True)))

    def test_unknown_git_state_disqualifies(self):
        self.assertFalse(classify.is_project_artifact(
            "/Volumes/External/proj/node_modules", facts(dirty=None)))

    def test_threshold_is_thirty_days(self):
        self.assertFalse(classify.is_project_artifact(
            "/Volumes/External/proj/node_modules", facts(age=29)))
        self.assertTrue(classify.is_project_artifact(
            "/Volumes/External/proj/node_modules", facts(age=30)))


class TestHumanSize(unittest.TestCase):
    def test_gigabytes(self):
        self.assertEqual(classify.human_size(8_589_934_592), "8.0G")

    def test_megabytes(self):
        self.assertEqual(classify.human_size(566_231_040), "540M")

    def test_zero(self):
        self.assertEqual(classify.human_size(0), "0B")

    def test_large_gigabytes_have_no_decimal(self):
        self.assertEqual(classify.human_size(49_392_123_904), "46G")

    def test_just_under_ten_keeps_its_decimal(self):
        self.assertEqual(classify.human_size(9_663_676_416), "9.0G")

    def test_exactly_ten_drops_the_decimal(self):
        self.assertEqual(classify.human_size(10_737_418_240), "10G")

    def test_ratio_just_below_ten_renders_without_a_decimal(self):
        # 9.9999G: the decimal branch would round this to "10.0G".
        self.assertEqual(classify.human_size(10_736_369_663), "10G")


class TestRenderTable(unittest.TestCase):
    def test_rows_are_ordered_biggest_first(self):
        out = classify.render_table(ROWS, root_totals={"": 100_000_000_000})
        lines = [l for l in out.splitlines() if "/" in l]
        self.assertIn("GameLibrary", lines[0])
        self.assertIn("BraveSoftware", lines[1])

    def test_header_names_every_column(self):
        out = classify.render_table(ROWS, root_totals={"": 100_000_000_000})
        for col in ("SIZE", "%ROOT", "PATH", "AGE", "TIER", "ACTION"):
            self.assertIn(col, out)

    def test_review_only_rows_render_without_a_tier(self):
        out = classify.render_table(ROWS, root_totals={"": 100_000_000_000})
        steam = [l for l in out.splitlines() if "GameLibrary" in l][0]
        self.assertIn("review only", steam)


APPS = [
    {"path": "/Applications/iMovie.app", "bytes": 3_972_844_748, "last_used": "2026-06-13"},
    {"path": "/Applications/Xcode.app", "bytes": 4_187_593_113, "last_used": None},
]


class TestRenderApps(unittest.TestCase):
    def test_apps_are_ordered_biggest_first(self):
        lines = classify.render_apps(APPS).splitlines()
        self.assertIn("Xcode.app", lines[1])
        self.assertIn("iMovie.app", lines[2])

    def test_missing_date_renders_unknown_not_unused(self):
        out = classify.render_apps(APPS)
        self.assertIn("unknown", out)
        self.assertNotIn("unused", out)

    def test_apps_are_never_proposed(self):
        for line in classify.render_apps(APPS).splitlines()[1:]:
            self.assertIn("review only", line)

    def test_header_has_last_used_not_age(self):
        header = classify.render_apps(APPS).splitlines()[0]
        self.assertIn("LAST USED", header)
        self.assertNotIn("AGE", header)


class TestRenderAskNotes(unittest.TestCase):
    def test_only_ask_rows_appear(self):
        rows = [
            {"path": "/a", "bytes": 100, "action": "ask", "regen": "costs a lot"},
            {"path": "/b", "bytes": 200, "action": "proposed", "regen": "cheap"},
        ]
        out = classify.render_ask_notes(rows)
        self.assertIn("/a", out)
        self.assertNotIn("/b", out)

    def test_regen_text_is_shown(self):
        rows = [{"path": "/a", "bytes": 100, "action": "ask", "regen": "redownloads 2.9G"}]
        self.assertIn("redownloads 2.9G", classify.render_ask_notes(rows))

    def test_missing_regen_is_not_silently_blank(self):
        rows = [{"path": "/a", "bytes": 100, "action": "ask", "regen": ""}]
        self.assertIn("expensive", classify.render_ask_notes(rows))

    def test_no_ask_rows_yields_nothing(self):
        rows = [{"path": "/b", "bytes": 200, "action": "proposed", "regen": "x"}]
        self.assertEqual(classify.render_ask_notes(rows), "")

    def test_biggest_first(self):
        rows = [
            {"path": "/small", "bytes": 100, "action": "ask", "regen": "x"},
            {"path": "/big", "bytes": 900, "action": "ask", "regen": "y"},
        ]
        self.assertLess(classify.render_ask_notes(rows).index("/big"),
                        classify.render_ask_notes(rows).index("/small"))


class TestDropNested(unittest.TestCase):
    def test_child_inside_parent_is_dropped(self):
        rows = [{"path": "/a", "bytes": 100, "action": "proposed"},
                {"path": "/a/b", "bytes": 60, "action": "proposed"}]
        self.assertEqual([r["path"] for r in classify.drop_nested(rows)], ["/a"])

    def test_siblings_are_both_kept(self):
        rows = [{"path": "/a/b", "bytes": 60, "action": "proposed"},
                {"path": "/a/c", "bytes": 40, "action": "proposed"}]
        self.assertEqual(len(classify.drop_nested(rows)), 2)

    def test_prefix_lookalike_is_not_treated_as_nested(self):
        # /a/bcd is NOT inside /a/b
        rows = [{"path": "/a/b", "bytes": 60, "action": "proposed"},
                {"path": "/a/bcd", "bytes": 40, "action": "proposed"}]
        self.assertEqual(len(classify.drop_nested(rows)), 2)

    def test_dedup_crosses_buckets(self):
        rows = [{"path": "/a", "bytes": 100, "action": "review only"},
                {"path": "/a/b", "bytes": 60, "action": "proposed"}]
        self.assertEqual([r["path"] for r in classify.drop_nested(rows)], ["/a"])

    def test_totals_do_not_double_count(self):
        rows = [{"path": "/a", "bytes": 100, "action": "proposed"},
                {"path": "/a/b", "bytes": 60, "action": "proposed"}]
        self.assertEqual(classify.totals(rows, 0, 0)["reclaimable"], 100)


class TestTotals(unittest.TestCase):
    def test_reclaimable_counts_only_proposed_and_ask(self):
        t = classify.totals(ROWS, unreadable_bytes=20_401_094_656, unreadable_count=412)
        self.assertEqual(t["reclaimable"], 8_589_934_592 + 7_086_696_038)

    def test_review_only_is_tracked_separately(self):
        t = classify.totals(ROWS, unreadable_bytes=0, unreadable_count=0)
        self.assertEqual(t["review_only"], 49_392_123_904)

    def test_unmeasured_never_enters_reclaimable(self):
        t = classify.totals(ROWS, unreadable_bytes=20_401_094_656, unreadable_count=412)
        self.assertEqual(t["unmeasured"], 20_401_094_656)
        self.assertEqual(t["unmeasured_count"], 412)
        self.assertNotIn(20_401_094_656, [t["reclaimable"], t["review_only"]])

    def test_proposed_child_under_review_only_parent_still_counts(self):
        rows = [{"path": "/Users", "bytes": 1000, "action": "review only"},
                {"path": "/Users/me/.npm", "bytes": 300, "action": "proposed"}]
        self.assertEqual(classify.totals(rows, 0, 0)["reclaimable"], 300)

    def test_proposed_child_under_proposed_parent_is_not_double_counted(self):
        rows = [{"path": "/Users/me/.npm", "bytes": 300, "action": "proposed"},
                {"path": "/Users/me/.npm/_cacache", "bytes": 200, "action": "proposed"}]
        self.assertEqual(classify.totals(rows, 0, 0)["reclaimable"], 300)

    def test_root_self_row_is_excluded(self):
        rows = [{"path": "/", "bytes": 999999, "action": "review only", "is_root": True},
                {"path": "/Users/me/.npm", "bytes": 300, "action": "proposed"}]
        t = classify.totals(rows, 0, 0)
        self.assertEqual(t["reclaimable"], 300)
        self.assertEqual(t["review_only"], 0)

    def test_mountpoint_stub_does_not_absorb_another_root(self):
        # The data volume's du -x walk emits a 0-byte /Volumes stub without
        # crossing into the mount. It must not swallow the other root's rows.
        rows = [
            {"path": "/", "bytes": 500, "action": "review only", "is_root": True, "root": "/"},
            {"path": "/Volumes", "bytes": 0, "action": "review only", "root": "/"},
            {"path": "/Volumes/External/big", "bytes": 800, "action": "review only",
             "root": "/Volumes/External"},
            {"path": "/Users/me/.npm", "bytes": 300, "action": "proposed", "root": "/"},
        ]
        t = classify.totals(rows, 0, 0)
        self.assertEqual(t["reclaimable"], 300)
        self.assertEqual(t["review_only"], 800)

    def test_nesting_still_dedups_within_one_root(self):
        rows = [
            {"path": "/Users/me/.npm", "bytes": 300, "action": "proposed", "root": "/"},
            {"path": "/Users/me/.npm/_cacache", "bytes": 200, "action": "proposed", "root": "/"},
        ]
        self.assertEqual(classify.totals(rows, 0, 0)["reclaimable"], 300)


class TestRealToolDirectoriesAreProtected(unittest.TestCase):
    """Regression fixtures, not illustrations.

    Two batches, two different gates, both real:

    Batch 1: every path found QUALIFYING FOR DELETION by
    `scan.project_pass(os.path.expanduser("~"))` on a real machine on
    2026-09-17 (home directory replaced with the literal /Users/me) - a
    reviewer ran Task 12's own Step 6 gate and got 119 artifacts, 24
    qualifying, all 24 installed tooling and none real projects.

    Batch 2: the `~`-scoped gate above never reaches /Applications or
    /opt, so it could not have caught these. Found QUALIFYING FOR DELETION
    by `scan.project_pass("/System/Volumes/Data")` - main()'s actual root -
    on 2026-09-17, after the scan-path canonicalization fix made the
    /Users-anchored patterns above match again. These are machine-wide
    paths (installed app bundles, Homebrew Cellar), not home-relative, so
    they are pinned verbatim with no /Users/me substitution.

    Both batches are pinned here so a future change to classify._DENY
    cannot silently un-protect any of these shapes again.
    """

    # Recorded verbatim from the qualifying rows of that run.
    REAL_QUALIFYING_PATHS = [
        # globalStorage (Devin)
        '/Users/me/Library/Application Support/Devin/User/globalStorage/ms-edgedevtools.vscode-edge-devtools/node_modules',
        # Claude Desktop MCP extension
        '/Users/me/Library/Application Support/Claude/Claude Extensions/ant.dir.ant.anthropic.filesystem/node_modules',
        # globalStorage (VS Code)
        '/Users/me/Library/Application Support/Code/User/globalStorage/ms-edgedevtools.vscode-edge-devtools/node_modules',
        # Library/pnpm tool install
        '/Users/me/Library/pnpm/.tools/pnpm/10.33.2_tmp_7359/node_modules',
        '/Users/me/Library/pnpm/.tools/pnpm/10.32.1_tmp_6603/node_modules',
        # Library/Caches/typescript tool cache
        '/Users/me/Library/Caches/typescript/5.9/node_modules',
        '/Users/me/Library/Caches/typescript/6.0/node_modules',
        # .devin/extensions
        '/Users/me/.devin/extensions/dsznajder.es7-react-js-snippets-4.4.3/node_modules',
        '/Users/me/.devin/extensions/ms-python.python-2026.4.0-universal/out/client/node_modules',
        '/Users/me/.devin/extensions/xdebug.php-debug-1.40.1-universal/node_modules',
        '/Users/me/.devin/extensions/curlconverter.curlconverter-0.0.3/node_modules',
        '/Users/me/.devin/extensions/zhuangtongfa.material-theme-3.19.0/node_modules',
        '/Users/me/.devin/extensions/oderwat.indent-rainbow-8.3.1/node_modules',
        '/Users/me/.devin/extensions/hermitter.oh-lucy-vscode-0.0.3/node_modules',
        '/Users/me/.devin/extensions/bmewburn.vscode-intelephense-client-1.18.5-universal/node_modules',
        '/Users/me/.devin/extensions/zobo.php-intellisense-1.3.3/node_modules',
        '/Users/me/.devin/extensions/esbenp.prettier-vscode-12.4.0-universal/node_modules',
        '/Users/me/.devin/extensions/ritwickdey.liveserver-5.7.10-universal/node_modules',
        '/Users/me/.devin/extensions/ms-vscode.vscode-typescript-next-6.0.20260206/node_modules',
        '/Users/me/.devin/extensions/postman.postman-for-vscode-1.19.1/node_modules',
        '/Users/me/.devin/extensions/ms-edgedevtools.vscode-edge-devtools-2.1.10/node_modules',
        # .antigravity/extensions
        '/Users/me/.antigravity/extensions/hermitter.oh-lucy-vscode-0.0.1-universal/node_modules',
        '/Users/me/.antigravity/extensions/ms-python.python-2026.4.0-universal/out/client/node_modules',
        '/Users/me/.antigravity/extensions/esbenp.prettier-vscode-12.4.0-universal/node_modules',
        # Batch 2 - found via scan.project_pass("/System/Volumes/Data"), the
        # real integrated root, not $HOME. Installed application bundles and
        # Homebrew Cellar - machine-wide paths, no /Users/me substitution.
        '/Applications/Docker.app/Contents/MacOS/Docker Desktop.app/Contents/Resources/app.asar.unpacked/node_modules',
        '/Applications/Devin.app/Contents/Resources/app/node_modules',
        '/opt/homebrew/Cellar/heroku/11.7.1/libexec/node_modules',
        '/opt/homebrew/Cellar/heroku/11.7.1/lib/client/11.9.0-c5fbf4b/node_modules',
    ]

    def test_each_real_qualifying_path_is_now_protected(self):
        for path in self.REAL_QUALIFYING_PATHS:
            with self.subTest(path=path):
                self.assertIsNotNone(classify.is_protected(path))


class TestDetectOs(unittest.TestCase):
    def test_darwin_is_macos(self):
        self.assertEqual(classify.detect_os("darwin"), "macos")

    def test_linux(self):
        self.assertEqual(classify.detect_os("linux"), "linux")

    def test_windows(self):
        self.assertEqual(classify.detect_os("win32"), "windows")

    def test_anything_else_is_unknown(self):
        self.assertEqual(classify.detect_os("freebsd12"), "unknown")


class TestUnknownOsIsReportOnly(unittest.TestCase):
    def test_empty_manifest_proposes_nothing(self):
        for path in ("/home/me/.cache", "/Users/me/.Trash", "/anything"):
            self.assertEqual(classify.classify_row(path, [])["action"], "review only")


# ---------------------------------------------------------------------------
# C1/C2 - render_table's row cap and per-root percentages
# ---------------------------------------------------------------------------

class TestRootTotals(unittest.TestCase):
    def test_reads_bytes_from_each_roots_self_row(self):
        scan_data = {"roots": [
            {"root": "/System/Volumes/Data", "rows": [
                {"path": "/", "bytes": 114_000_000_000, "mtime": 1_700_000_000.0},
                {"path": "/Users", "bytes": 60_000_000_000, "mtime": 1_700_000_000.0},
            ]},
            {"root": "/Volumes/External", "rows": [
                {"path": "/Volumes/External", "bytes": 166_000_000_000, "mtime": 1_700_000_000.0},
            ]},
        ]}
        self.assertEqual(classify.root_totals(scan_data),
                         {"/": 114_000_000_000, "/Volumes/External": 166_000_000_000})

    def test_data_volume_root_path_is_canonicalised_to_slash(self):
        scan_data = {"roots": [{"root": "/System/Volumes/Data",
                                 "rows": [{"path": "/", "bytes": 500, "mtime": 1.0}]}]}
        self.assertEqual(classify.root_totals(scan_data), {"/": 500})

    def test_root_missing_its_self_row_is_absent_not_zero(self):
        scan_data = {"roots": [{"root": "/Volumes/External", "rows": [
            {"path": "/Volumes/External/big", "bytes": 500, "mtime": 1.0}]}]}
        self.assertEqual(classify.root_totals(scan_data), {})

    def test_no_roots_returns_empty_dict(self):
        self.assertEqual(classify.root_totals({"roots": []}), {})


class TestRenderTablePercentages(unittest.TestCase):
    def test_percentage_is_relative_to_its_own_root_not_a_combined_sum(self):
        # Regression: summing bytes across both scan roots made a root's
        # own 100%-of-itself row read as a single digit. External is 166G
        # of its own 166G root, not a fraction of Data's unrelated 114G.
        rows = [
            {"path": "/Volumes/External", "bytes": 166_000_000_000, "age_days": 0,
             "tier": None, "action": "review only", "root": "/Volumes/External"},
            {"path": "/", "bytes": 114_000_000_000, "age_days": 0,
             "tier": None, "action": "review only", "root": "/"},
        ]
        totals = {"/Volumes/External": 166_000_000_000, "/": 114_000_000_000}
        out = classify.render_table(rows, totals)
        prefix = classify.human_size(166_000_000_000) + " "
        team_line = [l for l in out.splitlines() if l.startswith(prefix)][0]
        self.assertIn("100%", team_line)

    def test_a_smaller_child_gets_a_real_double_digit_share_not_9_percent(self):
        # GameLibrary alone, without its root's own self-row also present -
        # drop_nested legitimately collapses a review-only child into a
        # review-only parent when both are in the same render_table call
        # (that is the point of the cap in C1); this isolates the %ROOT
        # division fix from that separate, correct collapsing behaviour.
        rows = [
            {"path": "/Volumes/External/GameLibrary", "bytes": 46_000_000_000, "age_days": 60,
             "tier": None, "action": "review only", "root": "/Volumes/External"},
        ]
        out = classify.render_table(rows, {"/Volumes/External": 166_000_000_000})
        steam_line = [l for l in out.splitlines() if "GameLibrary" in l][0]
        self.assertIn("28%", steam_line)  # 46/166 ~= 28%, not 46/(166+46+...) ~= 9%

    def test_missing_root_total_renders_dash_not_a_number(self):
        rows = [{"path": "/x", "bytes": 100, "age_days": 1, "tier": None,
                 "action": "review only", "root": "unknown-root"}]
        out = classify.render_table(rows, {})
        prefix = "%-7s %-6s" % (classify.human_size(100), "-")
        self.assertTrue(any(l.startswith(prefix) for l in out.splitlines()))

    def test_zero_root_total_renders_dash_instead_of_raising(self):
        rows = [{"path": "/x", "bytes": 100, "age_days": 1, "tier": None,
                 "action": "review only", "root": "/z"}]
        out = classify.render_table(rows, {"/z": 0})  # would ZeroDivisionError unguarded
        self.assertIn("/x", out)


class TestRenderTableCap(unittest.TestCase):
    def _review_rows(self, n, root="/z"):
        return [{"path": "/z/dir%03d" % i, "bytes": 1000 - i, "age_days": 1,
                 "tier": None, "action": "review only", "root": root}
                for i in range(n)]

    def test_default_review_limit_is_25(self):
        self.assertEqual(classify.REVIEW_ROW_LIMIT, 25)

    def test_every_actionable_row_is_shown_even_past_the_review_limit(self):
        actionable = [{"path": "/z/ask%02d" % i, "bytes": 500 + i, "age_days": 1,
                       "tier": "costly", "action": "ask", "root": "/z"}
                      for i in range(30)]
        rows = actionable + self._review_rows(30)
        out = classify.render_table(rows, {"/z": 100_000}, review_limit=5)
        for r in actionable:
            self.assertIn(r["path"], out)

    def test_review_only_rows_are_capped_at_the_review_limit(self):
        out = classify.render_table(self._review_rows(30), {"/z": 100_000}, review_limit=5)
        shown = [l for l in out.splitlines() if "/z/dir" in l]
        self.assertEqual(len(shown), 5)

    def test_omitted_review_rows_are_named_with_count_and_bytes(self):
        rows = self._review_rows(30)
        out = classify.render_table(rows, {"/z": 100_000}, review_limit=5)
        last_line = out.splitlines()[-1]
        self.assertIn("25", last_line)
        omitted_bytes = sum(r["bytes"] for r in sorted(rows, key=lambda r: -r["bytes"])[5:])
        self.assertIn(classify.human_size(omitted_bytes), last_line)

    def test_no_omission_line_when_under_the_cap(self):
        out = classify.render_table(self._review_rows(3), {"/z": 100_000}, review_limit=25)
        self.assertNotIn("omitted", out)

    def test_review_only_dedup_runs_before_the_cap(self):
        # A parent and its children must not both consume a slot - and
        # with only 1 row surviving dedup, the cap is never even reached.
        rows = [{"path": "/z/parent", "bytes": 10_000, "age_days": 1, "tier": None,
                 "action": "review only", "root": "/z"}]
        rows += [{"path": "/z/parent/child%02d" % i, "bytes": 100, "age_days": 1,
                  "tier": None, "action": "review only", "root": "/z"}
                 for i in range(30)]
        out = classify.render_table(rows, {"/z": 100_000}, review_limit=25)
        self.assertNotIn("omitted", out)
        self.assertNotIn("child", out)
        data_lines = [l for l in out.splitlines()[1:] if l.strip()]
        self.assertEqual(len(data_lines), 1)

    def test_actionable_rows_are_also_deduplicated(self):
        # Matches totals()'s own bucket semantics: du prints one row per
        # depth, so a manifest match's parent and child are the same
        # reclaim target, not two.
        rows = [
            {"path": "/z/cache", "bytes": 5000, "age_days": 1, "tier": "free", "action": "proposed", "root": "/z"},
            {"path": "/z/cache/sub", "bytes": 3000, "age_days": 1, "tier": "free", "action": "proposed", "root": "/z"},
        ]
        out = classify.render_table(rows, {"/z": 100_000})
        data_lines = [l for l in out.splitlines()[1:] if l.strip()]
        self.assertEqual(len(data_lines), 1)


class TestRenderTableUnknownAge(unittest.TestCase):
    def test_negative_age_days_renders_question_mark(self):
        rows = [{"path": "/x", "bytes": 100, "age_days": -1, "tier": None,
                 "action": "review only", "root": "/"}]
        out = classify.render_table(rows, {"/": 1000})
        line = [l for l in out.splitlines() if "/x" in l][0]
        self.assertIn("?", line)
        self.assertNotIn("-1d", line)


class TestDropNestedRootSlashGuard(unittest.TestCase):
    def test_root_slash_does_not_swallow_the_rest_of_the_tree(self):
        rows = [
            {"path": "/", "bytes": 100_000, "action": "review only"},
            {"path": "/Users", "bytes": 60_000, "action": "review only"},
            {"path": "/Applications", "bytes": 20_000, "action": "review only"},
        ]
        kept = classify.drop_nested(rows)
        self.assertEqual({r["path"] for r in kept}, {"/", "/Users", "/Applications"})

    def test_root_slash_is_safe_even_without_an_is_root_flag(self):
        # Regression for the "0B headline bug": this guard must not depend
        # on the caller having first filtered out is_root rows - a payload
        # missing its "root" key would defeat an is_root-only guard.
        rows = [
            {"path": "/", "bytes": 100_000, "action": "review only"},
            {"path": "/Volumes/External/big", "bytes": 90_000, "action": "review only"},
        ]
        self.assertEqual(len(classify.drop_nested(rows)), 2)

    def test_duplicate_root_slash_rows_still_dedup_to_one(self):
        rows = [
            {"path": "/", "bytes": 100_000, "action": "review only"},
            {"path": "/", "bytes": 100_000, "action": "review only"},
        ]
        self.assertEqual(len(classify.drop_nested(rows)), 1)


# ---------------------------------------------------------------------------
# I6/I7 - the METHOD column
# ---------------------------------------------------------------------------

class TestRowMethod(unittest.TestCase):
    def test_manifest_command_wins_regardless_of_tier(self):
        self.assertEqual(
            classify.row_method("proposed", "cheap", "npm-cache", "npm cache clean --force"),
            "npm cache clean --force")
        self.assertEqual(
            classify.row_method("ask", "costly", "docker-images", "docker system prune"),
            "docker system prune")

    def test_user_trash_is_never_run_through_a_command(self):
        self.assertEqual(classify.row_method("proposed", "free", "user-trash", None),
                         "empty Trash yourself")
        self.assertEqual(classify.row_method("proposed", "free", "user-trash", "some command"),
                         "empty Trash yourself")

    def test_cheap_tier_with_no_command_is_permanent_rm(self):
        self.assertEqual(classify.row_method("proposed", "cheap", "node-gyp-cache", None), "rm")

    def test_free_tier_with_no_command_is_trash(self):
        self.assertEqual(classify.row_method("proposed", "free", "user-logs", None), "trash")

    def test_project_tier_with_no_command_is_trash(self):
        self.assertEqual(classify.row_method("ask", "project", None, None), "trash")

    def test_costly_tier_with_no_command_is_trash(self):
        self.assertEqual(classify.row_method("ask", "costly", "uv-cache", None), "trash")

    def test_review_only_rows_have_no_method(self):
        self.assertIsNone(classify.row_method("review only", None, None, None))

    def test_protected_rows_have_no_method(self):
        self.assertIsNone(classify.row_method("protected", None, None, None))


class TestClassifyRowExposesMatchedEntry(unittest.TestCase):
    def setUp(self):
        classify.HOME = "/Users/me"

    def test_matched_entry_exposes_id_and_command(self):
        row = classify.classify_row("/Users/me/.npm", MANIFEST)
        self.assertEqual(row["id"], "npm-cache")
        self.assertEqual(row["command"], "npm cache clean --force")

    def test_matched_entry_with_no_command_exposes_id_and_none(self):
        row = classify.classify_row("/Users/me/.Trash", MANIFEST)
        self.assertEqual(row["id"], "user-trash")
        self.assertIsNone(row["command"])

    def test_unmatched_path_has_no_id_or_command(self):
        row = classify.classify_row("/Volumes/External/GameLibrary", MANIFEST)
        self.assertIsNone(row["id"])
        self.assertIsNone(row["command"])

    def test_protected_path_has_no_id_or_command(self):
        manifest = MANIFEST + [{"id": "bad", "path": "~/Library/Mail", "tier": "free",
                                "why": "x", "regen": "y", "command": None}]
        row = classify.classify_row("/Users/me/Library/Mail", manifest)
        self.assertIsNone(row["id"])
        self.assertIsNone(row["command"])


class TestMethodColumnInTable(unittest.TestCase):
    def test_method_header_and_command_value_render(self):
        rows = [{"path": "/Users/me/.npm", "bytes": 100, "age_days": 1, "tier": "cheap",
                 "action": "proposed", "root": "/", "method": "npm cache clean --force"}]
        out = classify.render_table(rows, {"/": 1000})
        self.assertIn("METHOD", out.splitlines()[0])
        self.assertIn("npm cache clean --force", out)

    def test_missing_method_renders_dash(self):
        rows = [{"path": "/Volumes/External/steam", "bytes": 100, "age_days": 1, "tier": None,
                 "action": "review only", "root": "/Volumes/External"}]  # no "method" key at all
        out = classify.render_table(rows, {"/Volumes/External": 1000})
        line = [l for l in out.splitlines() if "steam" in l][0]
        self.assertTrue(line.rstrip().endswith("-"))


class TestBuildRowsMethod(unittest.TestCase):
    def setUp(self):
        classify.HOME = "/Users/me"
        self.now = 1_700_000_000.0

    def test_row_carries_manifest_command_as_method(self):
        scan_data = {"roots": [{"root": "/", "rows": [
            {"path": "/Users/me/.npm", "bytes": 100, "mtime": self.now}], "files": []}]}
        rows = classify.build_rows(scan_data, MANIFEST, self.now)
        self.assertEqual(rows[0]["method"], "npm cache clean --force")

    def test_docker_row_method_is_its_own_command(self):
        scan_data = {"roots": [], "docker": {"available": True, "bytes": 12_000_000, "reason": ""}}
        rows = classify.build_rows(scan_data, MANIFEST_WITH_DOCKER, self.now)
        self.assertEqual(rows[0]["method"], "docker system prune")


# ---------------------------------------------------------------------------
# I5 - the discarded projects pass, wired in
# ---------------------------------------------------------------------------

class TestBuildRowsProjectPass(unittest.TestCase):
    def setUp(self):
        classify.HOME = "/Users/me"
        self.now = 1_700_000_000.0

    def _scan(self, projects, rows=None, root="/Volumes/External"):
        return {"roots": [{"root": root, "rows": rows or [], "files": [], "projects": projects}]}

    def test_qualifying_project_row_is_project_tier_ask_action(self):
        scan_data = self._scan([{"path": "/Volumes/External/app/node_modules",
                                  "bytes": 900_000_000, "age_days": 45, "qualifies": True}])
        row = classify.build_rows(scan_data, MANIFEST, self.now)[0]
        self.assertEqual(row["tier"], "project")
        self.assertEqual(row["action"], "ask")

    def test_qualifying_project_row_names_its_age_and_has_a_regen_note(self):
        scan_data = self._scan([{"path": "/Volumes/External/app/node_modules",
                                  "bytes": 900_000_000, "age_days": 45, "qualifies": True}])
        row = classify.build_rows(scan_data, MANIFEST, self.now)[0]
        self.assertIn("45", row["why"])
        self.assertTrue(row["regen"])

    def test_qualifying_project_row_appears_in_ask_notes(self):
        scan_data = self._scan([{"path": "/Volumes/External/app/node_modules",
                                  "bytes": 900_000_000, "age_days": 45, "qualifies": True}])
        rows = classify.build_rows(scan_data, MANIFEST, self.now)
        self.assertIn("/Volumes/External/app/node_modules", classify.render_ask_notes(rows))

    def test_qualifying_project_row_method_is_trash(self):
        scan_data = self._scan([{"path": "/Volumes/External/app/node_modules",
                                  "bytes": 900_000_000, "age_days": 45, "qualifies": True}])
        row = classify.build_rows(scan_data, MANIFEST, self.now)[0]
        self.assertEqual(row["method"], "trash")

    def test_non_qualifying_project_row_is_classified_like_any_path(self):
        scan_data = self._scan([{"path": "/Volumes/External/app/node_modules",
                                  "bytes": 900_000_000, "age_days": 2, "qualifies": False}])
        row = classify.build_rows(scan_data, MANIFEST, self.now)[0]
        self.assertEqual(row["action"], "review only")

    def test_non_qualifying_tool_directory_project_row_is_protected(self):
        scan_data = self._scan(
            [{"path": "/Users/me/.vscode/extensions/foo/node_modules",
              "bytes": 900_000_000, "age_days": 400, "qualifies": False}],
            root="/")
        row = classify.build_rows(scan_data, MANIFEST, self.now)[0]
        self.assertEqual(row["action"], "protected")

    def test_project_row_upgrades_existing_du_row_instead_of_appending(self):
        scan_data = self._scan(
            projects=[{"path": "/Volumes/External/app/node_modules", "bytes": 999,
                       "age_days": 45, "qualifies": True}],
            rows=[{"path": "/Volumes/External/app/node_modules", "bytes": 900_000_000, "mtime": self.now}],
        )
        rows = classify.build_rows(scan_data, MANIFEST, self.now)
        matches = [r for r in rows if r["path"] == "/Volumes/External/app/node_modules"]
        self.assertEqual(len(matches), 1, "two rows for one path would double-count and double-ask")
        self.assertEqual(matches[0]["action"], "ask")
        self.assertEqual(matches[0]["tier"], "project")
        # bytes/age come from the existing du row, not re-measured by the project pass
        self.assertEqual(matches[0]["bytes"], 900_000_000)

    def test_project_row_appended_when_no_du_row_exists_at_that_path(self):
        scan_data = self._scan([{"path": "/Volumes/External/very/deep/app/node_modules",
                                  "bytes": 5_000_000, "age_days": 45, "qualifies": True}])
        rows = classify.build_rows(scan_data, MANIFEST, self.now)
        matches = [r for r in rows if r["path"] == "/Volumes/External/very/deep/app/node_modules"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["bytes"], 5_000_000)

    def test_no_projects_key_does_not_crash(self):
        scan_data = {"roots": [{"root": "/", "rows": [], "files": []}]}
        self.assertEqual(classify.build_rows(scan_data, MANIFEST, self.now), [])


# ---------------------------------------------------------------------------
# I4 - the unmeasured (sudo-gap) bytes
# ---------------------------------------------------------------------------

class TestUnreadableGapBytes(unittest.TestCase):
    def test_gap_is_df_used_minus_self_row_bytes(self):
        scan_data = {
            "roots": [{"root": "/System/Volumes/Data", "rows": [
                {"path": "/", "bytes": 113_000_000_000, "mtime": 1.0}]}],
            "mounts": [{"mount": "/System/Volumes/Data", "used_bytes": 132_000_000_000}],
        }
        root_tot = classify.root_totals(scan_data)
        self.assertEqual(classify.unreadable_gap_bytes(scan_data, root_tot), 19_000_000_000)

    def test_gap_floors_at_zero_when_du_already_saw_more_than_df(self):
        scan_data = {
            "roots": [{"root": "/Volumes/External", "rows": [
                {"path": "/Volumes/External", "bytes": 179_000_000_000, "mtime": 1.0}]}],
            "mounts": [{"mount": "/Volumes/External", "used_bytes": 174_000_000_000}],
        }
        root_tot = classify.root_totals(scan_data)
        self.assertEqual(classify.unreadable_gap_bytes(scan_data, root_tot), 0)

    def test_root_without_a_matching_mount_contributes_nothing(self):
        scan_data = {"roots": [{"root": "/nope", "rows": []}], "mounts": []}
        self.assertEqual(classify.unreadable_gap_bytes(scan_data, {}), 0)

    def test_gap_sums_across_multiple_roots(self):
        scan_data = {
            "roots": [
                {"root": "/System/Volumes/Data", "rows": [{"path": "/", "bytes": 100, "mtime": 1.0}]},
                {"root": "/Volumes/External", "rows": [{"path": "/Volumes/External", "bytes": 50, "mtime": 1.0}]},
            ],
            "mounts": [
                {"mount": "/System/Volumes/Data", "used_bytes": 150},
                {"mount": "/Volumes/External", "used_bytes": 80},
            ],
        }
        root_tot = classify.root_totals(scan_data)
        self.assertEqual(classify.unreadable_gap_bytes(scan_data, root_tot), 80)  # (150-100)+(80-50)

    def test_never_folded_into_reclaimable(self):
        t = classify.totals(ROWS, unreadable_bytes=19_000_000_000, unreadable_count=404)
        self.assertNotIn(19_000_000_000, (t["reclaimable"], t["review_only"]))
        self.assertEqual(t["unmeasured"], 19_000_000_000)


# ---------------------------------------------------------------------------
# I10 - scan-age header
# ---------------------------------------------------------------------------

class TestRenderScanAgeHeader(unittest.TestCase):
    def test_fresh_scan_has_no_stale_marker(self):
        now = 1_700_100_000.0
        data = {"roots": [{"scanned_at": now - 3600}]}
        self.assertNotIn("STALE", classify.render_scan_age_header(data, now))

    def test_scan_older_than_24h_is_marked_stale(self):
        now = 1_700_100_000.0
        data = {"roots": [{"scanned_at": now - 25 * 3600}]}
        self.assertIn("STALE", classify.render_scan_age_header(data, now))

    def test_exactly_24h_is_not_yet_stale(self):
        now = 1_700_100_000.0
        data = {"roots": [{"scanned_at": now - 24 * 3600}]}
        self.assertNotIn("STALE", classify.render_scan_age_header(data, now))

    def test_oldest_of_several_roots_governs_staleness(self):
        now = 1_700_100_000.0
        data = {"roots": [{"scanned_at": now - 1 * 3600}, {"scanned_at": now - 25 * 3600}]}
        self.assertIn("STALE", classify.render_scan_age_header(data, now))

    def test_missing_timestamp_does_not_crash(self):
        out = classify.render_scan_age_header({"roots": []}, 1_700_100_000.0)
        self.assertIn("unknown", out)


# ---------------------------------------------------------------------------
# End-to-end: main() wiring (I3's overlap label, I4's printed gap, I10's
# header, and C2's %ROOT fix, all together as they actually print).
# ---------------------------------------------------------------------------

class TestMainIntegration(unittest.TestCase):
    def setUp(self):
        self._real_home = classify.HOME
        self.addCleanup(self._restore_home)

    def _restore_home(self):
        classify.HOME = self._real_home

    def _write_scan(self, now):
        scan_data = {
            "roots": [
                {"root": "/System/Volumes/Data", "scanned_at": now - 1800,
                 "rows": [
                     {"path": "/", "bytes": 114_000_000_000, "mtime": now},
                     {"path": "/Users/me/.npm", "bytes": 2_500_000_000, "mtime": now},
                 ],
                 "files": [], "projects": [],
                 "unreadable_count": 400, "transient_count": 0},
                {"root": "/Volumes/External", "scanned_at": now - 1200,
                 "rows": [
                     {"path": "/Volumes/External", "bytes": 166_000_000_000, "mtime": now},
                     {"path": "/Volumes/External/GameLibrary", "bytes": 46_000_000_000, "mtime": now},
                 ],
                 "files": [], "projects": [],
                 "unreadable_count": 4, "transient_count": 0},
            ],
            "apps": [],
            "snapshots": [],
            "mounts": [
                {"mount": "/System/Volumes/Data", "used_bytes": 133_000_000_000},
                {"mount": "/Volumes/External", "used_bytes": 174_000_000_000},
            ],
            "docker": {"available": False, "reason": "daemon down", "bytes": 0},
        }
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(scan_data, tmp)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return tmp.name

    def _run(self, now):
        path = self._write_scan(now)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            classify.main([path, "--os", "macos"])
        return buf.getvalue()

    # main() always stamps its own now_ts from the real wall clock (there is
    # no injectable clock), so every fixture here is built relative to real
    # time.time() rather than a fixed historical epoch - otherwise a scan
    # timestamped near a fixed past epoch would always read as STALE.
    def test_header_is_first_line_and_not_stale_for_a_fresh_scan(self):
        out = self._run(time.time())
        self.assertTrue(out.splitlines()[0].startswith("scanned:"))
        self.assertNotIn("STALE", out.splitlines()[0])

    def test_root_row_shows_100_percent_of_its_own_root(self):
        out = self._run(time.time())
        prefix = classify.human_size(166_000_000_000) + " "
        team_line = [l for l in out.splitlines() if l.startswith(prefix)][0]
        self.assertIn("100%", team_line)

    def test_footer_labels_the_overlap_between_reclaimable_and_review_only(self):
        out = self._run(time.time())
        self.assertIn("review only:", out)
        self.assertIn("includes the reclaimable bytes above", out)

    def test_footer_prints_unreadable_bytes_and_entry_count_never_zero_bytes(self):
        out = self._run(time.time())
        self.assertIn("unreadable (need sudo):", out)
        self.assertIn("404 entries", out)
        self.assertNotIn("0B across 404 entries", out)

    def test_reclaimable_row_shows_its_manifest_command_as_method(self):
        out = self._run(time.time())
        self.assertIn("npm cache clean --force", out)


# ---------------------------------------------------------------------------
# TCC - the Full Disk Access footer note
# ---------------------------------------------------------------------------

class TestFullDiskAccessNote(unittest.TestCase):
    def setUp(self):
        classify.HOME = "/Users/me"

    def _row(self, path, action="proposed", root="/"):
        return {"path": path, "bytes": 100, "action": action, "root": root}

    def test_silent_when_granted(self):
        scan_data = {"full_disk_access": {"granted": True, "probed": []}}
        rows = [self._row("/Users/me/Library/Logs")]
        self.assertEqual(classify.full_disk_access_note(scan_data, rows), "")

    def test_silent_when_no_library_rows_are_affected(self):
        scan_data = {"full_disk_access": {"granted": False, "probed": []}}
        rows = [self._row("/Users/me/.cache/uv", action="ask")]
        self.assertEqual(classify.full_disk_access_note(scan_data, rows), "")

    def test_missing_key_is_treated_as_absent_not_a_crash(self):
        # An older scan payload predating this field: absent is the
        # conservative reading, but there's still nothing to warn about
        # here since no row sits under ~/Library.
        rows = [self._row("/Users/me/.cache/uv", action="ask")]
        self.assertEqual(classify.full_disk_access_note({}, rows), "")

    def test_missing_key_still_warns_when_a_library_row_is_proposed(self):
        rows = [self._row("/Users/me/Library/Logs")]
        note = classify.full_disk_access_note({}, rows)
        self.assertIn("no Full Disk Access", note)

    def test_appears_when_absent_and_a_library_row_is_proposed(self):
        scan_data = {"full_disk_access": {"granted": False, "probed": []}}
        rows = [self._row("/Users/me/Library/Logs")]
        note = classify.full_disk_access_note(scan_data, rows)
        self.assertIn("no Full Disk Access", note)
        self.assertIn("System Settings", note)
        self.assertIn("Full Disk Access", note)

    def test_appears_for_an_ask_row_too_not_only_proposed(self):
        scan_data = {"full_disk_access": {"granted": False, "probed": []}}
        rows = [self._row("/Users/me/Library/Developer/Xcode/DerivedData", action="ask")]
        note = classify.full_disk_access_note(scan_data, rows)
        self.assertIn("no Full Disk Access", note)

    def test_review_only_and_protected_library_rows_do_not_count(self):
        scan_data = {"full_disk_access": {"granted": False, "probed": []}}
        rows = [
            self._row("/Users/me/Library/Mail", action="protected"),
            self._row("/Users/me/Library/SomeRandomDir", action="review only"),
        ]
        self.assertEqual(classify.full_disk_access_note(scan_data, rows), "")

    def test_non_library_rows_never_count(self):
        scan_data = {"full_disk_access": {"granted": False, "probed": []}}
        rows = [self._row("/Users/me/.cache/uv", action="ask"),
                self._row("/Volumes/External/GameLibrary", action="review only")]
        self.assertEqual(classify.full_disk_access_note(scan_data, rows), "")

    def test_counts_multiple_affected_rows_excluding_caches(self):
        scan_data = {"full_disk_access": {"granted": False, "probed": []}}
        rows = [
            self._row("/Users/me/Library/Logs", action="proposed"),
            self._row("/Users/me/Library/Caches/BraveSoftware", action="proposed"),
            self._row("/Users/me/Library/Developer/Xcode/DerivedData", action="ask"),
        ]
        note = classify.full_disk_access_note(scan_data, rows)
        # Logs and DerivedData count, but Caches is excluded (verified reachable)
        self.assertIn("2 rows", note)
        self.assertNotIn("3 rows", note)

    def test_singular_count_is_grammatical(self):
        scan_data = {"full_disk_access": {"granted": False, "probed": []}}
        rows = [self._row("/Users/me/Library/Logs")]
        note = classify.full_disk_access_note(scan_data, rows)
        self.assertIn("1 row outside ~/Library/Caches", note)
        self.assertNotIn("1 rows", note)

    def test_cache_only_rows_produce_no_note(self):
        # ~/Library/Caches is verified reachable without Full Disk Access,
        # so cache-only rows should not generate a warning.
        scan_data = {"full_disk_access": {"granted": False, "probed": []}}
        rows = [
            self._row("/Users/me/Library/Caches/BraveSoftware", action="proposed"),
            self._row("/Users/me/Library/Caches/VSCode.ShipIt", action="proposed"),
        ]
        note = classify.full_disk_access_note(scan_data, rows)
        self.assertEqual(note, "")

    def test_nested_library_caches_rows_are_deduplicated_and_excluded(self):
        # Mirrors render_table's own dedup: du emits one row per directory
        # at every depth, so a parent and its child are the same reclaim
        # target and must count once. But all Caches are excluded.
        scan_data = {"full_disk_access": {"granted": False, "probed": []}}
        rows = [
            self._row("/Users/me/Library/Caches/BraveSoftware", action="proposed"),
            self._row("/Users/me/Library/Caches/BraveSoftware/Cache", action="proposed"),
        ]
        note = classify.full_disk_access_note(scan_data, rows)
        self.assertEqual(note, "")

    def test_bare_library_root_itself_counts(self):
        scan_data = {"full_disk_access": {"granted": False, "probed": []}}
        rows = [self._row("/Users/me/Library", action="proposed")]
        note = classify.full_disk_access_note(scan_data, rows)
        self.assertIn("1 row outside ~/Library/Caches", note)

    def test_derived_data_still_counts_as_affected(self):
        # ~/Library/Developer/Xcode/DerivedData is NOT under Caches and is genuinely
        # unknown whether it needs FD Access, so it stays in the warning.
        scan_data = {"full_disk_access": {"granted": False, "probed": []}}
        rows = [self._row("/Users/me/Library/Developer/Xcode/DerivedData", action="ask")]
        note = classify.full_disk_access_note(scan_data, rows)
        self.assertIn("1 row outside ~/Library/Caches", note)

    def test_mixed_cache_and_non_cache_rows_counts_only_non_cache(self):
        # When both Caches and Logs are proposed, only Logs counts in the warning.
        scan_data = {"full_disk_access": {"granted": False, "probed": []}}
        rows = [
            self._row("/Users/me/Library/Caches/Homebrew", action="proposed"),
            self._row("/Users/me/Library/Logs", action="proposed"),
        ]
        note = classify.full_disk_access_note(scan_data, rows)
        self.assertIn("1 row outside ~/Library/Caches", note)

    def test_a_path_that_merely_starts_with_library_as_a_string_is_not_counted(self):
        # /Users/me/LibraryStuff must not match the ~/Library prefix check.
        scan_data = {"full_disk_access": {"granted": False, "probed": []}}
        rows = [self._row("/Users/me/LibraryStuff", action="proposed")]
        self.assertEqual(classify.full_disk_access_note(scan_data, rows), "")


class TestMainIntegrationFullDiskAccessNote(unittest.TestCase):
    def setUp(self):
        self._real_home = classify.HOME
        self.addCleanup(self._restore_home)

    def _restore_home(self):
        classify.HOME = self._real_home

    def _write_scan(self, now, granted, library_row=True):
        rows = [{"path": "/", "bytes": 114_000_000_000, "mtime": now}]
        if library_row:
            rows.append({"path": "/Users/me/Library/Logs", "bytes": 73_000_000, "mtime": now})
        scan_data = {
            "roots": [{"root": "/System/Volumes/Data", "scanned_at": now - 1800,
                       "rows": rows, "files": [], "projects": [],
                       "unreadable_count": 0, "transient_count": 0}],
            "apps": [], "snapshots": [],
            "mounts": [{"mount": "/System/Volumes/Data", "used_bytes": 114_000_000_000}],
            "docker": {"available": False, "reason": "daemon down", "bytes": 0},
            "full_disk_access": {"granted": granted, "probed": []},
        }
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(scan_data, tmp)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return tmp.name

    def _run(self, now, granted, library_row=True):
        classify.HOME = "/Users/me"
        path = self._write_scan(now, granted, library_row)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            classify.main([path, "--os", "macos"])
        return buf.getvalue()

    def test_note_appears_after_the_footer_when_ungranted_with_a_library_row(self):
        out = self._run(time.time(), granted=False)
        self.assertIn("no Full Disk Access", out)
        self.assertIn("1 row outside ~/Library/Caches", out)
        # It comes after the existing footer lines, not before them.
        self.assertLess(out.index("local snapshots:"), out.index("no Full Disk Access"))

    def test_note_is_silent_when_access_is_granted(self):
        out = self._run(time.time(), granted=True)
        self.assertNotIn("Full Disk Access", out)

    def test_note_is_silent_when_no_library_rows_are_proposed(self):
        out = self._run(time.time(), granted=False, library_row=False)
        self.assertNotIn("Full Disk Access", out)


# ---------------------------------------------------------------------------
# FIX 1 - PATH column truncation keeps METHOD aligned
# ---------------------------------------------------------------------------

class TestFitPath(unittest.TestCase):
    def test_shorter_than_width_is_unchanged(self):
        self.assertEqual(classify._fit_path("/short/path", width=52), "/short/path")

    def test_exactly_width_is_unchanged(self):
        path = "/" + "b" * 51  # 52 chars
        self.assertEqual(len(path), 52)
        self.assertEqual(classify._fit_path(path, width=52), path)

    def test_longer_than_width_is_truncated_to_exactly_width(self):
        path = "/Users/me/projects/" + ("deeply-nested-dir/" * 5) + "node_modules"
        out = classify._fit_path(path, width=52)
        self.assertEqual(len(out), 52)

    def test_truncation_keeps_the_original_tail_with_a_leading_ellipsis(self):
        path = "/Users/me/projects/" + ("deeply-nested-dir/" * 5) + "node_modules"
        out = classify._fit_path(path, width=52)
        self.assertTrue(out.startswith("..."))
        self.assertEqual(out[3:], path[-49:])


class TestRenderTablePathTruncation(unittest.TestCase):
    def setUp(self):
        classify.HOME = "/Users/me"

    def test_long_path_is_truncated_in_the_rendered_table(self):
        long_path = "/Volumes/External/projects/" + ("deeply-nested-dir/" * 5) + "node_modules"
        rows = [{"path": long_path, "bytes": 100, "age_days": 1, "tier": "free",
                 "action": "proposed", "root": "/", "method": "trash"}]
        out = classify.render_table(rows, {"/": 1000})
        line = [l for l in out.splitlines() if "trash" in l][0]
        self.assertIn("...", line)
        self.assertNotIn(long_path, line)

    def test_home_abbreviation_runs_before_truncation(self):
        # Chosen so the RAW path (with the full HOME prefix) is over the
        # width and would need truncation, but the ~-ABBREVIATED form is
        # short enough that it should NOT be truncated at all. This only
        # comes out right if ~ substitution happens before the width check.
        suffix = "/" + "x" * 45  # raw path: 9 ("/Users/me") + 46 = 55 chars (> 52)
        long_under_home = classify.HOME + suffix
        abbreviated = "~" + suffix  # 47 chars (<= 52) - must render untruncated
        self.assertGreater(len(long_under_home), 52)
        self.assertLessEqual(len(abbreviated), 52)
        rows = [{"path": long_under_home, "bytes": 100, "age_days": 1, "tier": "free",
                 "action": "proposed", "root": "/", "method": "trash"}]
        out = classify.render_table(rows, {"/": 1000})
        line = [l for l in out.splitlines() if "trash" in l][0]
        self.assertIn(abbreviated, line)
        self.assertNotIn("...", line)
        self.assertNotIn("/Users/me", line)


class TestRenderTableMethodAlignment(unittest.TestCase):
    """The important one: METHOD is the one column an agent acts on
    directly, so a long PATH must never shift it out of column with the
    header or with other rows."""

    def test_method_starts_at_the_same_offset_regardless_of_path_length(self):
        short_path = "/Users/me/.npm"
        boundary_path = "/" + "b" * 51  # exactly 52 chars, unchanged by _fit_path
        long_path = "/Volumes/External/projects/" + ("deeply-nested-dir/" * 5) + "node_modules"
        self.assertEqual(len(boundary_path), 52)
        rows = [
            {"path": short_path, "bytes": 700, "age_days": 5, "tier": "cheap",
             "action": "proposed", "root": "/", "method": "rm"},
            {"path": boundary_path, "bytes": 600, "age_days": 9, "tier": "free",
             "action": "proposed", "root": "/", "method": "trash"},
            {"path": long_path, "bytes": 500, "age_days": 12, "tier": "project",
             "action": "ask", "root": "/", "method": "npm cache clean --force"},
        ]
        out = classify.render_table(rows, {"/": 1000})
        lines = out.splitlines()
        method_offset = lines[0].index("METHOD")
        data_lines = [l for l in lines[1:] if l.strip()]
        self.assertEqual(len(data_lines), 3)
        self.assertEqual(data_lines[0][method_offset:], "rm")
        self.assertEqual(data_lines[1][method_offset:], "trash")
        self.assertEqual(data_lines[2][method_offset:], "npm cache clean --force")


class TestRenderAppsPathTruncation(unittest.TestCase):
    def test_long_app_name_is_truncated_to_the_app_column_width(self):
        long_name = "Extremely Long Application Display Name For Testing.app"
        self.assertGreater(len(long_name), 36)
        apps = [{"path": "/Applications/" + long_name, "bytes": 100, "last_used": "2026-01-01"}]
        out = classify.render_apps(apps)
        line = out.splitlines()[1]
        self.assertIn("...", line)

    def test_short_app_name_is_unchanged(self):
        apps = [{"path": "/Applications/Xcode.app", "bytes": 100, "last_used": "2026-01-01"}]
        out = classify.render_apps(apps)
        self.assertIn("Xcode.app", out)
        self.assertNotIn("...", out)


if __name__ == "__main__":
    unittest.main()
