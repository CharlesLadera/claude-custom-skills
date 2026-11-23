import unittest

from helpers import make_vault

import vault as V


class TestParseLinks(unittest.TestCase):
    def test_wikilink_parts(self):
        ls = V.parse_links("see [[Note#Head|shown]] and ![[img.png]]")
        self.assertEqual([(l.path, l.heading, l.alias, l.embed) for l in ls],
                         [("Note", "#Head", "shown", False), ("img.png", None, None, True)])

    def test_table_pipe(self):
        (l,) = V.parse_links("| [[Note\\|alias]] | x |")
        self.assertEqual((l.path, l.alias, l.pipe), ("Note", "alias", "\\|"))

    def test_markdown_links(self):
        ls = V.parse_links("[a](My%20Note.md) [b](<Other File.md#Sec>) [c](https://x.y) [d](#local)")
        self.assertEqual([(l.path, l.heading, l.angle) for l in ls],
                         [("My Note.md", None, False), ("Other File.md", "#Sec", True)])

    def test_code_is_ignored(self):
        text = "`[[inline]]`\n```\n[[fenced]]\n```\n[[real]]"
        self.assertEqual([l.path for l in V.parse_links(text)], ["real"])
        self.assertEqual(V.parse_links(text)[0].line, 5)


class TestFrontmatter(unittest.TestCase):
    def test_lists_and_scalars(self):
        fm, body = V.split_frontmatter("---\ntags:\n  - a\n  - B\ncreated: 2026-01-02\nx: [p, q]\n---\nbody")
        d = V.parse_frontmatter(fm)
        self.assertEqual(d["tags"], ["a", "B"])
        self.assertEqual(d["created"], "2026-01-02")
        self.assertEqual(d["x"], ["p", "q"])
        self.assertEqual(V.tags_of(d), {"a", "b"})
        self.assertEqual(body, "body")

    def test_no_frontmatter(self):
        self.assertEqual(V.split_frontmatter("# hi\n---\n"), (None, "# hi\n---\n"))

    def test_inline_tags_skip_headings_and_code(self):
        self.assertEqual(V.inline_tags("# Title\nsome #tag/sub and `#code` and a#b"), {"tag/sub"})


class TestResolve(unittest.TestCase):
    def setUp(self):
        self.v = V.Vault(make_vault({
            "a/Note.md": "", "b/Note.md": "", "b/deep/Note.md": "", "c/unique.md": "",
            "z_Media/pic.png": "", "c/v1.2 notes.md": "",
        }, git=False))

    def test_basename_case_insensitive(self):
        self.assertEqual(self.v.resolve("UNIQUE", "x.md"), "c/unique.md")

    def test_same_folder_wins_then_shortest(self):
        self.assertEqual(self.v.resolve("Note", "b/deep/other.md"), "b/deep/Note.md")
        self.assertEqual(self.v.resolve("Note", "elsewhere.md"), "a/Note.md")

    def test_paths_and_extensions(self):
        self.assertEqual(self.v.resolve("b/Note", "x.md"), "b/Note.md")
        self.assertEqual(self.v.resolve("deep/Note", "x.md"), "b/deep/Note.md")
        self.assertEqual(self.v.resolve("pic.png", "x.md"), "z_Media/pic.png")
        self.assertEqual(self.v.resolve("v1.2 notes", "x.md"), "c/v1.2 notes.md")
        self.assertIsNone(self.v.resolve("missing", "x.md"))

    def test_relative_markdown_path(self):
        self.assertEqual(self.v.resolve("../c/unique.md", "a/Note.md"), "c/unique.md")

    def test_shortest_linkpath(self):
        self.assertEqual(self.v.shortest_linkpath("c/unique.md", "x.md"), "unique")
        self.assertEqual(self.v.shortest_linkpath("b/Note.md", "x.md"), "b/Note")
        self.assertEqual(self.v.shortest_linkpath("z_Media/pic.png", "x.md"), "pic.png")


class TestNamesAndGlobs(unittest.TestCase):
    def test_kebab(self):
        self.assertEqual(V.kebab("External SSD & Storage Management"), "external-ssd-and-storage-management")
        self.assertEqual(V.kebab("🗺️ Life MOC"), "life-moc")
        self.assertEqual(V.kebab("Reading List (MOC)"), "reading-list-moc")

    def test_globs(self):
        self.assertTrue(V.matches_any("00 - Inbox/x.md", ["00 - Inbox/**"]))
        self.assertTrue(V.matches_any("a/b/Reports/x.md", ["**/Reports/**"]))
        self.assertFalse(V.matches_any("a/x.md", ["*.md"]))
        self.assertTrue(V.matches_any("x.md", ["*.md"]))


if __name__ == "__main__":
    unittest.main()
