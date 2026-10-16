#!/usr/bin/env python3
"""Rank enabled plugins by always-on token cost against how much you actually use them.

Joins three sources Claude Code exposes but never correlates:
  - `claude plugin list`            -> what is enabled, and at which scope
  - `claude plugin details <name>`  -> projected always-on token cost
  - ~/.claude.json `skillUsage`     -> invocation counts and last-used dates

Output is a ranked table plus a cut list. Writes nothing; the caller decides.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CONFIG = Path.home() / ".claude.json"
SESSIONS = Path.home() / ".claude" / "projects"
ALWAYS_ON = re.compile(r"Always-on:\s*~?([\d,]+)\s*tok")
SKILL_COUNT = re.compile(r"^\s*Skills \((\d+)\)", re.M)
MCP_COUNT = re.compile(r"^\s*MCP servers \((\d+)\)", re.M)
SKILL_CALL = re.compile(r'"name":"Skill","input":\{"skill":"([^"]+)"')
# Defaults, not laws. These were tuned against one machine's config and have no
# principle behind them, so every one is overridable from the command line.
COLD_DAYS = 60        # days without use before an expensive plugin counts as cold
NEGLIGIBLE_TOK = 500  # below this, cutting buys less than the risk of missing it
ONE_OFF_USES = 3      # "I tried it once" is not usage
EXPENSIVE_TOK = 1000  # at or above this, weak usage is no longer worth the rent


def run(cmd, timeout=90):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout + p.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return f"__ERROR__ {e}"


def installed_plugins():
    """Return {name: {'enabled': bool, 'scopes': set}} keyed by plugin@marketplace."""
    out = run(["claude", "plugin", "list"])
    if out.startswith("__ERROR__"):
        sys.exit(f"could not run `claude plugin list`: {out}")
    plugins, current = {}, None
    for line in out.splitlines():
        m = re.match(r"\s*❯\s*(\S+)", line)
        if m:
            current = m.group(1)
            plugins.setdefault(current, {"enabled": False, "scopes": set()})
            continue
        if current is None:
            continue
        m = re.match(r"\s*Scope:\s*(\S+)", line)
        if m:
            plugins[current]["scopes"].add(m.group(1))
        elif "Status:" in line:
            # A plugin installed at several scopes counts as enabled if any row is.
            plugins[current]["enabled"] |= "✔" in line
    return plugins


def details(name):
    out = run(["claude", "plugin", "details", name])
    if out.startswith("__ERROR__"):
        return {"name": name, "tok": 0, "skills": 0, "mcp": 0, "error": out}
    tok = ALWAYS_ON.search(out)
    skills = SKILL_COUNT.search(out)
    mcp = MCP_COUNT.search(out)
    return {
        "name": name,
        "tok": int(tok.group(1).replace(",", "")) if tok else 0,
        "skills": int(skills.group(1)) if skills else 0,
        "mcp": int(mcp.group(1)) if mcp else 0,
        "error": None,
    }


def usage_by_namespace():
    """Collapse skillUsage into per-plugin totals.

    Keys look like 'posthog:signals', 'superpowers:brainstorming', or a bare
    'end-of-day' for personal skills. The namespace before ':' is the plugin.
    """
    try:
        data = json.loads(CONFIG.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"warning: could not read {CONFIG}: {e}", file=sys.stderr)
        return {}
    agg = {}
    for key, val in (data.get("skillUsage") or {}).items():
        ns = key.split(":")[0] if ":" in key else "(personal)"
        count = val.get("usageCount", 0) if isinstance(val, dict) else (val or 0)
        last = val.get("lastUsedAt", 0) if isinstance(val, dict) else 0
        slot = agg.setdefault(ns, {"count": 0, "last": 0})
        slot["count"] += count
        slot["last"] = max(slot["last"], last)
    return agg


def days_since(ms, now_ms):
    return None if not ms else int((now_ms - ms) / 86_400_000)


def encode_project(path):
    """Claude Code names a project's session dir after its path, with '/' and '_' as '-'."""
    return str(path).replace("/", "-").replace("_", "-")


def local_usage(cwd):
    """Skill invocations recorded in THIS repo's session transcripts, by namespace.

    `skillUsage` in ~/.claude.json is machine-wide and cannot say where a skill
    was used, so a plugin busy in one repo looks busy everywhere. Transcripts are
    already stored per project, so counting Skill tool calls there gives the
    per-repo truth. Returns None when this repo has no transcripts, so callers can
    tell "no data" apart from "genuinely zero".
    """
    if not SESSIONS.is_dir():
        return None
    enc = encode_project(Path(cwd).resolve())
    # A worktree gets its own session dir named '<parent>--claude-worktrees-<x>';
    # it is the same codebase, so fold those counts in.
    dirs = [d for d in SESSIONS.iterdir()
            if d.is_dir() and (d.name == enc or d.name.startswith(enc + "-"))]
    if not dirs:
        return None
    counts = {}
    for d in dirs:
        for f in d.glob("*.jsonl"):
            try:
                with open(f, errors="ignore") as fh:
                    for line in fh:
                        if '"name":"Skill"' not in line:
                            continue
                        for skill in SKILL_CALL.findall(line):
                            ns = skill.split(":")[0] if ":" in skill else "(personal)"
                            counts[ns] = counts.get(ns, 0) + 1
            except OSError:
                continue
    return counts


def shared_config_plugins(cwd):
    """Plugins enabled in a git-tracked .claude/settings.json — shared with teammates.

    Disabling one of these from here would edit a committed file and change
    everyone's setup, so they are reported but never proposed for an auto-cut.
    """
    settings = Path(cwd) / ".claude" / "settings.json"
    if not settings.is_file():
        return set()
    # The repo may be untrusted (a fresh clone); its fsmonitor hook must not run.
    probe = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "ls-files", "--error-unmatch", str(settings)],
        cwd=cwd, capture_output=True, text=True,
    )
    if probe.returncode != 0:  # untracked: local to this machine, safe to edit
        return set()
    try:
        data = json.loads(settings.read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    # The key may sit at the top level or under 'enabledPlugins'.
    block = data.get("enabledPlugins", data)
    return {k for k, v in block.items() if v is True and "@" in k}


def verdict(row, t=None):
    """Cut recommendation. Cost only matters when the plugin is also unused.

    A cheap plugin is never worth cutting, however dead — the saving is smaller
    than the cost of discovering later that you wanted it.

    `t` carries the thresholds; omitting it uses the module defaults so the
    function stays callable on its own.
    """
    if t is None:
        t = argparse.Namespace(cold_days=COLD_DAYS, negligible_tok=NEGLIGIBLE_TOK,
                               one_off_uses=ONE_OFF_USES, expensive_tok=EXPENSIVE_TOK)
    # Enabled in a committed settings.json: cutting it here would change every
    # teammate's setup, so never propose it — report and let the user decide.
    if row["shared"]:
        return "SHARED", "enabled in committed .claude/settings.json"
    # Checked before the negligible-cost escape: `plugin details` excludes MCP
    # tool schemas, so a plugin reporting few tokens can still put a pile of
    # tool names in the prompt every turn.
    if row["mcp"] > 0 and row["count"] == 0:
        return "CUT", f"{row['mcp']} MCP server(s), never used"
    if row["tok"] < t.negligible_tok:
        return "keep", "negligible cost"
    if row["count"] == 0:
        return "CUT", "never invoked"
    # Per-repo evidence cannot justify a global disable: a plugin silent here but
    # busy elsewhere is in the wrong SCOPE, not dead. Cutting it globally would
    # break the repos that do use it.
    if row["here"] == 0 and row["tok"] >= t.expensive_tok:
        return "SCOPE", f"0 uses here, {row['count']} elsewhere — scope it per-project"
    if row["days"] is not None and row["days"] > t.cold_days:
        return "CUT", f"cold {row['days']}d"
    if row["count"] < t.one_off_uses and row["tok"] >= t.expensive_tok:
        return "CUT", f"only {row['count']} use(s) for ~{row['tok']:,} tok"
    if row["here"] is not None and row["here"] > 0:
        return "keep", f"{row['here']} uses here"
    return "keep", f"{row['count']} uses"


def mcp_servers():
    out = run(["claude", "mcp", "list"], timeout=120)
    servers = []
    for line in out.splitlines():
        m = re.match(r"^(\S.*?):\s+(.*?)\s+-\s+(.*)$", line.strip())
        if m:
            servers.append({"name": m.group(1), "target": m.group(2), "health": m.group(3)})
    return servers


def personal_skills():
    """Skills in ~/.claude/skills, which are always on and not owned by any plugin."""
    root = Path.home() / ".claude" / "skills"
    if not root.is_dir():
        return []
    found = []
    for skill_md in sorted(root.glob("*/SKILL.md")):
        try:
            head = skill_md.read_text(errors="ignore")[:4000]
        except OSError:
            continue
        m = re.match(r"---\s*\n(.*?)\n---", head, re.S)
        if not m:
            continue
        desc = re.search(r"^description:\s*(.+(?:\n\s+.+)*)$", m.group(1), re.M)
        found.append((skill_md.parent.name, " ".join(desc.group(1).split()) if desc else ""))
    return found


def mcp_inventory():
    """MCP servers by scope, read from config rather than probed (no network)."""
    try:
        data = json.loads(CONFIG.read_text())
    except (OSError, json.JSONDecodeError):
        return {}, {}
    glob_servers = sorted(data.get("mcpServers", {}))
    per_project = {}
    for path, cfg in (data.get("projects") or {}).items():
        names = sorted((cfg.get("mcpServers") or {}))
        if names:
            per_project[path] = names
    return glob_servers, per_project


def write_markdown(path, rows, cwd):
    """Full local inventory: everything installed, enabled or not.

    Written to disk rather than into the prompt, so completeness is free here —
    this is the opposite tradeoff from the always-on skill listing.
    """
    enabled = [r for r in rows if r["enabled"]]
    disabled = [r for r in rows if not r["enabled"]]
    total = sum(r["tok"] for r in enabled)
    glob_servers, per_project = mcp_inventory()
    personal = personal_skills()
    stamp = time.strftime("%Y-%m-%d %H:%M")

    L = [
        "# Installed Claude Code tooling",
        "",
        f"Generated {stamp} by `context-audit`. Re-run after installing or removing anything.",
        "",
        "## Summary",
        "",
        f"- **{len(enabled)} plugins enabled** — ~{total:,} tok always-on",
        f"- **{len(disabled)} plugins installed but disabled** — 0 tok, enable live with "
        "`claude plugin enable <name>`",
        f"- **{len(personal)} personal skills** in `~/.claude/skills` — always on",
        f"- **{len(glob_servers)} global MCP servers**, "
        f"**{sum(len(v) for v in per_project.values())}** scoped to specific projects",
        "",
        "## Enabled plugins",
        "",
        "| Plugin | Tokens | Skills | MCP | Uses | Last used |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for r in sorted(enabled, key=lambda r: -r["tok"]):
        last = f"{r['days']}d ago" if r["days"] is not None else "never"
        L.append(f"| `{r['short']}` | {r['tok']:,} | {r['skills']} | {r['mcp']} | {r['count']} | {last} |")

    L += ["", "## Disabled plugins", "",
          "Installed and ready. None of this is in the prompt; enable live when a task needs it.", "",
          "| Plugin | Tokens if enabled | Skills | MCP | Enable with |", "|---|---:|---:|---:|---|"]
    for r in sorted(disabled, key=lambda r: -r["tok"]):
        L.append(f"| `{r['short']}` | {r['tok']:,} | {r['skills']} | {r['mcp']} | "
                 f"`claude plugin enable {r['name']}` |")

    L += ["", "## Personal skills", "", "Always on, from `~/.claude/skills`.", ""]
    for name, desc in personal:
        L.append(f"- **{name}** — {desc[:160]}" if desc else f"- **{name}**")

    L += ["", "## MCP servers", ""]
    L.append(f"Global (every project): {', '.join(f'`{s}`' for s in glob_servers) or '_none_'}")
    L += ["", "Project-scoped:", ""]
    if per_project:
        for p, names in sorted(per_project.items()):
            L.append(f"- `{Path(p).name}` — {', '.join(f'`{n}`' for n in names)}")
    else:
        L.append("_none_")
    L += ["", "---", "",
          f"Audited from `{cwd}`. Plugin enable/disable applies live — no restart needed.", ""]

    Path(path).write_text("\n".join(L))
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--all", action="store_true", help="include disabled plugins")
    ap.add_argument("--skip-mcp", action="store_true", help="skip the MCP health probe")
    ap.add_argument("--write-md", nargs="?", const=str(Path.home() / ".claude" / "INSTALLED-TOOLING.md"),
                    metavar="PATH", help="write the full inventory to a markdown file")

    t = ap.add_argument_group(
        "verdict thresholds",
        "Heuristics, not laws — tuned against one machine. Raise them to cut harder, "
        "lower them to be more conservative.")
    t.add_argument("--negligible-tok", type=int, default=NEGLIGIBLE_TOK, metavar="N",
                   help=f"never cut a plugin below this always-on cost (default {NEGLIGIBLE_TOK})")
    t.add_argument("--cold-days", type=int, default=COLD_DAYS, metavar="N",
                   help=f"days unused before an expensive plugin counts as cold (default {COLD_DAYS})")
    t.add_argument("--one-off-uses", type=int, default=ONE_OFF_USES, metavar="N",
                   help=f"uses below which an expensive plugin reads as a one-off (default {ONE_OFF_USES})")
    t.add_argument("--expensive-tok", type=int, default=EXPENSIVE_TOK, metavar="N",
                   help=f"cost at or above which weak usage stops justifying it (default {EXPENSIVE_TOK})")
    args = ap.parse_args()
    if args.write_md:
        args.all = True  # an inventory that omits disabled plugins defeats the point

    now_ms = time.time() * 1000
    plugins = installed_plugins()
    targets = [n for n, v in plugins.items() if v["enabled"] or args.all]

    with ThreadPoolExecutor(max_workers=8) as pool:
        detail_rows = list(pool.map(details, targets))

    usage = usage_by_namespace()
    cwd = os.getcwd()
    here = local_usage(cwd)
    shared = shared_config_plugins(cwd)

    # local_usage() scrapes an internal transcript format with no stability
    # guarantee. If that format changes, every count silently reads zero and
    # every expensive plugin gets a bogus SCOPE verdict. Transcripts that exist
    # but yield no Skill calls at all is the signature of that breakage, so
    # discard the signal rather than act on it.
    if here is not None and sum(here.values()) == 0:
        print("warning: found session transcripts but no skill invocations in them — "
              "per-repo usage unavailable (transcript format may have changed); "
              "falling back to global counts", file=sys.stderr)
        here = None
    rows = []
    for d in detail_rows:
        short = d["name"].split("@")[0]
        u = usage.get(short, {"count": 0, "last": 0})
        row = {
            **d,
            "short": short,
            "enabled": plugins[d["name"]]["enabled"],
            "scopes": sorted(plugins[d["name"]]["scopes"]),
            "count": u["count"],
            "days": days_since(u["last"], now_ms),
            "here": None if here is None else here.get(short, 0),
            "shared": d["name"] in shared,
        }
        row["verdict"], row["why"] = verdict(row, args)
        rows.append(row)
    rows.sort(key=lambda r: -r["tok"])

    servers = [] if args.skip_mcp else mcp_servers()

    if args.write_md:
        out = write_markdown(args.write_md, rows, cwd)
        print(f"inventory written to {out}")
        if args.json:
            print(json.dumps({"thresholds": {k: v for k, v in vars(args).items() if k in ("negligible_tok", "cold_days", "one_off_uses", "expensive_tok")}, "plugins": rows, "mcp": servers}, indent=2))
        return

    if args.json:
        print(json.dumps({"thresholds": {k: v for k, v in vars(args).items() if k in ("negligible_tok", "cold_days", "one_off_uses", "expensive_tok")}, "plugins": rows, "mcp": servers}, indent=2))
        return

    enabled = [r for r in rows if r["enabled"]]
    total = sum(r["tok"] for r in enabled)
    cuts = [r for r in enabled if r["verdict"] == "CUT"]
    savable = sum(r["tok"] for r in cuts)

    scope_note = f"in {Path(cwd).name}" if here is not None else "no transcripts for this repo"
    print(f"\nENABLED PLUGINS — {len(enabled)} on, ~{total:,} tok always-on  ({scope_note})")
    print(f"thresholds: negligible<{args.negligible_tok} tok · cold>{args.cold_days}d · "
          f"one-off<{args.one_off_uses} uses · expensive>={args.expensive_tok} tok\n")
    print(f"{'plugin':26s} {'tok':>8s} {'skills':>7s} {'mcp':>4s} {'uses':>6s} {'here':>5s} {'last':>7s}  verdict")
    print("-" * 86)
    for r in rows:
        if not r["enabled"] and not args.all:
            continue
        last = f"{r['days']}d" if r["days"] is not None else "never"
        flag = "" if r["enabled"] else " (off)"
        local = "  -" if r["here"] is None else str(r["here"])
        print(
            f"{r['short'] + flag:26s} {r['tok']:8,d} {r['skills']:7d} {r['mcp']:4d} "
            f"{r['count']:6d} {local:>5s} {last:>7s}  {r['verdict']} — {r['why']}"
        )

    scope_rows = [r for r in enabled if r["verdict"] == "SCOPE"]
    if scope_rows:
        total_scope = sum(r["tok"] for r in scope_rows)
        print(f"\nWRONG SCOPE — ~{total_scope:,} tok carried here but used elsewhere")
        print("  Do NOT disable these globally; the repos that use them would break.")
        print("  Disable globally, then re-enable only where used:")
        for r in sorted(scope_rows, key=lambda r: -r["tok"]):
            print(f"    claude plugin disable {r['name']}   # ~{r['tok']:,} tok")
            print(f'    # then in each repo that uses it, .claude/settings.local.json:')
            print(f'    #   {{ "enabledPlugins": {{ "{r["name"]}": true }} }}')

    shared_rows = [r for r in enabled if r["shared"]]
    if shared_rows:
        print(f"\nSHARED CONFIG — {len(shared_rows)} plugin(s) enabled in a committed .claude/settings.json")
        print("  Disabling these here would change your teammates' setup too.")
        for r in shared_rows:
            local = "unknown" if r["here"] is None else f"{r['here']} uses here"
            print(f"  {r['short']:24s} ~{r['tok']:,} tok, {local}")
        print("  To turn one off for yourself only, add to .claude/settings.local.json:")
        for r in shared_rows:
            print(f'    {{ "enabledPlugins": {{ "{r["name"]}": false }} }}')

    # `plugin details` excludes MCP tool schemas, but a server's tool NAMES are
    # announced every turn, so a 0-tok plugin carrying servers is not free.
    mcp_only = [r for r in enabled if r["tok"] < args.negligible_tok and r["mcp"] > 0]
    if mcp_only:
        print(f"\nMCP-BEARING, LOW SKILL COST — {sum(r['mcp'] for r in mcp_only)} servers")
        print("  Reported tok excludes MCP tool schemas. Names still cost each turn.")
        for r in sorted(mcp_only, key=lambda r: -r["mcp"]):
            last = f"{r['days']}d" if r["days"] is not None else "never"
            print(f"  {r['short']:26s} {r['mcp']:2d} server(s), {r['count']} uses, last {last}")

    if cuts:
        print(f"\nCUT LIST — ~{savable:,} tok ({savable * 100 // max(total, 1)}% of always-on)\n")
        for r in sorted(cuts, key=lambda r: -r["tok"]):
            print(f"  claude plugin disable {r['name']}   # ~{r['tok']:,} tok — {r['why']}")
        print(f"\n  after cut: ~{total - savable:,} tok always-on")
    else:
        print("\nNothing to cut. Every enabled plugin is either cheap or in use.")

    if servers:
        print(f"\nMCP SERVERS — {len(servers)} configured")
        unhealthy = [s for s in servers if "✔" not in s["health"]]
        for s in unhealthy:
            print(f"  ! {s['name']}: {s['health']}")
        print(f"  {len(servers) - len(unhealthy)} connected, {len(unhealthy)} need attention")
        print("  Tool schemas load on demand (ToolSearch), but every server's tool")
        print("  NAMES sit in the prompt each turn. Scope work-only servers to the")
        print("  projects that need them via that project's .mcp.json.")

    errs = [r for r in rows if r["error"]]
    if errs:
        print(f"\n{len(errs)} plugin(s) failed to report: {', '.join(r['short'] for r in errs)}")


if __name__ == "__main__":
    main()
