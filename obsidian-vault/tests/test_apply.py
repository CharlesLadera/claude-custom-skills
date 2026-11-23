import json
import os
import subprocess
import tempfile
import unittest

from helpers import git_env, git_log, make_vault, read, write

import apply as A
import vault as V

git_env()

FILES = {
    "Morning Routine.md": "---\ntags: [f]\n---\nSee [[Reading List (MOC)#Snapshot|plan]] and [Viz](<Viz Tool.html>).\n",
    "Reading List (MOC).md": ("Notes: [[Morning Routine]] · ![[Morning Routine]]\n"
                               "| a | [[Morning Routine]] |\n"
                               "[md](Morning%20Routine.md) [ang](<Morning Routine.md#Top>)\n"
                               "[[Morning Routine#Top]]\n"),
    "Viz Tool.html": "<html></html>",
    "other/keep.md": ("[[Morning Routine|custom]] and [[other/keep]] and `[[Morning Routine]]`\n"
                      "[abs](<Morning Routine.md>) [rel](../Morning%20Routine.md#A%20B)"),
    "board.canvas": json.dumps({"nodes": [{"id": "1", "type": "file", "file": "Morning Routine.md"}],
                                "edges": []}),
}


def plan_file(plan):
    fd, p = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(plan, f)
    return p


class TestRenameRewritesLinks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = make_vault(FILES)
        plan = {"description": "kebab the reading notes", "moves": [
            {"from": "Morning Routine.md", "to": "lib/morning-routine.md"},
            {"from": "Reading List (MOC).md", "to": "lib/reading-list-moc.md"},
            {"from": "Viz Tool.html", "to": "lib/viz-tool.html"}]}
        cls.rc = A.main(["--vault", cls.root, "run", plan_file(plan)])

    def test_run_succeeded_and_committed(self):
        self.assertEqual(self.rc, 0)
        self.assertIn("obsidian-vault: kebab the reading notes", git_log(self.root))
        self.assertFalse(os.path.exists(os.path.join(self.root, "Morning Routine.md")))

    def test_wikilinks_keep_their_text_as_alias(self):
        moc = read(self.root, "lib/reading-list-moc.md")
        self.assertIn("Notes: [[morning-routine|Morning Routine]]", moc)
        self.assertIn("![[morning-routine]]", moc)          # embeds get no alias
        self.assertIn("| a | [[morning-routine\\|Morning Routine]] |", moc)  # table pipe escaped
        self.assertIn("[[morning-routine#Top]]", moc)       # heading kept, no alias

    def test_markdown_links(self):
        moc = read(self.root, "lib/reading-list-moc.md")
        self.assertIn("[md](morning-routine.md)", moc)
        self.assertIn("[ang](<morning-routine.md#Top>)", moc)
        keep = read(self.root, "other/keep.md")
        self.assertIn("[abs](<lib/morning-routine.md>)", keep)
        self.assertIn("[rel](../lib/morning-routine.md#A%20B)", keep)
        routine = read(self.root, "lib/morning-routine.md")
        self.assertIn("[[reading-list-moc#Snapshot|plan]]", routine)
        self.assertIn("[Viz](<viz-tool.html>)", routine)

    def test_other_notes_and_code(self):
        keep = read(self.root, "other/keep.md")
        self.assertIn("[[morning-routine|custom]]", keep)
        self.assertIn("[[other/keep]]", keep)
        self.assertIn("`[[Morning Routine]]`", keep)  # code untouched

    def test_canvas(self):
        data = json.loads(read(self.root, "board.canvas"))
        self.assertEqual(data["nodes"][0]["file"], "lib/morning-routine.md")

    def test_every_link_still_resolves(self):
        v = V.Vault(self.root)
        for p in v.notes:
            for l in V.parse_links(v.read(p)):
                if p != "CLAUDE.md":
                    self.assertIsNotNone(v.resolve(l.path, p), "%s: %s" % (p, l.raw))


class TestMoveEdgeCases(unittest.TestCase):
    def test_ambiguous_basename_gets_path_form(self):
        root = make_vault({"a/note.md": "", "b/x.md": "[[note]]", "c/note-src.md": ""})
        plan = {"description": "dup", "moves": [{"from": "c/note-src.md", "to": "c/note.md"}]}
        self.assertEqual(A.main(["--vault", root, "run", plan_file(plan)]), 0)
        self.assertEqual(read(root, "b/x.md"), "[[note]]")  # still the a/ note: unchanged

        plan = {"description": "move a", "moves": [{"from": "a/note.md", "to": "z/deep/note.md"}]}
        self.assertEqual(A.main(["--vault", root, "run", plan_file(plan)]), 0)
        self.assertEqual(read(root, "b/x.md"), "[[deep/note]]")

    def test_reports_emptied_folders_in_preview(self):
        import io
        import contextlib
        root = make_vault({"old/only.md": "", "keep/a.md": "", "keep/b.md": ""})
        plan = plan_file({"description": "e", "moves": [{"from": "old/only.md", "to": "new/only.md"},
                                                        {"from": "keep/a.md", "to": "new/a.md"}]})
        for cmd in ("preview", "run"):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(A.main(["--vault", root, cmd, plan]), 0)
            self.assertEqual(json.loads(buf.getvalue())["emptied_folders"], ["old"], cmd)
        self.assertTrue(os.path.isdir(os.path.join(root, "old")))  # reported, never removed

    def test_text_edits_exactly_once(self):
        root = make_vault({"n.md": "see [[gone]] and [[gone]]", "m.md": "see [[gone]]", "t.md": ""})
        twice = plan_file({"description": "x", "edits": [{"path": "n.md", "find": "[[gone]]", "replace": "[[t]]"}]})
        self.assertEqual(A.main(["--vault", root, "run", twice]), 2)
        self.assertEqual(read(root, "n.md"), "see [[gone]] and [[gone]]")
        plan = plan_file({"description": "retarget", "moves": [{"from": "m.md", "to": "z/m.md"}],
                          "edits": [{"path": "z/m.md", "find": "[[gone]]", "replace": "[[t]]"}]})
        self.assertEqual(A.main(["--vault", root, "run", plan]), 0)
        self.assertEqual(read(root, "z/m.md"), "see [[t]]")

    def test_moved_note_relative_links_fixed(self):
        root = make_vault({"a/n.md": "[t](../b/t.md) [[t]]", "b/t.md": ""})
        plan = {"description": "m", "moves": [{"from": "a/n.md", "to": "x/y/n.md"}]}
        self.assertEqual(A.main(["--vault", root, "run", plan_file(plan)]), 0)
        self.assertEqual(read(root, "x/y/n.md"), "[t](../../b/t.md) [[t]]")

    def test_case_only_rename(self):
        root = make_vault({"Note.md": "", "l.md": "[[Note]]"})
        plan = {"description": "c", "moves": [{"from": "Note.md", "to": "note.md"}], "aliasOnRename": False}
        self.assertEqual(A.main(["--vault", root, "run", plan_file(plan)]), 0)
        self.assertIn("note.md", os.listdir(root))
        self.assertEqual(read(root, "l.md"), "[[Note]]")  # case-insensitive: still resolves

    def test_refuses_existing_target_and_bad_paths(self):
        root = make_vault({"a.md": "", "b.md": ""})
        for to in ("b.md", "../out.md", ".obsidian/x.md", "a.txt"):
            plan = {"description": "x", "moves": [{"from": "a.md", "to": to}]}
            self.assertEqual(A.main(["--vault", root, "run", plan_file(plan)]), 2, to)
        self.assertTrue(os.path.exists(os.path.join(root, "a.md")))


class TestFrontmatter(unittest.TestCase):
    def test_add_only_missing(self):
        text, added, skipped = A.add_frontmatter("---\ntags: [a]\nempty:\n---\nbody",
                                                 {"tags": ["z"], "created": "2026-01-01", "empty": "v"})
        self.assertEqual((added, skipped), (["created", "empty"], ["tags"]))
        self.assertEqual(text, "---\ntags: [a]\ncreated: 2026-01-01\nempty: v\n---\nbody")

    def test_prepend_when_missing(self):
        text, added, _ = A.add_frontmatter("# T\n", {"tags": ["x", "y"], "title": "A: b"})
        self.assertEqual(text, '---\ntags:\n  - x\n  - y\ntitle: "A: b"\n---\n# T\n')

    def test_plan_frontmatter_after_move(self):
        root = make_vault({"Raw.md": "hello"})
        plan = {"description": "f", "moves": [{"from": "Raw.md", "to": "raw.md"}],
                "frontmatter": [{"path": "raw.md", "set": {"tags": ["t"]}}]}
        self.assertEqual(A.main(["--vault", root, "run", plan_file(plan)]), 0)
        self.assertEqual(read(root, "raw.md"), "---\ntags:\n  - t\n---\nhello")


class TestGitSafety(unittest.TestCase):
    def test_dirty_vault_needs_checkpoint(self):
        root = make_vault({"a.md": ""})
        write(root, "stray.md", "uncommitted")
        plan = plan_file({"description": "mv", "moves": [{"from": "a.md", "to": "b.md"}]})
        self.assertEqual(A.main(["--vault", root, "run", plan]), 2)
        self.assertTrue(os.path.exists(os.path.join(root, "a.md")))
        self.assertEqual(A.main(["--vault", root, "run", plan, "--checkpoint"]), 0)
        log = git_log(root)
        self.assertIn("obsidian-vault: checkpoint before mv", log)
        self.assertIn("obsidian-vault: mv", log)

    def test_os_and_app_noise_is_not_dirt(self):
        root = make_vault({"a.md": "", ".DS_Store": "x", "sub/.DS_Store": "x", ".obsidian/workspace.json": "{}"})
        for rel in (".DS_Store", "sub/.DS_Store", ".obsidian/workspace.json"):
            write(root, rel, "changed")
        self.assertEqual(A.dirty(root), [])
        self.assertIsNone(A.commit(root, "only noise"))  # no commit labelled as work
        self.assertNotIn("only noise", git_log(root))
        plan = plan_file({"description": "mv", "moves": [{"from": "a.md", "to": "b.md"}]})
        self.assertEqual(A.main(["--vault", root, "run", plan]), 0)  # no --checkpoint needed
        self.assertEqual(subprocess.run(["git", "-C", root, "status", "--porcelain"], capture_output=True,
                                        text=True).stdout, "")  # noise went into the commit

    def test_revert_undoes_a_run(self):
        root = make_vault({"a.md": "", "l.md": "[[a]]"})
        A.main(["--vault", root, "run", plan_file({"description": "mv", "moves": [{"from": "a.md", "to": "z/b.md"}]})])
        subprocess.run(["git", "-C", root, "revert", "--no-edit", "HEAD"], check=True, capture_output=True)
        self.assertEqual(read(root, "l.md"), "[[a]]")
        self.assertTrue(os.path.exists(os.path.join(root, "a.md")))

    def test_preview_writes_nothing(self):
        root = make_vault({"a.md": "", "l.md": "[[a]]"})
        A.main(["--vault", root, "preview", plan_file({"description": "p", "moves": [{"from": "a.md", "to": "b.md"}]})])
        self.assertEqual(read(root, "l.md"), "[[a]]")
        self.assertTrue(os.path.exists(os.path.join(root, "a.md")))

    def test_vault_flag_after_subcommand_and_bad_plan(self):
        root = make_vault({"a.md": ""})
        bad = plan_file({"no": "description"})
        self.assertEqual(A.main(["preview", bad, "--vault", root]), 2)
        self.assertEqual(A.main(["--vault", root, "preview", "/dev/null"]), 2)

    def test_git_verbs_are_limited(self):
        with self.assertRaises(A.PlanError):
            A.git_run(".", "push")


class TestPathSafety(unittest.TestCase):
    def test_hostile_move_targets(self):
        root = make_vault({"a.md": ""})
        for to in (".git/hooks/a.md", "x/.git/a.md", "x\\..\\..\\a.md", "C:/a.md", "x/../../a.md",
                   "x//a.md", "/tmp/a.md", "x/./a.md"):
            plan = {"description": "x", "moves": [{"from": "a.md", "to": to}]}
            self.assertEqual(A.main(["--vault", root, "run", plan_file(plan)]), 2, to)
        self.assertTrue(os.path.exists(os.path.join(root, "a.md")))

    def test_import_into_git_refused(self):
        root = make_vault({"a.md": ""})
        src = tempfile.mkdtemp()
        write(src, "pre-commit", "#!/bin/sh\necho owned\n")
        for to in (".git/hooks", "x/../.git/hooks", "notes\\x"):
            self.assertEqual(A.main(["--vault", root, "import", os.path.join(src, "pre-commit"), "--to", to]), 2, to)
        self.assertFalse(os.path.exists(os.path.join(root, ".git", "hooks", "pre-commit")))

    def test_symlinks_out_of_vault_refused(self):
        outside = tempfile.mkdtemp()
        write(outside, "secret.md", "[[gone]]")
        root = make_vault({"a.md": ""})
        os.symlink(os.path.join(outside, "secret.md"), os.path.join(root, "link.md"))
        os.symlink(outside, os.path.join(root, "outdir"))
        subprocess.run(["git", "-C", root, "add", "-A"], check=True)
        subprocess.run(["git", "-C", root, "commit", "-qm", "links"], check=True)
        edit = plan_file({"description": "e", "edits": [{"path": "link.md", "find": "[[gone]]", "replace": "x"}]})
        self.assertEqual(A.main(["--vault", root, "run", edit]), 2)
        self.assertEqual(read(outside, "secret.md"), "[[gone]]")
        move = plan_file({"description": "m", "moves": [{"from": "a.md", "to": "outdir/a.md"}]})
        self.assertEqual(A.main(["--vault", root, "run", move]), 2)
        self.assertFalse(os.path.exists(os.path.join(outside, "a.md")))

    def test_symlinks_into_own_git_refused(self):
        root = make_vault({"a.md": ""})
        os.symlink(os.path.join(root, ".git", "hooks"), os.path.join(root, "hooks"))
        write(root, ".git/hooks/pre-commit.md", "[[gone]]")
        os.symlink(os.path.join(root, ".git", "hooks", "pre-commit.md"), os.path.join(root, "pc.md"))
        subprocess.run(["git", "-C", root, "add", "-A"], check=True)
        subprocess.run(["git", "-C", root, "commit", "-qm", "links"], check=True)
        move = plan_file({"description": "m", "moves": [{"from": "a.md", "to": "hooks/a.md"}]})
        self.assertEqual(A.main(["--vault", root, "run", move]), 2)
        self.assertFalse(os.path.exists(os.path.join(root, ".git", "hooks", "a.md")))
        edit = plan_file({"description": "e", "edits": [{"path": "pc.md", "find": "[[gone]]", "replace": "x"}]})
        self.assertEqual(A.main(["--vault", root, "run", edit]), 2)
        self.assertEqual(read(root, ".git/hooks/pre-commit.md"), "[[gone]]")


class TestImport(unittest.TestCase):
    def test_copies_and_refuses_overwrite(self):
        root = make_vault({"lib/existing.md": ""})
        src = tempfile.mkdtemp()
        write(src, "new.md", "n")
        write(src, "existing.md", "e")
        self.assertEqual(A.main(["--vault", root, "import", os.path.join(src, "new.md"), "--to", "lib"]), 0)
        self.assertEqual(read(root, "lib/new.md"), "n")
        self.assertTrue(os.path.exists(os.path.join(src, "new.md")))  # source kept
        self.assertEqual(A.main(["--vault", root, "import", os.path.join(src, "existing.md"), "--to", "lib"]), 2)
        self.assertEqual(read(root, "lib/existing.md"), "")


if __name__ == "__main__":
    unittest.main()
