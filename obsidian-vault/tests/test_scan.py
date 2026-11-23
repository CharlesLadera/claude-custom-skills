import argparse
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


if __name__ == "__main__":
    unittest.main()
