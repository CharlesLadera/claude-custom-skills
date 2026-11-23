import json
import os
import subprocess
import tempfile
import unittest

from helpers import SYMLINKS, git_env, git_log, make_vault, read, write

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
        self.assertIn("[[morning-routine#Top|Morning Routine > Top]]", moc)  # shown text kept

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

    @unittest.skipUnless(SYMLINKS, "symlinks need Developer Mode or admin on Windows")
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

    @unittest.skipUnless(SYMLINKS, "symlinks need Developer Mode or admin on Windows")
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

def run_main(argv):
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = A.main(argv)
    return rc, json.loads(buf.getvalue())


class TestGateFixes(unittest.TestCase):
    """Bugs and gaps found by the pre-PR gate (2026-11-23)."""

    def test_chained_and_swapped_moves_apply_fully(self):
        root = make_vault({"a.md": "A", "b.md": "B", "x.md": "X", "y.md": "Y", "l.md": "[[a]] [[b]] [[x]] [[y]]"})
        plan = {"description": "chain and swap", "aliasOnRename": False, "moves": [
            {"from": "a.md", "to": "b.md"}, {"from": "b.md", "to": "c.md"},
            {"from": "x.md", "to": "y.md"}, {"from": "y.md", "to": "x.md"}]}
        self.assertEqual(A.main(["--vault", root, "run", plan_file(plan)]), 0)
        self.assertEqual((read(root, "b.md"), read(root, "c.md")), ("A", "B"))
        self.assertEqual((read(root, "x.md"), read(root, "y.md")), ("Y", "X"))
        self.assertEqual(read(root, "l.md"), "[[b]] [[c]] [[y]] [[x]]")

    def test_failed_run_rolls_back_moves_and_writes(self):
        root = make_vault({"a.md": "A", "m.md": "M", "l.md": "[[a]] [[m]]"})
        vault = V.Vault(root)
        plan = {"description": "x", "moves": [{"from": "a.md", "to": "q/a2.md"}, {"from": "m.md", "to": "z/m2.md"}]}
        moves, edits, _ = A.compute(vault, plan)
        write(root, "z/m2.md", "appeared")  # second target shows up between compute and execute
        with self.assertRaises(A.PlanError):
            A.execute(vault, plan, moves, edits)
        self.assertEqual((read(root, "a.md"), read(root, "m.md")), ("A", "M"))
        self.assertFalse(os.path.exists(os.path.join(root, "q", "a2.md")))
        self.assertEqual(read(root, "l.md"), "[[a]] [[m]]")
        self.assertEqual(read(root, "z/m2.md"), "appeared")
        self.assertEqual([n for n in os.listdir(root) if "obsidian-vault" in n], [])

    @unittest.skipUnless(SYMLINKS, "symlinks need Developer Mode or admin on Windows")
    def test_symlinked_linking_note_refused_before_any_move(self):
        root = make_vault({"a.md": "A", "real/n.md": "[[a]]"})
        os.symlink(os.path.join(root, "real", "n.md"), os.path.join(root, "n-link.md"))
        subprocess.run(["git", "-C", root, "add", "-A"], check=True)
        subprocess.run(["git", "-C", root, "commit", "-qm", "link"], check=True)
        plan = plan_file({"description": "m", "moves": [{"from": "a.md", "to": "b.md"}]})
        self.assertEqual(A.main(["--vault", root, "preview", plan]), 2)
        self.assertEqual(A.main(["--vault", root, "run", plan]), 2)
        self.assertTrue(os.path.exists(os.path.join(root, "a.md")))

    def test_import_refuses_duplicate_basenames_in_one_batch(self):
        root = make_vault({"a.md": ""})
        s1, s2 = tempfile.mkdtemp(), tempfile.mkdtemp()
        write(s1, "n.md", "one")
        write(s2, "N.md", "two")
        self.assertEqual(A.main(["--vault", root, "import", os.path.join(s1, "n.md"), os.path.join(s2, "N.md"),
                                 "--to", "imp"]), 2)
        self.assertFalse(os.path.exists(os.path.join(root, "imp")))

    def test_parenthesised_paths_survive_markdown_rewrites(self):
        root = make_vault({"Notes/a (draft).md": "", "l.md": "[x](Notes/a%20(draft).md) [y](<Notes/a (draft).md>)"})
        v = V.Vault(root)
        self.assertEqual([v.resolve(l.path, "l.md") for l in V.parse_links(read(root, "l.md"))],
                         ["Notes/a (draft).md"] * 2)
        plan = {"description": "m", "moves": [{"from": "Notes/a (draft).md", "to": "Done/b (final).md"}]}
        self.assertEqual(A.main(["--vault", root, "run", plan_file(plan)]), 0)
        text = read(root, "l.md")
        v = V.Vault(root)
        self.assertEqual([v.resolve(l.path, "l.md") for l in V.parse_links(text)], ["Done/b (final).md"] * 2)
        self.assertNotIn(".md)", text.replace(".md)", ".md]", 2).replace("]", ""))  # no stray tail

    def test_line_endings_and_odd_bytes_preserved(self):
        root = make_vault({"y.md": ""})
        with open(os.path.join(root, "crlf.md"), "wb") as f:
            f.write(b"a\r\nsee [[y]]\r\nbad \xff byte\r\n")
        subprocess.run(["git", "-C", root, "add", "-A"], check=True)
        subprocess.run(["git", "-C", root, "commit", "-qm", "crlf"], check=True)
        plan = {"description": "m", "aliasOnRename": False, "moves": [{"from": "y.md", "to": "z.md"}],
                "frontmatter": [{"path": "crlf.md", "set": {"tags": ["t"]}}]}
        self.assertEqual(A.main(["--vault", root, "run", plan_file(plan)]), 0)
        with open(os.path.join(root, "crlf.md"), "rb") as f:
            self.assertEqual(f.read(), b"---\r\ntags:\r\n  - t\r\n---\r\na\r\nsee [[z]]\r\nbad \xff byte\r\n")

    def test_case_only_rename_never_overwrites_a_leftover_temp(self):
        root = make_vault({"Note.md": "real", "l.md": "[[Note]]"})
        plan = {"description": "c", "moves": [{"from": "Note.md", "to": "note.md"}]}
        self.assertEqual(A.main(["--vault", root, "run", plan_file(plan)]), 0)
        self.assertEqual(read(root, "note.md"), "real")
        self.assertEqual([n for n in os.listdir(root) if "tmp" in n], [])

    def test_frontmatter_value_types(self):
        self.assertEqual(A.yaml_item("publish", True), ["publish: true"])
        self.assertEqual(A.yaml_item("n", 3), ["n: 3"])
        for bad in (None, {"a": 1}):
            with self.assertRaises(A.PlanError):
                A.yaml_item("k", bad)

    def test_fills_empty_list_value(self):
        text, added, skipped = A.add_frontmatter("---\ntags: []\n---\nbody", {"tags": ["x"]})
        self.assertEqual((added, skipped), (["tags"], []))
        self.assertEqual(text, "---\ntags:\n  - x\n---\nbody")

    def test_two_moves_to_one_target_refused(self):
        root = make_vault({"a.md": "", "b.md": ""})
        plan = plan_file({"description": "x", "moves": [{"from": "a.md", "to": "c.md"}, {"from": "b.md", "to": "C.md"}]})
        self.assertEqual(A.main(["--vault", root, "run", plan]), 2)
        self.assertTrue(os.path.exists(os.path.join(root, "a.md")) and os.path.exists(os.path.join(root, "b.md")))

    def test_alias_on_rename_false(self):
        root = make_vault({"Morning Routine.md": "", "l.md": "[[Morning Routine]]"})
        plan = {"description": "r", "aliasOnRename": False,
                "moves": [{"from": "Morning Routine.md", "to": "lib/morning-routine.md"}]}
        self.assertEqual(A.main(["--vault", root, "run", plan_file(plan)]), 0)
        self.assertEqual(read(root, "l.md"), "[[morning-routine]]")

    def test_markdown_link_without_extension_keeps_none(self):
        root = make_vault({"Morning Routine.md": "", "l.md": "[x](Morning%20Routine)"})
        plan = {"description": "r", "moves": [{"from": "Morning Routine.md", "to": "lib/morning-routine.md"}]}
        self.assertEqual(A.main(["--vault", root, "run", plan_file(plan)]), 0)
        self.assertEqual(read(root, "l.md"), "[x](lib/morning-routine)")

    def test_vault_must_be_git_top_level(self):
        root = make_vault({"sub/a.md": "", "sub/l.md": "[[a]]"})
        plan = plan_file({"description": "x", "moves": [{"from": "a.md", "to": "b.md"}]})
        rc, out = run_main(["--vault", os.path.join(root, "sub"), "run", plan])
        self.assertEqual(rc, 2)
        self.assertIn("not the top of its git repo", out["error"])
        self.assertTrue(os.path.exists(os.path.join(root, "sub", "a.md")))

    def test_import_of_file_already_in_vault_refused(self):
        root = make_vault({"a.md": "x"})
        self.assertEqual(A.main(["--vault", root, "import", os.path.join(root, "a.md"), "--to", "fin"]), 2)
        self.assertFalse(os.path.exists(os.path.join(root, "fin")))

    def test_checkpoint_subcommand(self):
        root = make_vault({"a.md": ""})
        rc, out = run_main(["--vault", root, "checkpoint", "-m", "nothing"])
        self.assertEqual((rc, out["commit"]), (0, None))
        write(root, "b.md", "new")
        rc, out = run_main(["--vault", root, "checkpoint", "-m", "msg"])
        self.assertTrue(out["commit"])
        self.assertIn("obsidian-vault: msg", git_log(root))


class TestRegateGaps(unittest.TestCase):
    """Second gate round (2026-11-23): rollback of writes, YAML special words, import of a folder."""

    def _plan_with_unwritable_second_note(self):
        root = make_vault({"a.md": "A", "l1.md": "[[a]] one", "l2.md": "[[a]] two"})
        os.chmod(os.path.join(root, "l2.md"), 0o444)
        self.addCleanup(os.chmod, os.path.join(root, "l2.md"), 0o644)
        vault = V.Vault(root)
        plan = {"description": "x", "aliasOnRename": False, "moves": [{"from": "a.md", "to": "b.md"}]}
        moves, edits, _ = A.compute(vault, plan)
        self.assertEqual(list(edits), ["l1.md", "l2.md"])
        return root, vault, plan, moves, edits

    @unittest.skipIf(os.name != "nt" and os.geteuid() == 0, "root can write read-only files")
    def test_rollback_restores_rewritten_notes(self):
        root, vault, plan, moves, edits = self._plan_with_unwritable_second_note()
        with self.assertRaises(A.PlanError) as cm:
            A.execute(vault, plan, moves, edits)
        self.assertIn("nothing was changed", str(cm.exception))
        self.assertEqual((read(root, "l1.md"), read(root, "l2.md")), ("[[a]] one", "[[a]] two"))
        self.assertEqual(read(root, "a.md"), "A")
        self.assertFalse(os.path.exists(os.path.join(root, "b.md")))

    @unittest.skipIf(os.name != "nt" and os.geteuid() == 0, "root can write read-only files")
    def test_rollback_never_overwrites_a_file_that_appeared(self):
        root, vault, plan, moves, edits = self._plan_with_unwritable_second_note()
        real_inside = A.inside

        def inside(r, rel, what):
            if what == "edited file" and rel == "l2.md":
                write(root, "a.md", "new a")  # someone recreates a.md before the rollback runs
            return real_inside(r, rel, what)
        A.inside = inside
        self.addCleanup(setattr, A, "inside", real_inside)
        with self.assertRaises(A.PlanError) as cm:
            A.execute(vault, plan, moves, edits)
        self.assertIn("rollback was incomplete", str(cm.exception))
        self.assertEqual(read(root, "a.md"), "new a")
        self.assertEqual(read(root, "b.md"), "A")  # the moved file stays where it went

    def test_yaml_special_words_are_quoted(self):
        text, _, _ = A.add_frontmatter("body", {"status": "no", "flag": "true", "x": "null", "y": "On"})
        self.assertEqual(text, '---\nstatus: "no"\nflag: "true"\nx: "null"\ny: "On"\n---\nbody')

    def test_import_of_a_folder_refused(self):
        root = make_vault({"a.md": ""})
        rc, out = run_main(["--vault", root, "import", tempfile.mkdtemp(), "--to", "x"])
        self.assertEqual(rc, 2)
        self.assertIn("not a file", out["error"])
        self.assertFalse(os.path.exists(os.path.join(root, "x")))


class TestRound2(unittest.TestCase):
    """Second /code-review round (2026-11-23)."""

    def test_relative_md_link_never_lands_on_a_lookalike(self):
        root = make_vault({"sub/foo.md": "root one", "old.md": "", "a/n.md": "[t](../old.md)"})
        plan = {"description": "m", "moves": [{"from": "old.md", "to": "a/sub/foo.md"}]}
        self.assertEqual(A.main(["--vault", root, "run", plan_file(plan)]), 0)
        v = V.Vault(root)
        (l,) = V.parse_links(read(root, "a/n.md"))
        self.assertEqual(v.resolve(l.path, "a/n.md"), "a/sub/foo.md")

    def test_empty_quoted_values_are_replaced_not_duplicated(self):
        for empty in ('""', "''", "~", "null"):
            text, added, _ = A.add_frontmatter("---\ntags: %s\ntitle: x\n---\nbody\n" % empty, {"tags": ["a"]})
            self.assertEqual(added, ["tags"])
            self.assertEqual(text.count("tags:"), 1, empty)

    def test_nested_mapping_counts_as_a_value(self):
        src = "---\ntitle: x\ncreated:\n  date: 2026-01-01\n---\nbody"
        text, added, skipped = A.add_frontmatter(src, {"created": "2026-02-02"})
        self.assertEqual((text, added, skipped), (src, [], ["created"]))

    def test_bom_note_keeps_one_frontmatter_block(self):
        text, added, _ = A.add_frontmatter("﻿---\ntitle: x\n---\nbody\n", {"tags": ["a"]})
        self.assertEqual(text, "﻿---\ntitle: x\ntags:\n  - a\n---\nbody\n")

    def test_bad_plan_makes_no_checkpoint(self):
        root = make_vault({"n.md": "x x"})
        write(root, "stray.md", "dirty")
        plan = plan_file({"description": "bad", "edits": [{"path": "n.md", "find": "x", "replace": "y"}]})
        self.assertEqual(A.main(["--vault", root, "run", plan, "--checkpoint"]), 2)
        self.assertNotIn("checkpoint before bad", git_log(root))

    def test_commit_failure_after_apply_says_so(self):
        root = make_vault({"a.md": "", "l.md": "[[a]]"})
        hook = os.path.join(root, ".git", "hooks", "pre-commit")
        with open(hook, "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(hook, 0o755)
        rc, out = run_main(["--vault", root, "run", plan_file({"description": "m", "moves": [{"from": "a.md", "to": "b.md"}]})])
        self.assertEqual(rc, 2)
        self.assertIn("applied but not committed", out["error"])
        self.assertEqual(out["report"]["moves"], [{"from": "a.md", "to": "b.md"}])

    def test_import_os_error_is_reported_as_json(self):
        root = make_vault({"notes.md": ""})
        src = tempfile.mkdtemp()
        write(src, "a.md", "a")
        rc, out = run_main(["--vault", root, "import", os.path.join(src, "a.md"), "--to", "notes.md/sub"])
        self.assertEqual(rc, 2)
        self.assertIn("error", out)


class TestRound3(unittest.TestCase):
    """Third /code-review round (2026-11-23)."""

    def test_rollback_of_a_swap_and_a_reverse_chain_is_complete(self):
        root = make_vault({"a.md": "A", "b.md": "B", "c.md": "C", "x.md": "X", "l1.md": "[[q]]", "l2.md": "[[q]]",
                           "q.md": "Q"})
        os.chmod(os.path.join(root, "l2.md"), 0o444)
        self.addCleanup(os.chmod, os.path.join(root, "l2.md"), 0o644)
        vault = V.Vault(root)
        plan = {"description": "x", "aliasOnRename": False, "moves": [
            {"from": "a.md", "to": "b.md"}, {"from": "b.md", "to": "a.md"},      # swap
            {"from": "x.md", "to": "y.md"}, {"from": "c.md", "to": "x.md"},      # reverse-ordered chain
            {"from": "q.md", "to": "q2.md"}]}
        moves, edits, _ = A.compute(vault, plan)
        if os.name != "nt" and os.geteuid() == 0:
            self.skipTest("root can write read-only files")
        with self.assertRaises(A.PlanError) as cm:
            A.execute(vault, plan, moves, edits)
        self.assertIn("nothing was changed", str(cm.exception))
        for name, text in (("a.md", "A"), ("b.md", "B"), ("c.md", "C"), ("x.md", "X"), ("q.md", "Q"),
                           ("l1.md", "[[q]]"), ("l2.md", "[[q]]")):
            self.assertEqual(read(root, name), text, name)
        self.assertEqual(sorted(n for n in os.listdir(root) if n.endswith(".md")),
                         ["CLAUDE.md", "a.md", "b.md", "c.md", "l1.md", "l2.md", "q.md", "x.md"])

    def test_write_that_fails_midway_is_restored(self):
        root = make_vault({"a.md": "A", "l1.md": "[[a]] one"})
        vault = V.Vault(root)
        plan = {"description": "x", "edits": [{"path": "l1.md", "find": "one", "replace": "bad \ud800 text"}]}
        moves, edits, _ = A.compute(vault, plan)
        with self.assertRaises(A.PlanError) as cm:
            A.execute(vault, plan, moves, edits)
        self.assertIn("nothing was changed", str(cm.exception))
        self.assertEqual(read(root, "l1.md"), "[[a]] one")

    def test_import_failure_names_copied_files_and_never_overwrites(self):
        root = make_vault({"a.md": ""})
        src = tempfile.mkdtemp()
        write(src, "one.md", "1")
        write(src, "two.md", "2")
        os.chmod(os.path.join(src, "two.md"), 0o000)
        self.addCleanup(os.chmod, os.path.join(src, "two.md"), 0o644)
        if os.name != "nt" and os.geteuid() == 0:
            self.skipTest("root can read anything")
        rc, out = run_main(["--vault", root, "import", os.path.join(src, "one.md"), os.path.join(src, "two.md"),
                            "--to", "imp"])
        self.assertEqual(rc, 2)
        self.assertIn("imp/one.md", out["error"])
        self.assertIn("not committed", out["error"])

    def test_canvas_text_cards_rewritten_and_bad_canvas_tolerated(self):
        root = make_vault({"Old Note.md": "", "board.canvas": json.dumps({"nodes": [
                               {"id": "1", "type": "text", "text": "see [[Old Note]]"}, "junk", {"id": "2"}]}),
                           "weird.canvas": "[]"})
        plan = {"description": "r", "aliasOnRename": False, "moves": [{"from": "Old Note.md", "to": "old-note.md"}]}
        self.assertEqual(A.main(["--vault", root, "run", plan_file(plan)]), 0)
        data = json.loads(read(root, "board.canvas"))
        self.assertEqual(data["nodes"][0]["text"], "see [[old-note]]")
        self.assertEqual(read(root, "weird.canvas"), "[]")

    def test_number_like_strings_are_quoted(self):
        self.assertEqual(A.yaml_item("version", "1.10"), ['version: "1.10"'])
        self.assertEqual(A.yaml_item("tags", ["2024", "007", "1e3", "ok-2024"]),
                         ["tags:", '  - "2024"', '  - "007"', '  - "1e3"', "  - ok-2024"])

    def test_mixed_line_endings_only_new_lines_change(self):
        src = "---\r\ntitle: x\r\n---\r\nline one\nline two\r\n"
        text, added, _ = A.add_frontmatter(src, {"tags": ["a"]})
        self.assertEqual(text, "---\r\ntitle: x\r\ntags:\r\n  - a\r\n---\r\nline one\nline two\r\n")


class TestReviewerShouldFix(unittest.TestCase):
    def test_refusals_change_nothing(self):
        root = make_vault({"a.md": "A", "l.md": "[[a]]"})
        for plan in ({"description": "x", "moves": [{"from": "missing.md", "to": "m.md"}]},
                     {"description": "x", "moves": [{"from": "a.md", "to": "b.md"}],
                      "edits": [{"path": "a.md", "find": "A", "replace": "B"}]}):
            self.assertEqual(A.main(["--vault", root, "run", plan_file(plan)]), 2)
        self.assertEqual((read(root, "a.md"), read(root, "l.md")), ("A", "[[a]]"))
        self.assertEqual(git_log(root).strip(), "init")

    def test_empty_list_value_written_as_brackets(self):
        text, _, _ = A.add_frontmatter("body", {"tags": []})
        self.assertTrue(text.startswith("---\ntags: []\n---\n"))


class TestRound4(unittest.TestCase):
    """Fourth gate round (2026-11-23): /code-review and the tests reviewer."""

    def test_alias_kept_in_table_row_without_leading_pipe(self):
        root = make_vault({"Old Name.md": "", "home.md": "a | b\n--- | ---\ncol1 | [[Old Name]]\nx | y\n"})
        plan = {"description": "r", "moves": [{"from": "Old Name.md", "to": "res/new-name.md"}]}
        self.assertEqual(A.main(["--vault", root, "run", plan_file(plan)]), 0)
        self.assertEqual(read(root, "home.md"), "a | b\n--- | ---\ncol1 | [[new-name\\|Old Name]]\nx | y\n")

    def test_plain_line_with_pipe_link_is_not_a_table(self):
        root = make_vault({"Old Name.md": "", "home.md": "see [[x|y]] and [[Old Name]]\n"})
        plan = {"description": "r", "moves": [{"from": "Old Name.md", "to": "new-name.md"}]}
        self.assertEqual(A.main(["--vault", root, "run", plan_file(plan)]), 0)
        self.assertEqual(read(root, "home.md"), "see [[x|y]] and [[new-name|Old Name]]\n")

    def test_alias_for_heading_and_md_suffix_links(self):
        root = make_vault({"Old Name.md": "# Sec", "home.md": "[[Old Name.md]] [[Old Name#Sec]]\n"})
        plan = {"description": "r", "moves": [{"from": "Old Name.md", "to": "res/new-name.md"}]}
        self.assertEqual(A.main(["--vault", root, "run", plan_file(plan)]), 0)
        self.assertEqual(read(root, "home.md"), "[[new-name|Old Name]] [[new-name#Sec|Old Name > Sec]]\n")

    def test_case_only_duplicate_targets_refused_by_compute(self):
        root = make_vault({"a.md": "", "b.md": ""})
        plan = {"description": "x", "moves": [{"from": "a.md", "to": "c.md"}, {"from": "b.md", "to": "C.md"}]}
        with self.assertRaises(A.PlanError) as cm:
            A.compute(V.Vault(root), plan)
        self.assertIn("two moves target the same path", str(cm.exception))
        self.assertEqual(A.main(["--vault", root, "preview", plan_file(plan)]), 2)

    def test_interrupt_rolls_back_and_reraises(self):
        root = make_vault({"a.md": "A", "b.md": "B"})
        vault = V.Vault(root)
        plan = {"description": "x", "moves": [{"from": "a.md", "to": "x/a.md"}, {"from": "b.md", "to": "x/b.md"}]}
        moves, edits, _ = A.compute(vault, plan)
        real = os.rename
        calls = []

        def rename(a, b):
            calls.append(a)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real(a, b)
        A.os.rename = rename
        try:
            with self.assertRaises(KeyboardInterrupt):
                A.execute(vault, plan, moves, edits)
        finally:
            A.os.rename = real
        self.assertEqual((read(root, "a.md"), read(root, "b.md")), ("A", "B"))
        self.assertEqual([n for n in os.listdir(root) if n.startswith(".obsidian-vault-")], [])

    def test_checkpoint_refuses_staging_leftovers(self):
        root = make_vault({"a.md": "A"})
        os.rename(os.path.join(root, "a.md"), os.path.join(root, ".obsidian-vault-0-a.md"))
        plan = plan_file({"description": "x", "edits": []})
        rc, out = run_main(["--vault", root, "run", plan, "--checkpoint"])
        self.assertEqual(rc, 2)
        self.assertIn(".obsidian-vault-0-a.md", out["error"])
        self.assertEqual(git_log(root).strip(), "init")

    def test_more_yaml_lookalikes_quoted(self):
        self.assertEqual(A.yaml_value("0x1F"), '"0x1F"')
        self.assertEqual(A.yaml_value("0o17"), '"0o17"')
        self.assertEqual(A.yaml_value("foo "), '"foo "')

    def test_empty_list_item_counts_as_empty_and_closing_line_kept(self):
        text, added, _ = A.add_frontmatter("---\ntags:\n  -\ntitle: x\n...\nbody\n", {"tags": ["a"]})
        self.assertEqual(added, ["tags"])
        self.assertEqual(text, "---\ntitle: x\ntags:\n  - a\n...\nbody\n")

    def test_rollback_names_new_empty_folders(self):
        root = make_vault({"a.md": "A", "l1.md": "[[a]]", "l2.md": "[[a]]"})
        os.chmod(os.path.join(root, "l2.md"), 0o444)
        self.addCleanup(os.chmod, os.path.join(root, "l2.md"), 0o644)
        if os.name != "nt" and os.geteuid() == 0:
            self.skipTest("root can write read-only files")
        vault = V.Vault(root)
        plan = {"description": "x", "aliasOnRename": False, "moves": [{"from": "a.md", "to": "new/deep/a.md"}],
                "edits": [{"path": "l2.md", "find": "[[a]]", "replace": "[[a]] x"}]}
        moves, edits, _ = A.compute(vault, plan)
        with self.assertRaises(A.PlanError) as cm:
            A.execute(vault, plan, moves, edits)
        self.assertIn("no file was changed", str(cm.exception))
        self.assertIn("new/deep", str(cm.exception))
        self.assertEqual(read(root, "a.md"), "A")

    def test_malformed_plans_give_json_errors(self):
        root = make_vault({"a.md": "x"})
        for plan in ({"description": "x", "edits": [{"path": "a.md", "find": "x"}]},
                     {"description": "x", "moves": [{"from": "a.md"}]},
                     {"description": "x", "moves": [{"from": "a.md", "to": "b.md"}, {"from": "a.md", "to": "c.md"}]},
                     {"description": "x", "frontmatter": [{"path": "a.md", "set": ["tags"]}]},
                     {"description": "x", "moves": "a.md"}):
            rc, out = run_main(["--vault", root, "preview", plan_file(plan)])
            self.assertEqual(rc, 2, plan)
            self.assertIn("error", out)

    def test_broken_canvas_left_alone_during_run(self):
        root = make_vault({"a.md": "", "bad.canvas": "{oops", "ok.canvas": json.dumps({"nodes": []})})
        plan = {"description": "m", "moves": [{"from": "a.md", "to": "b.md"}]}
        self.assertEqual(A.main(["--vault", root, "run", plan_file(plan)]), 0)
        self.assertEqual(read(root, "bad.canvas"), "{oops")

    def test_incomplete_rollback_names_where_files_are(self):
        root = make_vault({"a.md": "A", "l1.md": "[[a]]", "l2.md": "[[a]]"})
        os.chmod(os.path.join(root, "l2.md"), 0o444)
        self.addCleanup(os.chmod, os.path.join(root, "l2.md"), 0o644)
        if os.name != "nt" and os.geteuid() == 0:
            self.skipTest("root can write read-only files")
        vault = V.Vault(root)
        plan = {"description": "x", "aliasOnRename": False, "moves": [{"from": "a.md", "to": "b.md"}]}
        moves, edits, _ = A.compute(vault, plan)
        real = os.rename
        n = []

        def rename(a, b):
            n.append(a)
            if len(n) == 3:  # the first undo rename (parking b.md)
                raise OSError("disk says no")
            return real(a, b)
        A.os.rename = rename
        try:
            with self.assertRaises(A.PlanError) as cm:
                A.execute(vault, plan, moves, edits)
        finally:
            A.os.rename = real
        self.assertIn("rollback was incomplete", str(cm.exception))
        self.assertIn("it is at b.md", str(cm.exception))


class TestRound5Plans(unittest.TestCase):
    def test_more_plan_shape_refusals(self):
        root = make_vault({"a.md": "x"})
        for plan, msg in (({"description": "x", "moves": ["a.md"]}, "plan moves[0] must be an object"),
                          ({"description": "x", "aliasOnRename": "yes"}, "plan aliasOnRename must be true or false")):
            rc, out = run_main(["--vault", root, "run", plan_file(plan)])
            self.assertEqual((rc, out["error"]), (2, msg))
        self.assertEqual(read(root, "a.md"), "x")
        self.assertEqual(git_log(root).strip(), "init")


class TestRound5Review(unittest.TestCase):
    """Fifth /code-review round (2026-11-23)."""

    def test_every_empty_spelling_replaced_once(self):
        for src in ('---\ntags: [""]\ntitle: x\n---\nbody\n', "---\ntags: [ , ]\ntitle: x\n---\nbody\n",
                    '---\ntitle: x\ntags:\n  - ""\nstatus: y\n---\nbody\n', '---\n"tags": []\ntitle: x\n---\nbody\n'):
            text, added, _ = A.add_frontmatter(src, {"tags": ["a"]})
            self.assertEqual(added, ["tags"], src)
            fm, _ = V.split_frontmatter(text)
            self.assertEqual(sum(1 for l in fm.split("\n") if l.lstrip('"').startswith("tags")), 1, text)
            self.assertNotIn('  - ""', text)
            self.assertEqual(V.parse_frontmatter(fm)["tags"], ["a"], text)

    def test_checkpoint_refuses_staging_leftovers(self):
        root = make_vault({"a.md": "A"})
        os.rename(os.path.join(root, "a.md"), os.path.join(root, ".obsidian-vault-0-a.md"))
        rc, out = run_main(["--vault", root, "checkpoint", "-m", "add x"])
        self.assertEqual(rc, 2)
        self.assertIn(".obsidian-vault-0-a.md", out["error"])
        self.assertEqual(git_log(root).strip(), "init")

    def test_long_names_can_move(self):
        long = "n" * 240 + ".md"
        root = make_vault({long: "L", "l.md": "[[%s]]" % long[:-3]})
        plan = {"description": "r", "aliasOnRename": False, "moves": [{"from": long, "to": "short.md"}]}
        self.assertEqual(A.main(["--vault", root, "run", plan_file(plan)]), 0)
        self.assertEqual(read(root, "short.md"), "L")
        self.assertEqual(read(root, "l.md"), "[[short]]")

    def test_canvas_group_background_follows_a_move(self):
        root = make_vault({"assets/map.png": "png", "board.canvas": json.dumps(
            {"nodes": [{"id": "g", "type": "group", "background": "assets/map.png"}]})})
        plan = {"description": "m", "moves": [{"from": "assets/map.png", "to": "04 - Archive/map.png"}]}
        self.assertEqual(A.main(["--vault", root, "run", plan_file(plan)]), 0)
        self.assertEqual(json.loads(read(root, "board.canvas"))["nodes"][0]["background"], "04 - Archive/map.png")

    def test_missing_vault_folder_is_an_error(self):
        rc, out = run_main(["--vault", "/surely/not/a/vault", "preview", plan_file({"description": "x"})])
        self.assertEqual(rc, 2)
        self.assertIn("not a folder", out["error"])


class TestRound6(unittest.TestCase):
    def test_duplicate_key_with_a_value_is_never_dropped(self):
        src = "---\ntags: [a]\ntitle: x\ntags:\n---\nbody"
        text, added, skipped = A.add_frontmatter(src, {"tags": ["new"]})
        self.assertEqual((text, added, skipped), (src, [], ["tags"]))

    def test_tilde_vault_path_is_expanded(self):
        root = make_vault({"a.md": ""})
        from unittest import mock
        with mock.patch.dict(os.environ, {"HOME": os.path.dirname(root)}):
            rc, out = run_main(["--vault", "~/" + os.path.basename(root), "preview", plan_file({"description": "x"})])
        self.assertEqual(rc, 0, out)


class TestRound6Rollback(unittest.TestCase):
    def test_failed_restore_is_reported_with_the_note(self):
        import builtins
        root = make_vault({"a.md": "A", "l1.md": "[[a]] one", "l2.md": "[[a]] two"})
        vault = V.Vault(root)
        plan = {"description": "x", "aliasOnRename": False, "moves": [{"from": "a.md", "to": "b.md"}],
                "edits": [{"path": "l1.md", "find": "one", "replace": "1"}, {"path": "l2.md", "find": "two", "replace": "2"}]}
        moves, edits, _ = A.compute(vault, plan)
        writes = []

        def fake_open(path, mode="r", *a, **k):
            if "w" in mode:
                writes.append(os.path.basename(path))
                if len(writes) >= 2:  # l2's write fails, then l1's restore fails too
                    raise OSError("disk full")
            return builtins.open(path, mode, *a, **k)
        A.open = fake_open
        try:
            with self.assertRaises(A.PlanError) as cm:
                A.execute(vault, plan, moves, edits)
        finally:
            del A.open
        self.assertIn("rollback was incomplete", str(cm.exception))
        self.assertIn("restore l1.md", str(cm.exception))


class TestRound7(unittest.TestCase):
    def test_list_after_blank_or_comment_counts_as_filled(self):
        for src in ("---\ntitle: x\ntags:\n\n- a\n---\nb", "---\ntitle: x\ntags:\n# list\n- a\n---\nb"):
            text, added, skipped = A.add_frontmatter(src, {"tags": ["new"]})
            self.assertEqual((text, added, skipped), (src, [], ["tags"]))

    def test_empty_list_after_blank_is_replaced_cleanly(self):
        text, added, _ = A.add_frontmatter('---\ntitle: x\ntags:\n\n- ""\n---\nb', {"tags": ["a"]})
        self.assertEqual(added, ["tags"])
        self.assertNotIn('- ""', text)
        self.assertEqual(V.parse_frontmatter(V.split_frontmatter(text)[0])["tags"], ["a"])


class TestRound8(unittest.TestCase):
    def test_hash_prefixed_values_are_values(self):
        src = "---\ntags:\n  - #project\n---\nb"
        self.assertEqual(A.add_frontmatter(src, {"tags": ["new"]}), (src, [], ["tags"]))

    def test_comments_are_never_removed(self):
        text, _, _ = A.add_frontmatter('---\ntags:\n\n# c\n- ""\nx: 1\n---\nb', {"tags": ["a"]})
        self.assertEqual(text, "---\n\n# c\nx: 1\ntags:\n  - a\n---\nb")

    def test_any_text_in_a_key_means_skip(self):
        for src in ("---\ntags: # fill in later\n---\nb", "---\ntags: [] # todo\n---\nb",
                    "---\ntags: [#a, #b]\n---\nb", "---\ntags:\n  - #project\n---\nb"):
            self.assertEqual(A.add_frontmatter(src, {"tags": ["x"]}), (src, [], ["tags"]), src)


class TestGate7(unittest.TestCase):
    def test_edit_with_no_match_refused(self):
        root = make_vault({"n.md": "no link here"})
        plan = plan_file({"description": "x", "edits": [{"path": "n.md", "find": "[[gone]]", "replace": "y"}]})
        rc, out = run_main(["--vault", root, "run", plan])
        self.assertEqual(rc, 2)
        self.assertIn("occurs 0 times", out["error"])
        self.assertEqual(read(root, "n.md"), "no link here")

    @unittest.skipUnless(SYMLINKS, "symlinks need Developer Mode or admin on Windows")
    def test_symlink_to_sibling_with_vault_prefix_is_outside(self):
        root = make_vault({"a.md": ""})
        sib = root + "-x"
        os.makedirs(sib)
        os.symlink(sib, os.path.join(root, "linked"))
        with self.assertRaises(A.PlanError) as cm:
            A.inside(root, "linked/n.md", "move target")
        self.assertIn("outside the vault", str(cm.exception))


class TestFinal(unittest.TestCase):
    def test_parser_veto_keeps_items_after_a_stray_line(self):
        src = "---\ntags:\nfoo\n- a\n---\nb"
        self.assertEqual(A.add_frontmatter(src, {"tags": ["n"]}), (src, [], ["tags"]))

    def test_null_items_are_empty(self):
        text, added, _ = A.add_frontmatter("---\ntags:\n  - null\n  - ~\n---\nb", {"tags": ["x"]})
        self.assertEqual((added, text), (["tags"], "---\ntags:\n  - x\n---\nb"))


class TestFinalGate(unittest.TestCase):
    def test_self_link_follows_rename_heading_self_link_stays(self):
        root = make_vault({"A.md": "# Top\nsee [[A]] and [[#Top]]"})
        plan = {"description": "r", "moves": [{"from": "A.md", "to": "b.md"}]}
        self.assertEqual(A.main(["--vault", root, "run", plan_file(plan)]), 0)
        self.assertEqual(read(root, "b.md"), "# Top\nsee [[b|A]] and [[#Top]]")

    @unittest.skipUnless(SYMLINKS, "symlinks need Developer Mode or admin on Windows")
    def test_target_under_hidden_symlinked_folder_refused(self):
        root = make_vault({"notes/x.md": "", "y.md": ""})
        os.symlink(os.path.join(root, "notes"), os.path.join(root, ".alias"))
        with self.assertRaises(A.PlanError):
            A.compute(V.Vault(root), {"description": "m", "moves": [{"from": "y.md", "to": ".alias/y.md"}]})

    def test_broken_links_left_alone(self):
        root = make_vault({"a.md": "", "n.md": "[[Nowhere]] [[a]]"})
        moves, edits, report = A.compute(V.Vault(root), {"description": "m", "aliasOnRename": False,
                                                        "moves": [{"from": "a.md", "to": "z/b.md"}]})
        self.assertEqual(edits["n.md"], "[[Nowhere]] [[b]]")
        self.assertNotIn("[[Nowhere]]", json.dumps(report["link_rewrites"]))

    def test_rollback_reports_a_failed_undo_move(self):
        root = make_vault({"a.md": "A", "l.md": "plain text"})
        vault = V.Vault(root)
        plan = {"description": "x", "aliasOnRename": False, "moves": [{"from": "a.md", "to": "b.md"}],
                "edits": [{"path": "l.md", "find": "plain", "replace": "edited"}]}
        moves, edits, _ = A.compute(vault, plan)
        real_rename, real_open = os.rename, open
        state = {"failing": False}

        def fake_open(path, mode="r", *a, **k):
            if "w" in mode and path.endswith("l.md") and not state["failing"]:
                state["failing"] = True
                raise OSError("write refused")
            return real_open(path, mode, *a, **k)

        def fake_rename(a, b):
            if state["failing"] and os.path.basename(b) == "a.md":
                raise OSError("cannot go back")
            return real_rename(a, b)
        A.open, A.os.rename = fake_open, fake_rename
        try:
            with self.assertRaises(A.PlanError) as cm:
                A.execute(vault, plan, moves, edits)
        finally:
            del A.open
            A.os.rename = real_rename
        self.assertIn("rollback was incomplete", str(cm.exception))
        self.assertIn("undo move a.md", str(cm.exception))
        self.assertIn("it is at .obsidian-vault-", str(cm.exception))

    def test_import_target_that_appears_is_not_overwritten(self):
        root = make_vault({"a.md": ""})
        src = tempfile.mkdtemp()
        write(src, "n.md", "new")
        real_copy = A.shutil.copy2

        def racing_copy(s, d, *a, **k):
            write(root, "imp/n.md", "someone else's")
            return real_copy(s, d, *a, **k)
        A.shutil.copy2 = racing_copy
        real_lexists = os.path.lexists
        try:
            # the target appears after planning: the pre-copy check must see it
            calls = {"n": 0}

            def lexists(p):
                if p.endswith(os.path.join("imp", "n.md")):
                    calls["n"] += 1
                    if calls["n"] > 1:
                        write(root, "imp/n.md", "someone else's")
                return real_lexists(p)
            A.os.path.lexists = lexists
            with self.assertRaises(A.PlanError) as cm:
                A.do_import(V.Vault(root), [os.path.join(src, "n.md")], "imp")
        finally:
            A.shutil.copy2 = real_copy
            A.os.path.lexists = real_lexists
        self.assertIn("appeared on disk", str(cm.exception))
        self.assertEqual(read(root, "imp/n.md"), "someone else's")


if __name__ == "__main__":
    unittest.main()
