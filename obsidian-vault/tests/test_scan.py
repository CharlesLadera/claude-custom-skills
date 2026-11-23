import argparse
import json
import unittest

from helpers import make_vault

import scan
import vault as V

FILES = {
    "Home.md": "---\ntags: [home]\ncreated: 2026-01-01\n---\n[[good-note]] [[Bad Name]] [[missing]]",
    "good-note.md": "---\ntags: [a]\ndate: 2026-01-01\n---\nBody mentions Bad Name in prose. #topic",
    "Bad Name.md": "---\ntags: [b]\ncreated: 2026-01-01\n---\nlinks [[good-note]]",
    "no-fm.md": "just text, links [[good-note]]",
    "empty-one.md": "---\ntags: [x]\ncreated: 2026-01-01\n---\n\n# Title only\n",
    "dir/good-note.md": "---\ntags: [c]\ncreated: 2026-01-01\nstatus: done\n---\n<% tp.date.now() %>",
    "00 - Inbox/Raw Capture.md": "whatever",
    "Reports/Daily Report.md": "[[nowhere]]",
    "dir/Alpha <> Beta.md": "---\ntags: [x]\ncreated: 2026-01-01\n---\n[[good-note]]",
    "Reports/Old: draft.md": "ignored for lint, not for portability",
}


class TestLint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v = V.Vault(make_vault(FILES, git=False))
        cls.res = scan.lint(cls.v, V.load_rules(cls.v.root), 0)
        cls.items = cls.res["items"]

    def paths(self, kind):
        return sorted(i.get("path") or i.get("source") for i in self.items[kind])

    def test_rules_come_from_claude_md(self):
        self.assertEqual(self.res["rules"], "CLAUDE.md")

    def test_broken_links_skip_ignored_folders(self):
        self.assertEqual([i["link"] for i in self.items["broken_links"]], ["[[missing]]"])

    def test_unportable_names(self):
        self.assertEqual(self.paths("unportable_names"), ["Reports/Old: draft.md", "dir/Alpha <> Beta.md"])

    def test_suggest_taken_and_untitled(self):
        v = V.Vault(make_vault({"To Do.md": "x", "b/to-do.md": "y", "Untitled 1.md": "z"}, git=False))
        items = {i["path"]: i for i in scan.lint(v, V.load_rules(v.root), 0)["items"]["naming"]}
        self.assertTrue(items["To Do.md"].get("suggest_taken"))
        self.assertTrue(items["Untitled 1.md"].get("name_from_content"))

    def test_naming_respects_exemptions(self):
        self.assertEqual(self.paths("naming"), ["Bad Name.md", "dir/Alpha <> Beta.md"])
        self.assertEqual(self.items["naming"][0]["suggest"], "bad-name.md")

    def test_frontmatter(self):
        self.assertEqual(self.paths("missing_frontmatter"), ["no-fm.md"])
        self.assertEqual(self.paths("missing_keys"), [])  # date satisfies created|date

    def test_empty_orphans_dupes_templater(self):
        self.assertEqual(self.paths("empty"), ["empty-one.md"])
        self.assertIn("empty-one.md", self.paths("orphans"))
        self.assertIn("no-fm.md", self.paths("orphans"))
        self.assertNotIn("Home.md", self.paths("orphans"))
        self.assertNotIn("good-note.md", self.paths("orphans"))
        self.assertEqual(self.items["duplicate_names"][0]["name"], "good-note")
        self.assertEqual(self.paths("templater_leftovers"), ["dir/good-note.md"])


class TestQueries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v = V.Vault(make_vault(FILES, git=False))

    def test_backlinks(self):
        res = scan.backlinks(self.v, "Bad Name")
        self.assertEqual([b["source"] for b in res["backlinks"]], ["Home.md"])

    def test_unlinked_mentions(self):
        res = scan.unlinked(self.v, "Bad Name.md")
        self.assertEqual([m["source"] for m in res["mentions"]], ["good-note.md"])

    def test_find(self):
        ns = argparse.Namespace(tag="topic", prop=None, text=None, name=None, folder=None)
        self.assertEqual([r["path"] for r in scan.find(self.v, ns)["results"]], ["good-note.md"])
        ns = argparse.Namespace(tag=None, prop="status=done", text=None, name=None, folder=None)
        self.assertEqual([r["path"] for r in scan.find(self.v, ns)["results"]], ["dir/good-note.md"])
        ns = argparse.Namespace(tag=None, prop=None, text="prose", name=None, folder=None)
        self.assertEqual(scan.find(self.v, ns)["results"][0]["matches"][0]["line"], 5)



class TestScanGaps(unittest.TestCase):
    def test_trailing_space_or_dot_is_unportable(self):
        v = V.Vault(make_vault({"dir /a.md": "x", "trail./b.md": "x", "ok/c.md": "x"}, git=False))
        res = scan.lint(v, V.load_rules(v.root), 0)
        self.assertEqual(sorted(i["path"] for i in res["items"]["unportable_names"]), ["dir /a.md", "trail./b.md"])

    def test_inbox_lists_only_inbox_notes(self):
        import datetime
        v = V.Vault(make_vault({"00 - Inbox/idea.md": "---\ntags: [x]\n---\n\nFirst line here\nmore",
                                "elsewhere.md": "no"}, git=False))

        class Today(datetime.date):
            @classmethod
            def today(cls):
                return cls(2099, 1, 1)
        old, scan.date = scan.date, Today
        try:
            res = scan.inbox(v, V.load_rules(v.root))
        finally:
            scan.date = old
        (n,) = res["notes"]
        self.assertEqual((n["path"], n["first_line"], n["chars"]), ("00 - Inbox/idea.md", "First line here", 20))
        self.assertGreater(n["age_days"], 20000)

    def test_cli_resolve_and_rules(self):
        import io
        import contextlib
        root = make_vault({"x/Note.md": ""}, git=False)
        for argv, key, want in ((["--vault", root, "resolve", "Note"], "resolves_to", "x/Note.md"),
                                (["rules", "--vault", root], "_source", "CLAUDE.md")):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                scan.main(argv)
            self.assertEqual(json.loads(buf.getvalue())[key], want)


class TestCanvasInbound(unittest.TestCase):
    def test_note_on_a_canvas_is_not_an_orphan(self):
        v = V.Vault(make_vault({"used.md": "---\ntags: [x]\ncreated: 2026-01-01\n---\nbody",
                                "board.canvas": json.dumps({"nodes": [{"id": "1", "type": "file", "file": "used.md"}]})},
                               git=False))
        res = scan.lint(v, V.load_rules(v.root), 0)
        self.assertNotIn("used.md", [i["path"] for i in res["items"]["orphans"]])


class TestScanShouldFix(unittest.TestCase):
    def test_missing_keys_listed(self):
        v = V.Vault(make_vault({"n.md": "---\ntags: [x]\n---\n[[CLAUDE]] body"}, git=False))
        items = scan.lint(v, V.load_rules(v.root), 0)["items"]["missing_keys"]
        self.assertEqual(items, [{"path": "n.md", "missing": ["created|date"]}])

    def test_cli_dispatch_for_every_command(self):
        import io
        import contextlib
        root = make_vault({"x/Note.md": "---\ntags: [t]\n---\nsee [[Other]]", "Other.md": "Note mentioned",
                           "00 - Inbox/i.md": "raw"}, git=False)
        v = V.Vault(root)
        direct = scan.lint(v, V.load_rules(v.root), 5)
        for argv, check in ((["--vault", root, "lint", "--limit", "5"], lambda o: o["summary"] == direct["summary"]),
                            (["--vault", root, "backlinks", "Other"], lambda o: o["count"] == 1),
                            (["--vault", root, "find", "--tag", "t"], lambda o: o["count"] == 1),
                            (["--vault", root, "unlinked", "x/Note.md"], lambda o: o["count"] == 1),
                            (["--vault", root, "inbox"], lambda o: o["count"] == 1)):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                scan.main(argv)
            self.assertTrue(check(json.loads(buf.getvalue())), argv)


class TestRound4Scan(unittest.TestCase):
    def test_canvas_text_card_counts_and_broken_canvas_skipped(self):
        v = V.Vault(make_vault({"used.md": "---\ntags: [x]\ncreated: 2026-01-01\n---\nbody",
                                "board.canvas": json.dumps({"nodes": [{"id": "1", "type": "text", "text": "[[used]]"}]}),
                                "bad.canvas": "{not json", "odd.canvas": json.dumps({"nodes": "x"})}, git=False))
        res = scan.lint(v, V.load_rules(v.root), 0)
        self.assertNotIn("used.md", [i["path"] for i in res["items"]["orphans"]])

    def test_find_filters_reject(self):
        v = V.Vault(make_vault({"01 - Projects/foo.md": "---\nstatus: open\n---\n",
                                "03 - Resources/foo.md": "---\nstatus: done\n---\n",
                                "03 - Resources/bar.md": "---\nstatus: done\n---\n"}, git=False))
        ns = lambda **k: argparse.Namespace(**dict(dict(tag=None, prop=None, text=None, name=None, folder=None), **k))
        paths = lambda n: sorted(r["path"] for r in scan.find(v, n)["results"])
        self.assertEqual(paths(ns(folder="03 - Resources")), ["03 - Resources/bar.md", "03 - Resources/foo.md"])
        self.assertEqual(paths(ns(name="^foo")), ["01 - Projects/foo.md", "03 - Resources/foo.md"])
        self.assertEqual(paths(ns(prop="status=done")), ["03 - Resources/bar.md", "03 - Resources/foo.md"])


class TestRound5Scan(unittest.TestCase):
    def test_templater_link_is_not_broken(self):
        v = V.Vault(make_vault({"n.md": "---\ntags: [x]\ncreated: 2026-01-01\n---\n[[<% tp.file.title %>]]"}, git=False))
        res = scan.lint(v, V.load_rules(v.root), 0)["items"]
        self.assertEqual(res["broken_links"], [])
        self.assertEqual([i["path"] for i in res["templater_leftovers"]], ["n.md"])

    def test_unknown_note_exits(self):
        root = make_vault({"a.md": ""}, git=False)
        with self.assertRaises(SystemExit) as cm:
            scan.main(["--vault", root, "backlinks", "nosuchnote"])
        self.assertIn("no file matches 'nosuchnote'", str(cm.exception))


class TestRound5Review(unittest.TestCase):
    def test_background_counts_and_bad_vault_exits(self):
        v = V.Vault(make_vault({"map.png": "x", "board.canvas": json.dumps(
            {"nodes": [{"id": "g", "type": "group", "background": "map.png"}]})}, git=False))
        res = scan.lint(v, V.load_rules(v.root), 0)
        self.assertNotIn("map.png", [i["path"] for i in res["items"]["orphans"]])
        with self.assertRaises(SystemExit) as cm:
            scan.main(["--vault", "/surely/not/a/vault", "lint"])
        self.assertIn("not a folder", str(cm.exception))


class TestRound6Scan(unittest.TestCase):
    def test_self_links_are_not_backlinks(self):
        v = V.Vault(make_vault({"Self.md": "# Heading\n[[Self]] [[#Heading]]", "other.md": "[[Self]]"}, git=False))
        res = scan.backlinks(v, "Self")
        self.assertEqual(([b["source"] for b in res["backlinks"]], res["count"]), (["other.md"], 1))

    def test_prop_without_value_needs_the_key(self):
        v = V.Vault(make_vault({"a.md": "---\nstatus: open\n---\n", "b.md": "---\ntags: [x]\n---\n"}, git=False))
        ns = argparse.Namespace(tag=None, prop="status", text=None, name=None, folder=None)
        self.assertEqual([r["path"] for r in scan.find(v, ns)["results"]], ["a.md"])

    def test_case_only_rename_is_not_taken(self):
        v = V.Vault(make_vault({"Foo.md": "x"}, git=False))
        (item,) = scan.lint(v, V.load_rules(v.root), 0)["items"]["naming"]
        self.assertEqual(item["suggest"], "foo.md")
        self.assertNotIn("suggest_taken", item)


class TestFinalScan(unittest.TestCase):
    def test_all_empty_list_is_a_missing_key(self):
        v = V.Vault(make_vault({"n.md": "---\ntags:\n  - #project\ncreated: 2026-01-01\n---\n[[CLAUDE]]"}, git=False))
        items = scan.lint(v, V.load_rules(v.root), 0)["items"]["missing_keys"]
        self.assertEqual(items, [{"path": "n.md", "missing": ["tags"]}])


if __name__ == "__main__":
    unittest.main()
