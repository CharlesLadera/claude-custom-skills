#!/usr/bin/env python3
"""Read-only vault scanner. Prints JSON. Never writes, moves, deletes or spawns a process.

  scan.py lint [--limit N]                      problems, grouped by kind
  scan.py backlinks <note>                      who links to a note
  scan.py find [--tag T] [--prop K=V] [--text RE] [--name RE] [--folder F]
  scan.py unlinked <note>                       plain-text mentions that are not links yet
  scan.py resolve <link> [--from <note>]        what a link resolves to
  scan.py inbox                                 inbox notes with age, size and first line
  scan.py rules                                 the rules in force and where they came from

<note> is a vault-relative path or anything a wikilink would resolve.
Every command takes --vault PATH; without it, vaultPath from config.json is used.
"""
import argparse
import json
import os
import re
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(SKILL, "lib"))
import vault as V  # noqa: E402


UNPORTABLE = re.compile(r'[<>:"|?*\\]|[ .](/|$)')


def open_vault(args):
    root = args.vault or V.load_config(SKILL)["vaultPath"]
    vault = V.Vault(V.require_dir(root))
    return vault, V.load_rules(vault.root)


def target_of(vault, name):
    hit = vault.lower.get(name.lower()) or vault.resolve(name, "")
    if not hit:
        raise SystemExit("no file matches %r" % name)
    return hit


def all_records(vault):
    return {p: V.note_record(vault, p) for p in vault.notes}


def lint(vault, rules, limit):
    recs = all_records(vault)
    ignore = rules.get("lintIgnore", []) + [rules["templates"] + "/**"]
    inbound = {p: 0 for p in vault.files}
    broken, templater = [], []
    for p, r in recs.items():
        if not V.matches_any(p, [rules["templates"] + "/**"]) and "<%" in V._code_mask(r["text"]):
            templater.append({"path": p})
        for l in r["links"]:
            if l.path.startswith("<%"):
                continue
            hit = vault.resolve(l.path, p)
            if hit is None:
                if not V.matches_any(p, ignore):
                    broken.append({"source": p, "line": l.line, "link": l.raw})
            elif hit != p:
                inbound[hit] += 1

    for p in vault.files:  # canvases link to notes too: file cards and links in text cards
        if not p.lower().endswith(".canvas"):
            continue
        try:
            data = json.loads(vault.read(p))
        except ValueError:
            continue
        nodes = data.get("nodes") if isinstance(data, dict) else None
        for node in nodes if isinstance(nodes, list) else []:
            if not isinstance(node, dict):
                continue
            for f in (node.get("file"), node.get("background")):  # file cards, group backgrounds
                if isinstance(f, str) and f in inbound:
                    inbound[f] += 1
            t = node.get("text")
            if isinstance(t, str):
                for l in V.parse_links(t):
                    hit = vault.resolve(l.path, p)
                    if hit and hit != p:
                        inbound[hit] += 1

    naming = rules["naming"]
    pat = re.compile(naming["pattern"])
    fm_rules = rules["frontmatter"]
    out = {k: [] for k in ("missing_frontmatter", "missing_keys", "naming", "empty", "orphans",
                           "duplicate_names")}
    for p, r in recs.items():
        if V.matches_any(p, ignore):
            continue
        name = V.stem(p)
        if not V.matches_any(p, naming["exempt"]) and not pat.match(name):
            out["naming"].append({"path": p, "suggest": V.folder(p) + ("/" if V.folder(p) else "")
                                  + V.kebab(name) + ".md"})
        if not V.matches_any(p, fm_rules["exempt"]):
            if not r["has_frontmatter"]:
                out["missing_frontmatter"].append({"path": p, "created_guess": V.file_created(vault, p)})
            else:
                # "created|date" means either key satisfies the requirement
                miss = [k for k in fm_rules["required"]
                        if not any(V.has_value(r["frontmatter"].get(alt)) for alt in k.split("|"))]
                if miss:
                    out["missing_keys"].append({"path": p, "missing": miss})
        if not r["body"].strip() or re.fullmatch(r"\s*(#.*)?\s*", r["body"]):
            out["empty"].append({"path": p})
        if inbound[p] == 0 and not V.matches_any(p, rules["orphans"]["exempt"]):
            out["orphans"].append({"path": p})

    # Windows (and Obsidian Sync to it) can't store these; checked on every file, not only notes.
    # lintIgnore doesn't apply: an archived file with such a name breaks Sync just the same.
    out["unportable_names"] = [{"path": p} for p in vault.files if UNPORTABLE.search(p)]

    stems = {V.stem(p).lower() for p in vault.notes}
    for item in out["naming"]:
        sug = V.stem(item["suggest"])
        if sug.lower() in stems and sug.lower() != V.stem(item["path"]).lower():
            item["suggest_taken"] = True  # pick another name, or links become ambiguous
        if re.match(r"^untitled(-\d+)?$", sug):
            item["name_from_content"] = True

    by_name = {}
    dup_exempt = ignore + rules.get("duplicates", {}).get("exempt", [])
    for p in vault.notes:
        if not V.matches_any(p, dup_exempt):
            by_name.setdefault(V.stem(p).lower(), []).append(p)
    out["duplicate_names"] = [{"name": k, "paths": v} for k, v in sorted(by_name.items()) if len(v) > 1]
    out["broken_links"] = broken
    out["templater_leftovers"] = templater

    summary = {k: len(v) for k, v in out.items()}
    if limit:
        out = {k: v[:limit] for k, v in out.items()}
    return {"vault": vault.root, "rules": rules["_source"], "notes": len(recs), "summary": summary,
            "items": out}


def backlinks(vault, name):
    target = target_of(vault, name)
    hits = []
    for p in vault.notes:
        for l in V.parse_links(vault.read(p)):
            if p != target and vault.resolve(l.path, p) == target:
                hits.append({"source": p, "line": l.line, "link": l.raw})
    return {"target": target, "count": len(hits), "backlinks": hits}


def find(vault, args):
    out = []
    text_re = re.compile(args.text, re.I) if args.text else None
    name_re = re.compile(args.name, re.I) if args.name else None
    for p in vault.notes:
        if args.folder and not (p + "/").lower().startswith(args.folder.rstrip("/").lower() + "/"):
            continue
        if name_re and not name_re.search(V.stem(p)):
            continue
        r = V.note_record(vault, p)
        if args.tag and not any(t == args.tag.lower() or t.startswith(args.tag.lower() + "/")
                                for t in r["tags"]):
            continue
        if args.prop:
            k, _, v = args.prop.partition("=")
            have = r["frontmatter"].get(k)
            vals = have if isinstance(have, list) else [have]
            if have is None or (v and v not in [str(x) for x in vals]):
                continue
        lines = []
        if text_re:
            lines = [{"line": i + 1, "text": ln.strip()[:200]}
                     for i, ln in enumerate(r["text"].splitlines()) if text_re.search(ln)]
            if not lines:
                continue
        out.append({"path": p, "tags": sorted(r["tags"]), "matches": lines[:5]})
    return {"count": len(out), "results": out}


def unlinked(vault, name):
    target = target_of(vault, name)
    rec = V.note_record(vault, target)
    terms = [V.stem(target)] + rec["aliases"]
    terms = [t for t in terms if len(t) >= 3]
    pats = [re.compile(r"(?<![\w\[])" + re.escape(t) + r"(?![\w\]])", re.I) for t in terms]
    hits = []
    for p in vault.notes:
        if p == target:
            continue
        text = vault.read(p)
        if any(vault.resolve(l.path, p) == target for l in V.parse_links(text)):
            continue
        masked = V._code_mask(V.split_frontmatter(text)[1])
        for i, ln in enumerate(masked.splitlines()):
            if any(pt.search(ln) for pt in pats):
                hits.append({"source": p, "text": ln.strip()[:200]})
                break
    return {"target": target, "terms": terms, "count": len(hits), "mentions": hits}


def inbox(vault, rules):
    today = date.today()
    out = []
    for p in vault.notes:
        if not p.startswith(rules["inbox"] + "/"):
            continue
        r = V.note_record(vault, p)
        created = V.file_created(vault, p)
        first = next((ln.strip() for ln in r["body"].splitlines() if ln.strip()), "")
        out.append({"path": p, "created": created,
                    "age_days": (today - date.fromisoformat(created)).days,
                    "chars": len(r["body"].strip()), "first_line": first[:120]})
    return {"count": len(out), "notes": out}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vault")
    sub = ap.add_subparsers(dest="cmd", required=True)
    # --vault works before or after the subcommand
    vault_opt = argparse.ArgumentParser(add_help=False)
    vault_opt.add_argument("--vault", default=argparse.SUPPRESS)
    s = sub.add_parser("lint", parents=[vault_opt])
    s.add_argument("--limit", type=int, default=0)
    s = sub.add_parser("backlinks", parents=[vault_opt])
    s.add_argument("note")
    s = sub.add_parser("find", parents=[vault_opt])
    for opt in ("--tag", "--prop", "--text", "--name", "--folder"):
        s.add_argument(opt)
    s = sub.add_parser("unlinked", parents=[vault_opt])
    s.add_argument("note")
    s = sub.add_parser("resolve", parents=[vault_opt])
    s.add_argument("link")
    s.add_argument("--from", dest="source", default="")
    sub.add_parser("inbox", parents=[vault_opt])
    sub.add_parser("rules", parents=[vault_opt])
    args = ap.parse_args(argv)

    vault, rules = open_vault(args)
    if args.cmd == "lint":
        res = lint(vault, rules, args.limit)
    elif args.cmd == "backlinks":
        res = backlinks(vault, args.note)
    elif args.cmd == "find":
        res = find(vault, args)
    elif args.cmd == "unlinked":
        res = unlinked(vault, args.note)
    elif args.cmd == "resolve":
        res = {"link": args.link, "from": args.source, "resolves_to": vault.resolve(args.link, args.source)}
    elif args.cmd == "inbox":
        res = inbox(vault, rules)
    else:
        res = rules
    json.dump(res, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
