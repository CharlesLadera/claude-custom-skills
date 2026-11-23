"""Read-only model of an Obsidian vault: files, frontmatter, links, link resolution.

Nothing in this module writes, moves, deletes or spawns a process. tests/test_safety.py
enforces that. Writes live in apply/apply.py only.

Link resolution follows Obsidian's own rules closely enough for lint and for rewriting
links when a file moves:
  * matching is case-insensitive;
  * a link path is tried as written and with ".md" appended;
  * an exact vault-relative path wins, then a path relative to the linking note's folder,
    then any file whose path ends with the link path on a folder boundary;
  * among several suffix matches, one in the linking note's folder wins, then the
    shortest path, then alphabetical order.
"""
import json
import os
import re
import unicodedata
from datetime import date, datetime
from urllib.parse import unquote

SKIP_DIRS = {".obsidian", ".trash", ".git", ".debris", ".DS_Store"}
NOTE_EXT = ".md"

DEFAULT_RULES = {
    "naming": {"pattern": r"^[a-z0-9][a-z0-9.\-]*$", "exempt": []},
    "frontmatter": {"required": ["tags", "created"], "exempt": []},
    "orphans": {"exempt": []},
    "duplicates": {"exempt": []},
    "lintIgnore": [],
    "inbox": "00 - Inbox",
    "archive": "04 - Archive",
    "templates": "_templates",
}

RULES_FENCE = re.compile(r"```json obsidian-vault-rules\s*\n(.*?)\n```", re.S)

# --- link syntax -----------------------------------------------------------------------

# [[target#heading|alias]] and ![[...]]. Inside a table the pipe is written "\|".
WIKILINK = re.compile(r"(!?)\[\[([^\[\]\n]*?)\]\]")
# [text](url) and [text](<url with spaces>), optional "title".
MDLINK = re.compile(r'(!?)\[([^\]\n]*)\]\((<[^>\n]+>|[^)\s]+)((?:\s+"[^"\n]*")?)\)')
SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")
FENCE = re.compile(r"^\s*(```|~~~)")
INLINE_CODE = re.compile(r"`[^`\n]*`")
INLINE_TAG = re.compile(r"(?<![\w/&#])#([A-Za-z0-9_/\-]*[A-Za-z_/\-][A-Za-z0-9_/\-]*)")


class Link(object):
    """One link occurrence. start/end are character offsets into the file text."""

    __slots__ = ("kind", "embed", "path", "heading", "alias", "line", "start", "end",
                 "raw", "pipe", "angle", "title", "text")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def as_dict(self):
        return {"kind": self.kind, "raw": self.raw, "path": self.path, "line": self.line}


def _code_mask(text):
    """Return a copy of text with fenced and inline code blanked out (same length)."""
    out = []
    in_fence = None
    for line in text.splitlines(True):
        m = FENCE.match(line)
        if in_fence:
            out.append(re.sub(r"[^\n]", " ", line))
            if m and m.group(1) == in_fence:
                in_fence = None
            continue
        if m:
            in_fence = m.group(1)
            out.append(re.sub(r"[^\n]", " ", line))
            continue
        out.append(INLINE_CODE.sub(lambda mm: " " * len(mm.group(0)), line))
    return "".join(out)


def _line_of(text, offset):
    return text.count("\n", 0, offset) + 1


def parse_links(text):
    """All wikilinks and internal markdown links outside code, in order."""
    masked = _code_mask(text)
    links = []
    for m in WIKILINK.finditer(masked):
        inner = text[m.start(2):m.end(2)]
        pipe = None
        alias = None
        for sep in ("\\|", "|"):
            if sep in inner:
                inner, alias = inner.split(sep, 1)
                pipe = sep
                break
        heading = None
        if "#" in inner:
            inner, heading = inner.split("#", 1)
            heading = "#" + heading
        links.append(Link(kind="wiki", embed=bool(m.group(1)), path=inner.strip(),
                          heading=heading, alias=alias, pipe=pipe, line=_line_of(text, m.start()),
                          start=m.start(), end=m.end(), raw=text[m.start():m.end()]))
    for m in MDLINK.finditer(masked):
        url = text[m.start(3):m.end(3)]
        angle = url.startswith("<")
        if angle:
            url = url[1:-1]
        if SCHEME.match(url) or url.startswith("#"):
            continue
        heading = None  # kept exactly as written (still percent-encoded when it was)
        if "#" in url:
            url, heading = url.split("#", 1)
            heading = "#" + heading
        links.append(Link(kind="md", embed=bool(m.group(1)), path=unquote(url), heading=heading,
                          angle=angle, title=text[m.start(4):m.end(4)], text=text[m.start(2):m.end(2)],
                          line=_line_of(text, m.start()), start=m.start(), end=m.end(),
                          raw=text[m.start():m.end()]))
    links.sort(key=lambda l: l.start)
    return links


def inline_tags(text):
    body = split_frontmatter(text)[1]
    masked = _code_mask(body)
    tags = set()
    for line in masked.splitlines():
        if re.match(r"^\s{0,3}#{1,6}\s", line):  # heading marker, not a tag
            line = re.sub(r"^\s{0,3}#{1,6}\s", "", line)
        for m in INLINE_TAG.finditer(line):
            tags.add(m.group(1).lower())
    return tags


# --- frontmatter -----------------------------------------------------------------------

def split_frontmatter(text):
    """(frontmatter_text_or_None, body). Frontmatter excludes the --- lines."""
    if not text.startswith("---"):
        return None, text
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return None, text
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
    return None, text


def _scalar(v):
    v = v.strip()
    if " #" in v and not v.startswith(("'", '"')):
        v = v.split(" #", 1)[0].strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    return v


def parse_frontmatter(fm):
    """Minimal YAML subset: top-level keys, inline scalars, [a, b] lists, '- item' lists."""
    data = {}
    if fm is None:
        return data
    key = None
    for line in fm.split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^([^\s:#][^:]*):(.*)$", line)
        if m and not line.startswith((" ", "\t", "-")):
            key = m.group(1).strip()
            val = m.group(2).strip()
            if val.startswith("[") and val.endswith("]"):
                data[key] = [_scalar(x) for x in val[1:-1].split(",") if x.strip()]
            elif val == "":
                data[key] = None
            else:
                data[key] = _scalar(val)
            continue
        m = re.match(r"^\s*-\s*(.*)$", line)
        if m and key is not None:
            if not isinstance(data.get(key), list):
                data[key] = []
            data[key].append(_scalar(m.group(1)))
    return data


def tags_of(data):
    t = data.get("tags")
    if t is None:
        return set()
    if isinstance(t, str):
        t = [x for x in re.split(r"[,\s]+", t) if x]
    return {x.lstrip("#").lower() for x in t if x}


# --- globs and rules -------------------------------------------------------------------

def glob_to_regex(pattern):
    """'**' matches any number of folders, '*' anything but '/', '?' one char."""
    i, out = 0, []
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def matches_any(path, patterns):
    return any(glob_to_regex(p).match(path) for p in patterns)


def load_rules(vault_root):
    """Rules come from the vault's CLAUDE.md, fenced as ```json obsidian-vault-rules."""
    rules = json.loads(json.dumps(DEFAULT_RULES))
    path = os.path.join(vault_root, "CLAUDE.md")
    source = "defaults (no obsidian-vault-rules block in CLAUDE.md)"
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            m = RULES_FENCE.search(f.read())
        if m:
            loaded = json.loads(m.group(1))
            for k, v in loaded.items():
                if isinstance(v, dict) and isinstance(rules.get(k), dict):
                    rules[k].update(v)
                else:
                    rules[k] = v
            source = "CLAUDE.md"
    rules["_source"] = source
    return rules


# --- names -----------------------------------------------------------------------------

def kebab(name):
    """'External SSD & Storage Management' -> 'external-ssd-and-storage-management'."""
    s = unicodedata.normalize("NFKD", name)
    s = s.replace("&", " and ").replace("@", " at ")
    s = "".join(c for c in s if c.isalnum() or c in " -_.")
    s = s.encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[\s_]+", "-", s.strip())
    s = re.sub(r"-{2,}", "-", s).strip("-.")
    return s or "untitled"


def stem(path):
    base = path.rsplit("/", 1)[-1]
    return base[:-len(NOTE_EXT)] if base.lower().endswith(NOTE_EXT) else base


def folder(path):
    return path.rsplit("/", 1)[0] if "/" in path else ""


def link_name(path):
    """What a link to PATH shows by default: stem for notes, full name for other files."""
    base = path.rsplit("/", 1)[-1]
    return stem(path) if base.lower().endswith(NOTE_EXT) else base


# --- the vault -------------------------------------------------------------------------

class Vault(object):
    def __init__(self, root, files=None):
        self.root = os.path.abspath(root)
        self.files = sorted(files) if files is not None else self._walk()
        self._index()

    def _walk(self):
        out = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
            rel_dir = os.path.relpath(dirpath, self.root)
            for fn in filenames:
                if fn.startswith("."):
                    continue
                rel = fn if rel_dir == "." else rel_dir.replace(os.sep, "/") + "/" + fn
                out.append(rel)
        return sorted(out)

    def _index(self):
        self.lower = {}
        for p in self.files:
            self.lower.setdefault(p.lower(), p)

    def with_moves(self, moves):
        """A Vault view with MOVES (old -> new) applied to the file list. Reads stay on disk."""
        moved = [moves.get(p, p) for p in self.files]
        v = Vault(self.root, moved)
        return v

    @property
    def notes(self):
        return [p for p in self.files if p.lower().endswith(NOTE_EXT)]

    def read(self, rel):
        with open(os.path.join(self.root, rel), encoding="utf-8", errors="replace") as f:
            return f.read()

    def resolve(self, linkpath, source):
        """Vault path a link resolves to from SOURCE, or None. Mirrors Obsidian's rules."""
        lp = linkpath.strip().replace("\\", "/")
        if lp == "":
            return source  # [[#heading]] points at the note itself
        while lp.startswith("./"):
            lp = lp[2:]
        lp = lp.lstrip("/")
        cands = [lp] if lp.lower().endswith(NOTE_EXT) else [lp, lp + NOTE_EXT]
        src_dir = folder(source)
        for c in cands:
            hit = self.lower.get(c.lower())
            if hit:
                return hit
        for c in cands:
            rel = os.path.normpath(os.path.join(src_dir, c)).replace(os.sep, "/")
            if not rel.startswith(".."):
                hit = self.lower.get(rel.lower())
                if hit:
                    return hit
        best = None
        for c in cands:
            suffix = "/" + c.lower()
            for p in self.files:
                pl = p.lower()
                if pl.endswith(suffix):
                    key = (0 if folder(p) == src_dir else 1, p.count("/"), len(p), p)
                    if best is None or key < best[0]:
                        best = (key, p)
        return best[1] if best else None

    def shortest_linkpath(self, target, source):
        """Shortest link text that resolves to TARGET from SOURCE ('.md' dropped for notes)."""
        name = link_name(target)
        full = target[:-len(NOTE_EXT)] if target.lower().endswith(NOTE_EXT) else target
        parts = full.split("/")
        for i in range(len(parts) - 1, -1, -1):
            cand = "/".join(parts[i:])
            if i == len(parts) - 1:
                cand = name if target.lower().endswith(NOTE_EXT) else parts[-1]
            if self.resolve(cand, source) == target:
                return cand
        return full


def note_record(vault, rel):
    text = vault.read(rel)
    fm, body = split_frontmatter(text)
    data = parse_frontmatter(fm)
    return {
        "path": rel,
        "text": text,
        "has_frontmatter": fm is not None,
        "frontmatter": data,
        "body": body,
        "tags": tags_of(data) | inline_tags(text),
        "aliases": _aliases(data),
        "links": parse_links(text),
    }


def _aliases(data):
    a = data.get("aliases") or data.get("alias")
    if a is None:
        return []
    return [a] if isinstance(a, str) else list(a)


def file_created(vault, rel):
    st = os.stat(os.path.join(vault.root, rel))
    ts = getattr(st, "st_birthtime", None) or st.st_mtime
    return date.fromtimestamp(ts).isoformat()


def file_modified(vault, rel):
    return datetime.fromtimestamp(os.stat(os.path.join(vault.root, rel)).st_mtime).date().isoformat()


def load_config(skill_root):
    path = os.path.join(skill_root, "config.json")
    if not os.path.isfile(path):
        raise SystemExit("config.json missing: copy config.example.json to config.json and set vaultPath")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    root = os.path.expanduser(cfg.get("vaultPath", ""))
    if not os.path.isdir(root):
        raise SystemExit("vaultPath does not exist: %s" % root)
    cfg["vaultPath"] = root
    return cfg
