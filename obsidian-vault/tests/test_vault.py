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



class TestSuffixTieBreak(unittest.TestCase):
    def test_same_folder_wins_among_suffix_matches(self):
        v = V.Vault(make_vault({"x/sub/Note.md": "", "a/sub/Note.md": ""}, git=False))
        self.assertEqual(v.resolve("sub/Note", "x/sub/s.md"), "x/sub/Note.md")
        self.assertEqual(v.resolve("sub/Note", "elsewhere.md"), "a/sub/Note.md")

    def test_balanced_parens_in_markdown_url(self):
        (l,) = V.parse_links("[t](Folder%20(x)/n%20(1).md#h)")
        self.assertEqual((l.path, l.heading), ("Folder (x)/n (1).md", "#h"))


class TestRegateVaultGaps(unittest.TestCase):
    def test_inline_comments_stripped_unless_quoted(self):
        self.assertEqual(V.parse_frontmatter("status: draft # wip"), {"status": "draft"})
        self.assertEqual(V.parse_frontmatter('t: "a # b"'), {"t": "a # b"})

    def test_load_config_errors(self):
        import json
        import tempfile
        d = tempfile.mkdtemp()
        with self.assertRaises(SystemExit) as cm:
            V.load_config(d)
        self.assertIn("config.json missing", str(cm.exception))
        with open(d + "/config.json", "w") as f:
            json.dump({"vaultPath": d + "/nope"}, f)
        with self.assertRaises(SystemExit) as cm:
            V.load_config(d)
        self.assertIn("vaultPath does not exist", str(cm.exception))
        with open(d + "/config.json", "w") as f:
            json.dump({"vaultPath": d}, f)
        self.assertEqual(V.load_config(d)["vaultPath"], d)


class TestRound2Vault(unittest.TestCase):
    def test_link_anchors_are_not_tags(self):
        self.assertEqual(V.inline_tags("see [[#Setup]] and [x](#install) but #real\n"), {"real"})

    def test_bom_frontmatter(self):
        fm, body = V.split_frontmatter("﻿---\ntitle: x\n---\nbody")
        self.assertEqual((fm, body), ("title: x", "body"))


class TestVaultShouldFix(unittest.TestCase):
    def test_unterminated_or_malformed_frontmatter(self):
        self.assertEqual(V.split_frontmatter("---\na: 1\nbody"), (None, "---\na: 1\nbody"))
        self.assertEqual(V.split_frontmatter("---x\na: 1\n---\n"), (None, "---x\na: 1\n---\n"))

    def test_dot_slash_link(self):
        v = V.Vault(make_vault({"sub/x.md": ""}, git=False))
        self.assertEqual(v.resolve("./sub/x", "Home.md"), "sub/x.md")


class TestRound4Vault(unittest.TestCase):
    def test_parser_and_resolver_edges(self):
        fm = V.parse_frontmatter("# a comment\ntags: a, b\nalias: Foo")
        self.assertEqual(V.tags_of(fm), {"a", "b"})
        self.assertEqual(V._aliases(fm), ["Foo"])
        self.assertTrue(V.matches_any("a1.md", ["a?.md"]))
        self.assertFalse(V.matches_any("a12.md", ["a?.md"]))
        v = V.Vault(make_vault({"x/n.md": "", "y/n.md": ""}, git=False))
        self.assertEqual(v.resolve("", "x/n.md"), "x/n.md")
        self.assertEqual(v.shortest_linkpath("y/n.md", "z.md"), "y/n")


class TestRound6Vault(unittest.TestCase):
    def test_empty_values_with_comments(self):
        self.assertEqual(V.parse_frontmatter("tags: [] # todo\nx: \"\" # y\nz: [a] # c"),
                         {"tags": [], "x": "", "z": ["a"]})


class TestRound7Vault(unittest.TestCase):
    def test_quote_aware_comments(self):
        self.assertEqual(V.parse_frontmatter('aliases: ["C #1", x] # note')["aliases"], ["C #1", "x"])
        self.assertEqual(V.parse_frontmatter("title: 'it''s' # todo")["title"], "it's")
        self.assertEqual(V.parse_frontmatter('t: "a \\" b" # c')["t"], 'a " b')
        self.assertEqual(V.parse_frontmatter("u: x#y # c")["u"], "x#y")


class TestRound8Vault(unittest.TestCase):
    def test_values_and_quotes(self):
        P = V.parse_frontmatter
        # A leading # is a comment, as YAML and Obsidian read it.
        self.assertEqual(P("tags:\n  - #project\n  - b")["tags"], ["", "b"])
        self.assertEqual(P("title: #1 priority")["title"], None)
        self.assertEqual(P("tags: # fill in later")["tags"], None)
        self.assertEqual(P('tags: ["#a", "#b"]')["tags"], ["#a", "#b"])
        self.assertEqual(P("title: Hello, \"world # x\"")["title"], 'Hello, "world')
        self.assertEqual(P("t: \"a\" 'b # c")["t"], "\"a\" 'b")
        self.assertEqual(P('t: "\\UFFFFFFFF \\ud83d\\ude00 \\N"')["t"], "\\UFFFFFFFF \U0001F600 \x85")
        self.assertEqual(P("t: x \"y # z")["t"], 'x "y')
        self.assertEqual(P("t: Say 'hi # there")["t"], "Say 'hi")
        self.assertEqual(P('t: "line\\nbreak\\t\\u00e9"')["t"], "line\nbreak\té")
        self.assertEqual(P('aliases: ["a, b", c, \'d, e\']')["aliases"], ["a, b", "c", "d, e"])


class TestFinalVault(unittest.TestCase):
    def test_flow_close_and_raw_bytes(self):
        P = V.parse_frontmatter
        self.assertEqual(P('tags: [] "a # b"')["tags"], '[] "a')
        self.assertEqual(P('aliases: ["Caf\udce9"]')["aliases"], ["Caf\udce9"])
        self.assertEqual(V.has_value(["", None]), False)
        self.assertEqual(V.has_value(["x"]), True)



if __name__ == "__main__":
    unittest.main()
