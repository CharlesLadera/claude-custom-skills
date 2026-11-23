#!/usr/bin/env python3
"""The only writer for the obsidian-vault skill.

  apply.py preview <plan.json>                 what run would do; writes nothing
  apply.py run <plan.json> [--checkpoint]      apply the plan as one git commit
  apply.py checkpoint -m <message>             commit the vault's current state
  apply.py import <file>... --to <folder> [--checkpoint]
                                               copy files from outside into the vault

Plan file:
  {
    "description": "file quick note, archive old drafts",
    "moves": [{"from": "quick note.md", "to": "03 - Resources/Tools/quick-note.md"}],
    "edits": [{"path": "Home.md", "find": "[[old target]]", "replace": "[[new-target]]"}],
    "frontmatter": [{"path": "03 - Resources/Tools/quick-note.md",
                     "set": {"tags": ["tools"], "created": "2026-11-23"}}],
    "aliasOnRename": true
  }

Moves rewrite every link that pointed at a moved file (wikilinks, markdown links,
embeds, canvas file nodes) so it still points there afterwards. "edits" replace text
whose "find" occurs exactly once in the file (for retargeting a broken link). "edits" and
"frontmatter" paths are paths after the moves. Frontmatter is only ever added: a key that
already has a value is left alone and reported as skipped.

Safety, enforced by tests/test_safety.py:
  * nothing is ever deleted, and no file is overwritten by a move;
  * git runs only through git_run, limited to GIT_VERBS (no push, reset, checkout...);
  * run and import refuse a dirty vault unless --checkpoint is given, which first
    commits the current state so the change can be undone with `git revert`.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(SKILL, "lib"))
import vault as V  # noqa: E402

GIT_VERBS = ("rev-parse", "status", "add", "commit", "log")
COMMIT_PREFIX = "obsidian-vault: "


class PlanError(Exception):
    pass


# --- git -------------------------------------------------------------------------------

def git_run(root, *args):
    if not args or args[0] not in GIT_VERBS:
        raise PlanError("git verb not allowed: %r" % (args[:1],))
    p = subprocess.run(["git", "-C", root] + list(args), capture_output=True, text=True)
    if p.returncode != 0:
        raise PlanError("git %s failed: %s" % (args[0], p.stderr.strip() or p.stdout.strip()))
    return p.stdout


def git_check(root):
    top = git_run(root, "rev-parse", "--show-toplevel").strip()
    if os.path.realpath(top) != os.path.realpath(root):
        raise PlanError("vault root %s is not the top of its git repo (%s)" % (root, top))


# Files the OS or the Obsidian app rewrite on their own. They never make the vault "dirty"
# (it would never be clean otherwise), but the next commit still includes them.
NOISE = re.compile(r'(^|/)\.DS_Store"?$|^"?\.obsidian/workspace(-mobile)?\.json"?$')


def dirty(root):
    lines = [l for l in git_run(root, "status", "--porcelain").splitlines() if l.strip()]
    return [l for l in lines if not NOISE.search(l[3:])]


def commit(root, message):
    """Commit everything, noise included, but only when something besides noise changed."""
    git_run(root, "add", "-A")
    if not dirty(root):
        return None
    git_run(root, "commit", "-q", "-m", COMMIT_PREFIX + message)
    return git_run(root, "rev-parse", "--short", "HEAD").strip()


def staging_leftovers(root):
    """Hidden files an interrupted run left under its staging names."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        out += [os.path.relpath(os.path.join(dirpath, f), root).replace(os.sep, "/")
                for f in filenames if f.startswith(".obsidian-vault-")]
    return sorted(out)


def refuse_leftovers(root):
    left = staging_leftovers(root)
    if left:
        raise PlanError("an interrupted run left notes under staging names: %s. Rename each back "
                        "to its original name (`git status` lists it as deleted) before anything "
                        "else; never checkpoint over them." % ", ".join(left))


def ensure_clean(root, checkpoint, description):
    git_check(root)
    refuse_leftovers(root)
    d = dirty(root)
    if not d:
        return None
    if not checkpoint:
        raise PlanError("vault has %d uncommitted change(s). Show them to the user, then rerun "
                        "with --checkpoint to commit them first." % len(d))
    return commit(root, "checkpoint before " + description)


# --- frontmatter -----------------------------------------------------------------------

PLAIN = re.compile(r"^[A-Za-z0-9][\w\-./ ]*$")
NUMBER_LIKE = re.compile(r"^[-+]?(0x[0-9a-fA-F_]+|0o[0-7_]+|(\d[\d_]*(\.\d*)?|\.\d+)([eE][-+]?\d+)?)$")  # YAML would read a number


def yaml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if v is None or isinstance(v, (dict, list, tuple)):
        raise PlanError("frontmatter value must be text, a number, true/false or a list of those: %r" % (v,))
    s = str(v)
    if s == s.strip() and PLAIN.match(s) and not NUMBER_LIKE.match(s) and s.lower() not in ("yes", "no", "true", "false", "null", "on", "off"):
        return s
    return json.dumps(s, ensure_ascii=False)


def yaml_item(key, value):
    if isinstance(value, (list, tuple)):
        if not value:
            return ["%s: []" % key]
        return ["%s:" % key] + ["  - %s" % yaml_value(x) for x in value]
    return ["%s: %s" % (key, yaml_value(value))]


# A key line's value, or a list item, that holds no data at all.
EMPTY_TEXT = re.compile(r"""^\s*(|\[[\s,]*((""|'')[\s,]*)*\]|""|''|~|null|Null|NULL)\s*$""")
EMPTY_ITEM = re.compile(r"""^\s*-\s*(""|''|~|null|Null|NULL)?\s*$""")


def add_frontmatter(text, items):
    """Return (new_text, added_keys, skipped_keys). Never changes an existing value.
    Only the frontmatter lines change: new lines use the frontmatter's own line ending
    (CRLF or LF), the body keeps its bytes, and a leading byte-order mark stays first."""
    if text.startswith("\ufeff"):
        new, added, skipped = add_frontmatter(text[1:], items)
        return "\ufeff" + new, added, skipped
    fm, body = V.split_frontmatter(text)
    if fm is not None:
        nl = "\r\n" if text.split("\n", 1)[0].endswith("\r") else "\n"
        fm_lines = [l[:-1] if l.endswith("\r") else l for l in fm.split("\n")]
    else:
        nl = "\r\n" if "\r\n" in text else "\n"
        fm_lines = []
    # Each top-level key with the indented or "-" lines under it (blank and comment lines in
    # between don't end it, as in YAML). A key is replaced only when every copy of it is
    # trivially empty as text (see EMPTY_TEXT); any other text, a comment included, means the
    # key is skipped, so nothing the user wrote is ever rewritten or deleted.
    blocks, cur = [], None
    for i, l in enumerate(fm_lines):
        m = re.match(r"^([^\s:#-][^:]*):", l)
        if m:
            k = m.group(1).strip()
            if len(k) > 1 and k[0] == k[-1] and k[0] in "\"'":
                k = k[1:-1]
            cur = (k, [i])
            blocks.append(cur)
        elif cur is not None and l.strip() and (l[:1] in (" ", "\t") or l.lstrip().startswith("-")):
            cur[1].append(i)
        elif l.strip() and not l.lstrip().startswith("#"):
            cur = None

    def filled(idx):
        first = fm_lines[idx[0]].split(":", 1)[1]
        return not (EMPTY_TEXT.match(first) and all(EMPTY_ITEM.match(fm_lines[i]) for i in idx[1:]))

    # A second veto: the key is also filled when the parser, reading the whole frontmatter,
    # gives it a value (its items can follow lines the block scan doesn't attach).
    existing = V.parse_frontmatter("\n".join(fm_lines)) if fm is not None else {}
    added, skipped, new_lines, drop = [], [], [], set()
    for k, v in items.items():
        mine = [idx for key, idx in blocks if key == k]
        if V.has_value(existing.get(k)) or any(filled(idx) for idx in mine):
            skipped.append(k)
            continue
        for idx in mine:
            drop.update(idx)  # every copy is empty: replace its own lines, nothing else
        added.append(k)
        new_lines += yaml_item(k, v)
    if not added:
        return text, added, skipped
    if fm is None:
        return "---" + nl + nl.join(new_lines) + nl + "---" + nl + text, added, skipped
    kept = [l for i, l in enumerate(fm_lines) if i not in drop]
    kept = [l for l in kept if l.strip()] if not "".join(kept).strip() else kept
    block = nl.join(kept + new_lines)
    head = text[:len(text) - len(body)] if body else text
    close = "..." if head.rstrip("\r\n").endswith("...") else "---"  # keep YAML's other end marker
    return "---" + nl + block + nl + close + nl + body, added, skipped


# --- moves -----------------------------------------------------------------------------

DELIM_ROW = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$|^\s*\|\s*:?-+:?\s*\|\s*$")


def _in_table(text, offset):
    """True when the link sits in a table row, with or without the leading pipe: the line
    belongs to a run of pipe-bearing lines that holds a delimiter row (--- | ---)."""
    lines = text.split("\n")
    i = text.count("\n", 0, offset)
    if lines[i].lstrip().startswith("|"):
        return True
    piped = lambda ln: "|" in V.WIKILINK.sub("", ln)
    if not piped(lines[i]):
        return False
    lo = i
    while lo > 0 and piped(lines[lo - 1]):
        lo -= 1
    hi = i
    while hi + 1 < len(lines) and piped(lines[hi + 1]):
        hi += 1
    return any(DELIM_ROW.match(lines[k]) for k in range(lo, hi + 1))


def _md_url(path, angle):
    # Parentheses are encoded too: an unbalanced one would end the link early.
    return "<%s>" % path if angle else quote(path, safe="/&'!,;=+@-._~")


def inside(root, rel, what):
    """Absolute path for REL, refused if it resolves (symlinks included) outside the vault."""
    real_root = os.path.realpath(root)
    real = os.path.realpath(os.path.join(root, rel))
    if not real.startswith(real_root + os.sep):
        raise PlanError("%s resolves outside the vault: %s" % (what, rel))
    # A symlink can also point at the vault's own .git or .obsidian; refuse hidden targets too.
    if any(p.startswith(".") for p in os.path.relpath(real, real_root).split(os.sep)):
        raise PlanError("%s resolves into a hidden folder: %s" % (what, rel))
    if os.path.islink(os.path.join(root, rel)):
        raise PlanError("%s is a symlink; edit or move its target instead: %s" % (what, rel))
    return os.path.join(root, rel)


def safe_target(root, rel, what):
    """A path the plan wants to create: plain, portable, no hidden or parent segments."""
    if not rel or os.path.isabs(rel) or "\\" in rel or ":" in rel or os.path.splitdrive(rel)[0]:
        raise PlanError("%s must be a plain vault-relative path: %r" % (what, rel))
    if any(p in ("", ".", "..") or p.startswith(".") for p in rel.split("/")):
        raise PlanError("%s may not contain empty, '..' or hidden segments: %r" % (what, rel))
    return inside(root, rel, what)


def validate_moves(vault, moves):
    seen = set()
    for src, dst in moves.items():
        if src not in vault.files:
            raise PlanError("move source not in vault: %s" % src)
        inside(vault.root, src, "move source")
        safe_target(vault.root, dst, "move target")
        if dst.lower() in seen:
            raise PlanError("two moves target the same path: %s" % dst)
        seen.add(dst.lower())
        other = vault.lower.get(dst.lower())
        if other and other != src and other not in moves:
            raise PlanError("move target already exists: %s" % dst)
        if os.path.splitext(src)[1].lower() != os.path.splitext(dst)[1].lower():
            raise PlanError("move must keep the file extension: %s -> %s" % (src, dst))


def rewrite_text(vault, after, moves, old_path, text, alias_on_rename=True):
    """Rewrite links in one note so each still reaches the file it reached before."""
    new_path = moves.get(old_path, old_path)
    out, pos, changes = [], 0, []
    for l in V.parse_links(text):
        target = vault.resolve(l.path, old_path)
        if target is None or (target == old_path and l.path == ""):
            continue
        want = moves.get(target, target)
        if after.resolve(l.path, new_path) == want:
            continue
        if l.kind == "wiki":
            lp = after.shortest_linkpath(want, new_path)
            alias, pipe = l.alias, l.pipe
            if (alias is None and alias_on_rename and not l.embed
                    and V.link_name(target) != V.link_name(want)):
                # Keep what the reader saw: Obsidian shows [[Old#Sec]] as "Old > Sec".
                alias = l.path.rsplit("/", 1)[-1]
                if alias.lower().endswith(V.NOTE_EXT):
                    alias = alias[:-len(V.NOTE_EXT)]
                if l.heading:
                    alias += l.heading.replace("#", " > ")
                pipe = "\\|" if _in_table(text, l.start) else "|"
            new = "%s[[%s%s%s]]" % ("!" if l.embed else "", lp, l.heading or "",
                                    (pipe + alias) if alias is not None else "")
        else:
            # Vault-absolute only when the link was written that way from a subfolder; from the
            # vault root the two readings coincide and relative is the portable choice.
            absolute = bool(V.folder(old_path)) and vault.lower.get(l.path.lower()) == target
            dest = want if absolute else os.path.relpath(want, V.folder(new_path) or ".").replace(os.sep, "/")
            if not l.path.lower().endswith(V.NOTE_EXT) and dest.lower().endswith(V.NOTE_EXT):
                dest = dest[:-len(V.NOTE_EXT)]
            if after.resolve(dest, new_path) != want:
                # A relative path can coincide with a different vault-absolute file; Obsidian
                # tries the absolute reading first, so write the absolute form instead.
                dest = want if l.path.lower().endswith(V.NOTE_EXT) or not want.lower().endswith(V.NOTE_EXT) \
                    else want[:-len(V.NOTE_EXT)]
            if l.angle:
                url = "<%s%s>" % (dest, l.heading or "")
            else:
                url = _md_url(dest, False) + (l.heading or "")
            new = "%s[%s](%s%s)" % ("!" if l.embed else "", l.text, url, l.title or "")
        out.append(text[pos:l.start])
        out.append(new)
        pos = l.end
        changes.append({"line": l.line, "before": l.raw, "after": new})
    if not changes:
        return text, []
    out.append(text[pos:])
    return "".join(out), changes


def rewrite_canvas(vault, after, moves, old, text, alias_on_rename=True):
    """File nodes that point at a moved file, and links inside text cards. A canvas that
    isn't the expected JSON shape is left alone."""
    try:
        data = json.loads(text)
    except ValueError:
        return text, []
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
        return text, []
    changes = []
    for node in data["nodes"]:
        if not isinstance(node, dict):
            continue
        for field in ("file", "background"):  # file cards, and a group's background image
            f = node.get(field)
            if isinstance(f, str) and f in moves:
                changes.append({"before": f, "after": moves[f]})
                node[field] = moves[f]
        t = node.get("text")
        if node.get("type") == "text" and isinstance(t, str):
            new_t, ch = rewrite_text(vault, after, moves, old, t, alias_on_rename)
            if ch:
                node["text"] = new_t
                changes += ch
    if not changes:
        return text, []
    return json.dumps(data, indent="\t", ensure_ascii=False), changes


def compute(vault, plan):
    """Everything a run would change, computed in memory. Nothing touches disk."""
    moves = {m["from"]: m["to"] for m in plan.get("moves", [])}
    validate_moves(vault, moves)
    after = vault.with_moves(moves)
    alias = plan.get("aliasOnRename", True)
    edits, report = {}, []
    for old in vault.files:
        low = old.lower()
        if not (low.endswith(V.NOTE_EXT) or low.endswith(".canvas")):
            continue
        text = vault.read(old)
        if low.endswith(".canvas"):
            new_text, ch = rewrite_canvas(vault, after, moves, old, text, alias)
        else:
            new_text, ch = rewrite_text(vault, after, moves, old, text, alias)
        if ch:
            new = moves.get(old, old)
            edits[new] = new_text
            report.append({"file": new, "changes": ch})
    inverse = {v: k for k, v in moves.items()}

    def current(p):
        if p not in after.files:
            raise PlanError("path not in vault after moves: %s" % p)
        src = inverse.get(p, p)
        inside(vault.root, src, "edited file")  # refuses a symlink that points out of the vault
        return edits.get(p, vault.read(src))

    # Text edits (e.g. retargeting a broken link): each "find" must occur exactly once.
    text_report = []
    for item in plan.get("edits", []):
        p, find, repl = item["path"], item["find"], item["replace"]
        text = current(p)
        n = text.count(find)
        if n != 1:
            raise PlanError("edit in %s: %r occurs %d times, must be exactly once" % (p, find, n))
        edits[p] = text.replace(find, repl, 1)
        text_report.append({"path": p, "before": find, "after": repl})

    fm_report = []
    for item in plan.get("frontmatter", []):
        p = item["path"]
        new_text, added, skipped = add_frontmatter(current(p), item["set"])
        if added:
            edits[p] = new_text
        fm_report.append({"path": p, "added": added, "skipped": skipped})

    # Every file the run will write must be a plain file inside the vault, checked now so a
    # refusal comes before the first move rather than halfway through.
    for p in edits:
        inside(vault.root, inverse.get(p, p), "edited file")

    emptied = sorted({V.folder(s) for s in moves if V.folder(s)
                      and not any(f.startswith(V.folder(s) + "/") for f in after.files)})
    return moves, edits, {"moves": [{"from": k, "to": v} for k, v in moves.items()],
                          "link_rewrites": report, "edits": text_report, "frontmatter": fm_report,
                          "emptied_folders": emptied,
                          "files_changed": len(set(edits) | set(moves.values()))}


def _temp_name(root, rel):
    """An unused hidden name next to REL for staging a move."""
    folder_abs, base = os.path.split(os.path.join(root, rel))
    # Keep the staging name within the 255-byte file-name limit for long note names.
    while len(base.encode("utf-8")) > 200:
        base = base[:-1]
    for n in range(1000):
        t = os.path.join(folder_abs, ".obsidian-vault-%d-%s" % (n, base))
        if not os.path.lexists(t):
            return t
    raise PlanError("no free temp name next to %s" % rel)


def execute(vault, plan, moves, edits):
    """Apply moves and writes. All or nothing: on any failure, completed renames are undone
    and rewritten files get their original text back before the error is raised.

    Moves go in two phases (every source to a temp name, then every temp to its target), so
    chains (a->b, b->c), swaps and case-only renames work whatever their order. Text is
    written exactly as computed: line endings and undecodable bytes are preserved."""
    root = vault.root
    inverse = {v: k for k, v in moves.items()}
    originals = {rel: vault.read(inverse.get(rel, rel)) for rel in edits}
    where = {}     # source path -> where that file is right now (a temp name, then its target)
    written = []   # notes whose new text is on disk
    created = []   # folders this run made (never removed: nothing here deletes)
    try:
        for src, dst in moves.items():
            inside(root, src, "move source")
            safe_target(root, dst, "move target")
        for src in moves:
            t = _temp_name(root, src)
            os.rename(os.path.join(root, src), t)
            where[src] = t
        for src, dst in moves.items():
            d = safe_target(root, dst, "move target")
            parent = os.path.dirname(d)
            while not os.path.isdir(parent) and parent != root:
                created.append(os.path.relpath(parent, root).replace(os.sep, "/"))
                parent = os.path.dirname(parent)
            os.makedirs(os.path.dirname(d), exist_ok=True)
            inside(root, dst, "move target")  # re-check now that the parent folders exist
            if os.path.lexists(d):
                raise PlanError("move target appeared on disk: %s" % dst)
            os.rename(where[src], d)
            where[src] = d
        for rel, text in edits.items():
            target = inside(root, rel, "edited file")
            f = open(target, "w", encoding="utf-8", errors="surrogateescape", newline="")
            written.append(rel)  # truncated from here on, so it needs restoring if the write fails
            with f:
                f.write(text)
    except BaseException as e:  # Ctrl-C too: an interrupted run must not leave staging names
        problems = []
        for rel in reversed(written):
            try:
                with open(os.path.join(root, rel), "w", encoding="utf-8", errors="surrogateescape",
                          newline="") as f:
                    f.write(originals[rel])
            except OSError as err:
                problems.append("restore %s: %s" % (rel, err))
        # Undo moves in two phases too (everything to a temp name, then back), so swaps and
        # chains go back whatever order they were listed in.
        parked = {}
        for src, now in where.items():
            try:
                t = _temp_name(root, src)
                os.rename(now, t)
                parked[src] = t
            except OSError as err:
                problems.append("undo move %s: %s; it is at %s" % (src, err, os.path.relpath(now, root)))
        for src, t in parked.items():
            back = os.path.join(root, src)
            now = t
            try:
                if not os.path.lexists(back):
                    os.rename(t, back)
                    continue
                # Something new sits at the original path: never overwrite it. Put the file at
                # its target when that is free, else leave it under the temp name, and say where.
                d = os.path.join(root, moves[src])
                if not os.path.lexists(d):
                    os.rename(t, d)
                    now = d
                problems.append("%s could not go back (a new file is there); it is at %s"
                                % (src, os.path.relpath(now, root)))
            except OSError as err:
                problems.append("undo move %s: %s; it is at %s" % (src, err, os.path.relpath(now, root)))
        if problems:
            raise PlanError("run failed (%s) and the rollback was incomplete: %s. Check `git status` "
                            "in the vault before anything else." % (e, "; ".join(problems)))
        if not isinstance(e, Exception):
            raise  # interrupted, and fully rolled back: let the interrupt through
        if created:
            raise PlanError("run failed; no file was changed, but it left these new empty folders: "
                            "%s: %s" % (", ".join(sorted(set(created))), e))
        raise PlanError("run failed and nothing was changed: %s" % e)


# --- import ----------------------------------------------------------------------------

def do_import(vault, files, to_folder):
    to_folder = to_folder.rstrip("/")
    if to_folder:
        safe_target(vault.root, to_folder, "--to")
    done, claimed = [], set()
    for f in files:
        src = os.path.abspath(os.path.expanduser(f))
        if not os.path.isfile(src):
            raise PlanError("not a file: %s" % f)
        if os.path.realpath(src).startswith(os.path.realpath(vault.root) + os.sep):
            raise PlanError("already inside the vault, use a move: %s" % f)
        rel = (to_folder + "/" if to_folder else "") + os.path.basename(src)
        safe_target(vault.root, rel, "import target")
        if vault.lower.get(rel.lower()) or os.path.lexists(os.path.join(vault.root, rel)):
            raise PlanError("import target already exists: %s" % rel)
        if rel.lower() in claimed:
            raise PlanError("two imported files would both land at %s" % rel)
        claimed.add(rel.lower())
        done.append((src, rel))
    copied = []
    try:
        for src, rel in done:
            dst = safe_target(vault.root, rel, "import target")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            inside(vault.root, rel, "import target")
            if os.path.lexists(dst):
                raise PlanError("import target appeared on disk: %s" % rel)
            shutil.copy2(src, dst)
            copied.append(rel)
    except (OSError, PlanError) as e:
        if not copied:
            raise PlanError(str(e))
        raise PlanError("import stopped (%s). Already copied and not committed: %s. Show the user before any "
                        "checkpoint; nothing is removed automatically." % (e, ", ".join(copied)))
    return copied


# --- cli -------------------------------------------------------------------------------

def load_plan(path):
    try:
        with open(path, encoding="utf-8") as f:
            plan = json.load(f)
    except (OSError, ValueError) as e:
        raise PlanError("cannot read plan %s: %s" % (path, e))
    if not isinstance(plan, dict) or not plan.get("description"):
        raise PlanError("plan needs a description (it becomes the commit message)")
    shapes = {"moves": {"from": str, "to": str}, "edits": {"path": str, "find": str, "replace": str},
              "frontmatter": {"path": str, "set": dict}}
    for key, fields in shapes.items():
        entries = plan.get(key, [])
        if not isinstance(entries, list):
            raise PlanError("plan %r must be a list" % key)
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise PlanError("plan %s[%d] must be an object" % (key, i))
            for f, kind in fields.items():
                if not isinstance(entry.get(f), kind):
                    raise PlanError("plan %s[%d] needs %r (%s)" % (key, i, f, kind.__name__))
    sources = [m["from"] for m in plan.get("moves", [])]
    if len(set(sources)) != len(sources):
        raise PlanError("plan moves the same file twice")
    if not isinstance(plan.get("aliasOnRename", True), bool):
        raise PlanError("plan aliasOnRename must be true or false")
    return plan


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vault")
    sub = ap.add_subparsers(dest="cmd", required=True)
    # --vault works before or after the subcommand
    vault_opt = argparse.ArgumentParser(add_help=False)
    vault_opt.add_argument("--vault", default=argparse.SUPPRESS)
    s = sub.add_parser("preview", parents=[vault_opt])
    s.add_argument("plan")
    s = sub.add_parser("run", parents=[vault_opt])
    s.add_argument("plan")
    s.add_argument("--checkpoint", action="store_true")
    s = sub.add_parser("checkpoint", parents=[vault_opt])
    s.add_argument("-m", "--message", required=True)
    s = sub.add_parser("import", parents=[vault_opt])
    s.add_argument("files", nargs="+")
    s.add_argument("--to", required=True)
    s.add_argument("--checkpoint", action="store_true")
    args = ap.parse_args(argv)

    root = os.path.expanduser(args.vault or V.load_config(SKILL)["vaultPath"])
    if not os.path.isdir(root):
        json.dump({"error": "vault path is not a folder: %s" % root}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 2
    try:
        if args.cmd == "checkpoint":
            git_check(root)
            refuse_leftovers(root)
            res = {"commit": commit(root, args.message)}
        elif args.cmd == "import":
            cp = ensure_clean(root, args.checkpoint, "import")
            added = do_import(V.Vault(root), args.files, args.to)
            res = {"checkpoint": cp, "imported": added,
                   "commit": commit(root, "import %d file(s) into %s" % (len(added), args.to))}
        else:
            plan = load_plan(args.plan)
            vault = V.Vault(root)
            if args.cmd == "preview":
                git_check(root)
                _, _, report = compute(vault, plan)
                report["uncommitted_changes"] = len(dirty(root))
                res = report
            else:
                compute(vault, plan)  # refuse a bad plan before any checkpoint commit
                cp = ensure_clean(root, args.checkpoint, plan["description"])
                vault = V.Vault(root)
                moves, edits, report = compute(vault, plan)
                execute(vault, plan, moves, edits)
                report["checkpoint"] = cp
                try:
                    report["commit"] = commit(root, plan["description"])
                except PlanError as e:
                    json.dump({"error": "changes applied but not committed: %s. Fix git, then run "
                                        "`apply.py checkpoint -m %r`; don't rerun the plan." % (e, plan["description"]),
                               "report": report}, sys.stdout, indent=2, ensure_ascii=False)
                    sys.stdout.write("\n")
                    return 2
                report["undo"] = "git -C %r revert %s" % (root, report["commit"]) if report["commit"] else None
                res = report
    except (PlanError, OSError) as e:
        msg = str(e) if isinstance(e, PlanError) else "%s; check `git status` in the vault" % e
        json.dump({"error": msg}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 2
    json.dump(res, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
