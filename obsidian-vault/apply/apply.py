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


def ensure_clean(root, checkpoint, description):
    git_check(root)
    d = dirty(root)
    if not d:
        return None
    if not checkpoint:
        raise PlanError("vault has %d uncommitted change(s). Show them to the user, then rerun "
                        "with --checkpoint to commit them first." % len(d))
    return commit(root, "checkpoint before " + description)


# --- frontmatter -----------------------------------------------------------------------

PLAIN = re.compile(r"^[A-Za-z0-9][\w\-./ ]*$")


def yaml_value(v):
    s = str(v)
    if PLAIN.match(s) and s.lower() not in ("yes", "no", "true", "false", "null", "on", "off"):
        return s
    return json.dumps(s, ensure_ascii=False)


def yaml_item(key, value):
    if isinstance(value, (list, tuple)):
        if not value:
            return ["%s: []" % key]
        return ["%s:" % key] + ["  - %s" % yaml_value(x) for x in value]
    return ["%s: %s" % (key, yaml_value(value))]


def add_frontmatter(text, items):
    """Return (new_text, added_keys, skipped_keys). Never changes an existing value."""
    fm, body = V.split_frontmatter(text)
    existing = V.parse_frontmatter(fm)
    added, skipped, new_lines = [], [], []
    empty_present = set()
    for k, v in items.items():
        if k in existing and existing[k] not in (None, "", []):
            skipped.append(k)
            continue
        if k in existing:
            empty_present.add(k)
        added.append(k)
        new_lines += yaml_item(k, v)
    if not added:
        return text, added, skipped
    if fm is None:
        return "---\n" + "\n".join(new_lines) + "\n---\n" + text, added, skipped
    kept = [l for l in fm.split("\n")
            if not any(re.match(r"^%s:\s*(\[\s*\])?\s*$" % re.escape(k), l) for k in empty_present)]
    kept = [l for l in kept if l.strip()] if not "".join(kept).strip() else kept
    block = "\n".join(kept + new_lines) if kept else "\n".join(new_lines)
    return "---\n" + block + "\n---\n" + body, added, skipped


# --- moves -----------------------------------------------------------------------------

def _in_table(text, offset):
    start = text.rfind("\n", 0, offset) + 1
    return text[start:offset].lstrip().startswith("|")


def _md_url(path, angle):
    return "<%s>" % path if angle else quote(path, safe="/()&'!,;=+@-._~")


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
            if (alias is None and alias_on_rename and not l.embed and l.heading is None
                    and V.link_name(target) != V.link_name(want)):
                alias = l.path.rsplit("/", 1)[-1]
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


def rewrite_canvas(moves, text):
    try:
        data = json.loads(text)
    except ValueError:
        return text, []
    changes = []
    for node in data.get("nodes", []):
        f = node.get("file")
        if f in moves:
            changes.append({"before": f, "after": moves[f]})
            node["file"] = moves[f]
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
            new_text, ch = rewrite_canvas(moves, text)
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

    emptied = sorted({V.folder(s) for s in moves if V.folder(s)
                      and not any(f.startswith(V.folder(s) + "/") for f in after.files)})
    return moves, edits, {"moves": [{"from": k, "to": v} for k, v in moves.items()],
                          "link_rewrites": report, "edits": text_report, "frontmatter": fm_report,
                          "emptied_folders": emptied,
                          "files_changed": len(set(edits) | set(moves.values()))}


def execute(vault, plan, moves, edits):
    root = vault.root
    for src, dst in moves.items():
        s, d = inside(root, src, "move source"), safe_target(root, dst, "move target")
        os.makedirs(os.path.dirname(d), exist_ok=True)
        inside(root, dst, "move target")  # re-check now that the parent folders exist
        if os.path.exists(d) and not os.path.samefile(s, d):
            raise PlanError("move target appeared on disk: %s" % dst)
        if os.path.exists(d):  # case-only rename on a case-insensitive disk
            tmp = s + ".obsidian-vault-tmp"
            os.rename(s, tmp)
            s = tmp
        os.rename(s, d)
    for rel, text in edits.items():
        with open(inside(root, rel, "edited file"), "w", encoding="utf-8") as f:
            f.write(text)


# --- import ----------------------------------------------------------------------------

def do_import(vault, files, to_folder):
    to_folder = to_folder.rstrip("/")
    if to_folder:
        safe_target(vault.root, to_folder, "--to")
    done = []
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
        done.append((src, rel))
    for src, rel in done:
        dst = safe_target(vault.root, rel, "import target")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        inside(vault.root, rel, "import target")
        shutil.copy2(src, dst)
    return [rel for _, rel in done]


# --- cli -------------------------------------------------------------------------------

def load_plan(path):
    try:
        with open(path, encoding="utf-8") as f:
            plan = json.load(f)
    except (OSError, ValueError) as e:
        raise PlanError("cannot read plan %s: %s" % (path, e))
    if not isinstance(plan, dict) or not plan.get("description"):
        raise PlanError("plan needs a description (it becomes the commit message)")
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

    root = args.vault or V.load_config(SKILL)["vaultPath"]
    try:
        if args.cmd == "checkpoint":
            git_check(root)
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
                cp = ensure_clean(root, args.checkpoint, plan["description"])
                vault = V.Vault(root)
                moves, edits, report = compute(vault, plan)
                execute(vault, plan, moves, edits)
                report["checkpoint"] = cp
                report["commit"] = commit(root, plan["description"])
                report["undo"] = "git -C %r revert %s" % (root, report["commit"]) if report["commit"] else None
                res = report
    except PlanError as e:
        json.dump({"error": str(e)}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 2
    json.dump(res, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
