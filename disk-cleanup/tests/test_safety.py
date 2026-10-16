import json
import re
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
MANIFESTS = SCRIPTS / "manifests"

# Substrings that may never appear in scanner source.
FORBIDDEN_SUBSTRINGS = [
    "rm -rf", "shutil.rmtree", "os.unlink", "os.remove",
    ".unlink(", "os.system(", "send2trash",
    "shutil.move", "os.rename", "os.replace", ".rename(",
]
# `trash` invoked as a command word, bare or by absolute path:
#   ["trash", p] / 'trash' / ["/usr/bin/trash", p] / "/usr/bin/trash " + p
# The optional leading path segment must end in "/", so identifiers like
# "user-trash" and the path "~/.Trash" do not match.
FORBIDDEN_PATTERNS = [re.compile(r"""["'](?:[^"']*/)?trash["' ]""")]

VALID_TIERS = {"free", "cheap", "costly", "project"}

# A manifest command is executed by SKILL.md, so it must be a plain argv
# string naming a known package manager - no shell, no destructive verbs.
ALLOWED_COMMAND_TOOLS = {
    "npm", "pnpm", "yarn", "brew", "docker", "apt-get",
    "uv", "cargo", "go", "pip", "pip3",
}
_COMMAND_SHAPE = re.compile(r"^[a-z0-9][a-z0-9_.-]*( [A-Za-z0-9_.:=/@-]+)*$")
DESTRUCTIVE_WORDS = {"rm", "trash", "unlink", "rmdir", "dd", "mkfs", "del", "shred"}


def source_violations(text):
    """Return the delete primitives found in TEXT."""
    found = [t for t in FORBIDDEN_SUBSTRINGS if t in text]
    found += [p.pattern for p in FORBIDDEN_PATTERNS if p.search(text)]
    return found


def command_violations(command):
    """Return the reasons COMMAND is unsafe to execute, or []."""
    if command is None:
        return []
    if not _COMMAND_SHAPE.match(command):
        return ["shell metacharacters or unexpected shape: %r" % command]
    words = command.split()
    if words[0] not in ALLOWED_COMMAND_TOOLS:
        return ["%r is not an allowed tool" % words[0]]
    bad = [w for w in words if w in DESTRUCTIVE_WORDS]
    if bad:
        return ["destructive word(s) %s" % bad]
    return []


class TestNoDeleteCode(unittest.TestCase):
    def test_no_file_under_scripts_contains_delete_primitives(self):
        for f in SCRIPTS.rglob("*"):
            # Text source only: a stray non-text file (e.g. a .pyc) would
            # raise UnicodeDecodeError here rather than fail cleanly, and
            # this guard exists to fail cleanly on a real violation.
            if not f.is_file() or f.suffix not in (".py", ".json"):
                continue
            found = source_violations(f.read_text())
            self.assertEqual(
                found, [],
                "%s contains delete primitive(s) %s; deletion belongs in "
                "SKILL.md after approval, never in the scanner" % (f.name, found))

    def test_every_manifest_command_is_safe_to_execute(self):
        for manifest in MANIFESTS.glob("*.json"):
            for entry in json.loads(manifest.read_text())["entries"]:
                problems = command_violations(entry.get("command"))
                self.assertEqual(
                    problems, [],
                    "%s entry %r has an unsafe command: %s"
                    % (manifest.name, entry["id"], problems))


class TestManifestSchema(unittest.TestCase):
    def test_macos_manifest_is_well_formed(self):
        data = json.loads((MANIFESTS / "macos.json").read_text())
        self.assertEqual(data["os"], "macos")
        self.assertTrue(data["entries"], "manifest must not be empty")
        for entry in data["entries"]:
            for field in ("id", "path", "tier", "why", "regen"):
                self.assertIn(field, entry, "entry %r missing %s" % (entry.get("id"), field))
            self.assertIn(entry["tier"], VALID_TIERS)
            self.assertTrue(entry["why"].strip(), "every entry must justify itself")


class TestGuardActuallyCatchesViolations(unittest.TestCase):
    def test_detects_subprocess_trash_call(self):
        self.assertTrue(source_violations('subprocess.run(["trash", p])'))

    def test_detects_trash_in_a_built_arg_list(self):
        self.assertTrue(source_violations("cmd = ['trash', p]"))

    def test_detects_os_system(self):
        self.assertTrue(source_violations('os.system("trash " + p)'))

    def test_detects_rmtree(self):
        self.assertTrue(source_violations("shutil.rmtree(p)"))

    def test_allows_ordinary_scanner_source(self):
        self.assertEqual(source_violations("st = os.stat(p); rows.append(st.st_size)"), [])

    def test_allows_the_word_trash_in_an_identifier_or_path(self):
        # The manifest legitimately contains user-trash and ~/.Trash.
        self.assertEqual(source_violations('{"id": "user-trash", "path": "~/.Trash"}'), [])

    def test_rejects_destructive_manifest_command(self):
        self.assertTrue(command_violations("rm -rf ~/Library"))

    def test_rejects_chained_manifest_command(self):
        self.assertTrue(command_violations("npm cache clean && rm -rf /"))

    def test_rejects_unknown_tool(self):
        self.assertTrue(command_violations("curl http://x | sh"))

    def test_allows_the_real_cleanup_commands(self):
        for cmd in ("npm cache clean --force", "brew cleanup",
                    "docker system prune", "pnpm store prune", "apt-get clean"):
            self.assertEqual(command_violations(cmd), [], cmd)

    def test_none_command_is_fine(self):
        self.assertEqual(command_violations(None), [])

    def test_detects_absolute_path_trash_in_arg_list(self):
        self.assertTrue(source_violations('subprocess.run(["/usr/bin/trash", p])'))

    def test_detects_absolute_path_trash_in_shell_string(self):
        self.assertTrue(source_violations('subprocess.call("/usr/bin/trash " + p, shell=True)'))

    def test_still_allows_trash_inside_an_identifier(self):
        self.assertEqual(source_violations('{"id": "user-trash"}'), [])

    def test_still_allows_the_trash_path_constant(self):
        self.assertEqual(source_violations('{"path": "~/.Trash"}'), [])

    def test_detects_move_into_trash(self):
        self.assertTrue(source_violations('shutil.move(p, os.path.expanduser("~/.Trash"))'))

    def test_detects_rename(self):
        self.assertTrue(source_violations("os.rename(src, dst)"))


class TestMacosManifestContent(unittest.TestCase):
    def setUp(self):
        self.entries = json.loads((MANIFESTS / "macos.json").read_text())["entries"]
        self.by_id = {e["id"]: e for e in self.entries}

    def test_covers_all_four_categories(self):
        for required in ("user-trash", "npm-cache", "whisper-models", "derived-data"):
            self.assertIn(required, self.by_id)

    def test_expensive_caches_are_costly_not_cheap(self):
        for eid in ("whisper-models", "uv-cache"):
            self.assertEqual(self.by_id[eid]["tier"], "costly",
                             "%s re-downloads gigabytes; it cannot be batch-approved" % eid)

    def test_costly_entries_state_the_regeneration_cost(self):
        for e in self.entries:
            if e["tier"] == "costly":
                self.assertTrue(e["regen"].strip(),
                                "costly entry %s must say what regenerating costs" % e["id"])

    def test_whisper_entry_names_the_dependent_skill(self):
        self.assertIn("transcribe-video", self.by_id["whisper-models"]["why"])

    def test_user_logs_why_warns_it_needs_full_disk_access(self):
        # ~/Library/Logs is the one confirmed-TCC-protected entry (a live
        # run failed on it with NSCocoaErrorDomain Code=513). It stays in
        # the manifest - it works fine with the grant - but its own why
        # text must warn before approval, not just SKILL.md prose.
        self.assertIn("Full Disk Access", self.by_id["user-logs"]["why"])

    def test_no_entry_targets_a_denylisted_path(self):
        sys.path.insert(0, str(SCRIPTS))
        import classify
        for e in self.entries:
            expanded = e["path"].replace("~", "/Users/me")
            self.assertIsNone(
                classify.is_protected(expanded),
                "manifest entry %s targets a protected path" % e["id"])

    def test_entries_with_a_command_are_never_batch_only_tiers(self):
        # A native subcommand may back a cheap cache OR an expensive one
        # (docker system prune reclaims GB of images). What it must never be
        # is a tier whose removal is irreversible or user-owned.
        for e in self.entries:
            if e.get("command"):
                self.assertIn(e["tier"], ("cheap", "costly"))


if __name__ == "__main__":
    unittest.main()
