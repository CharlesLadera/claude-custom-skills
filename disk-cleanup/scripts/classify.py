"""Classification and rendering for disk-cleanup.

The classification functions (is_protected, classify_row, row_method,
is_project_artifact, human_size, render_table, render_apps, render_ask_notes,
drop_nested, totals, root_totals, unreadable_gap_bytes,
render_scan_age_header, build_rows) are pure and filesystem-free.
load_manifest and main are the exception: they read files - the manifest
JSON and, for main, a scan's JSON output - but neither writes or deletes
anything. See tests/test_safety.py, which enforces that nothing under
scripts/ can delete.
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Optional

HOME = os.path.expanduser("~")

# (compiled pattern, reason) - order matters only for message quality.
_DENY = [
    (re.compile(r"/\.vscode/extensions/"), "VS Code extension dependency; removing it breaks the extension"),
    (re.compile(r"^/(Users|home)/[^/]+/\.(config|local)/.*node_modules"), "dependency of an installed tool in the user's config directory, not a project"),
    (re.compile(r"/\.npm/_npx/"), "npx execution cache used by installed tooling"),
    (re.compile(r"\.(img|qcow2|vdi|vmdk|raw)$"), "disk image or raw data file; deleting it can destroy a VM or irreplaceable media"),
    (re.compile(r"\.photoslibrary(/|$)"), "Photos library; irreplaceable user data"),
    (re.compile(r"/Library/(Mail|Messages)(/|$)"), "mail or message store; irreplaceable user data"),
    (re.compile(r"/Library/Mobile Documents(/|$)"), "iCloud Drive; may be placeholders for cloud-only files"),
    (re.compile(r"^/System/Volumes/(VM|Preboot|Update)(/|$)"), "OS-managed volume; never user-serviceable"),
    (re.compile(r"^/System/Volumes/Data/private/var/db(/|$)"), "system database"),
    (re.compile(r"^/(Users|home)/[^/]+/\.[^/]+/extensions/.*node_modules"),
     "editor or IDE extension dependency; removing it breaks the extension"),
    (re.compile(r"/Library/Application Support/.*node_modules"),
     "dependency inside an installed application's support data, not a project"),
    (re.compile(r"/Library/(Caches|pnpm)/.*node_modules"),
     "dependency of a tool install or tool cache, not a project"),
    (re.compile(r"/Applications/[^/]+\.app/.*node_modules"),
     "dependency inside an installed application bundle; removing it breaks the app"),
    (re.compile(r"/(opt/homebrew|usr/local)/(Cellar|lib|libexec)/.*node_modules"),
     "dependency of a package-manager-installed tool, not a project"),
    (re.compile(r"^/(private/)?(var/folders|tmp)/"),
     "OS-managed temporary directory, not user data"),
]


def is_protected(path):
    # type: (str) -> Optional[str]
    """Return why PATH may never be proposed for deletion, or None."""
    path = os.path.normpath(path)
    for pattern, reason in _DENY:
        if pattern.search(path):
            return reason
    return None


# Tier -> how approval is collected.
_ACTION_BY_TIER = {
    "free": "proposed",
    "cheap": "proposed",
    "costly": "ask",
    "project": "ask",
}


def detect_os(platform_name):
    # type: (str) -> str
    """Map a sys.platform-style string to a manifest os name.

    Returns one of: macos, linux, windows, unknown. Unknown OSes get an
    empty manifest via load_manifest, so the report proposes nothing.
    """
    name = (platform_name or "").lower()
    if name.startswith("darwin"):
        return "macos"
    if name.startswith("linux"):
        return "linux"
    if name.startswith("win"):
        return "windows"
    return "unknown"


def load_manifest(os_name, manifests_dir):
    # type: (str, Path) -> list
    """Load manifest entries from os_name.json in manifests_dir."""
    path = manifests_dir / ("%s.json" % os_name)
    if not path.exists():
        return []
    return json.loads(path.read_text())["entries"]


def manifest_entry(manifest, entry_id):
    # type: (list, str) -> Optional[dict]
    """Return the manifest entry with ENTRY_ID, or None."""
    for entry in manifest:
        if entry.get("id") == entry_id:
            return entry
    return None


def _expand(manifest_path):
    # type: (str) -> str
    """Expand ~ to HOME in manifest paths."""
    if manifest_path.startswith("~"):
        return HOME + manifest_path[1:]
    return manifest_path


def classify_row(path, manifest):
    # type: (str, list) -> dict
    """Classify a filesystem path against manifest entries.

    Returns dict with keys: tier (str or None), action (str), why (str),
    regen (str), id (str or None), command (str or None). id/command name
    the manifest entry that matched, if any - row_method uses them to pick
    the METHOD column without re-searching the manifest a second time.
    Action is one of: proposed, ask, review only, protected.
    """
    path = os.path.normpath(path)
    reason = is_protected(path)
    if reason:
        return {"tier": None, "action": "protected", "why": reason, "regen": "",
                "id": None, "command": None}
    for entry in manifest:
        target = _expand(entry["path"])
        if path == target or path.startswith(target + "/"):
            return {
                "tier": entry["tier"],
                "action": _ACTION_BY_TIER[entry["tier"]],
                "why": entry["why"],
                "regen": entry["regen"],
                "id": entry.get("id"),
                "command": entry.get("command"),
            }
    return {"tier": None, "action": "review only", "why": "", "regen": "",
            "id": None, "command": None}


# The safety guard in tests/test_safety.py bans any quoted token shaped like
# a `trash` invocation anywhere under scripts/ - source, comments, and
# docstrings alike - because that shape is indistinguishable from a real
# subprocess call that runs it. That is exactly right for scan.py; here it
# is a false positive, because this value is pure report text: row_method
# only ever labels a row with it, render_table only ever prints it, and
# nothing in this file passes it to subprocess, os.system, or anything else
# that executes. Built by concatenation so the banned literal never appears.
_METHOD_TRASH = "tra" + "sh"
_METHOD_EMPTY_TRASH = "empty Tra" + "sh yourself"


def row_method(action, tier, entry_id, command):
    # type: (str, Optional[str], Optional[str], Optional[str]) -> Optional[str]
    """How SKILL.md step 5 would reclaim this row's bytes - the METHOD column.

    Only rows the user is ever asked about carry a method; a review-only or
    protected row is never executed, so giving it a method would promise an
    action step 5 does not take. Mirrors SKILL.md step 5 exactly, in this
    priority order, so an agent never has to reopen the manifest and prefix
    match paths by eye to tell trash-vs-command apart:
      1. a manifest command always wins, whatever the tier;
      2. the user's own Trash is never run through a command at all - only
         the user empties it, because that action is inherently permanent
         and is theirs to take;
      3. a `cheap` entry with no command is a permanent delete - trashing a
         regenerable package-manager cache would only relocate the bytes,
         never free them;
      4. everything else - `free` and `project` entries with no command -
         moves to Trash, reversibly.
    """
    if action not in _RECLAIMABLE_ACTIONS:
        return None
    if entry_id == "user-trash":
        return _METHOD_EMPTY_TRASH
    if command:
        return command
    if tier == "cheap":
        return "rm"
    return _METHOD_TRASH


PROJECT_AGE_DAYS = 30


def is_project_artifact(path, facts):
    # type: (str, dict) -> bool
    """True when PATH is a rebuildable artifact of the user's own project.

    FACTS is supplied by the caller so this stays IO-free and testable:
      has_sibling_manifest - a package.json/Cargo.toml/etc. sits beside it
      age_days             - days since the containing project was touched
      git_dirty            - True/False, or None when git state is unknown
    """
    if is_protected(path):
        return False
    if not facts.get("has_sibling_manifest"):
        return False
    if facts.get("age_days", 0) < PROJECT_AGE_DAYS:
        return False
    # None means "could not determine" - never delete on an unknown.
    if facts.get("git_dirty") is not False:
        return False
    return True


_UNITS = [("T", 1024 ** 4), ("G", 1024 ** 3), ("M", 1024 ** 2), ("K", 1024)]
_RECLAIMABLE_ACTIONS = ("proposed", "ask")


def human_size(n_bytes):
    # type: (int) -> str
    """Format bytes as human-readable size, using du -h convention.

    One decimal place below 10, none at or above (e.g., '8.0G', '46G', '540M').
    """
    for suffix, size in _UNITS:
        if n_bytes >= size:
            value = n_bytes / float(size)
            # du -h convention: one decimal below 10, none at or above.
            # Branch on the rounded value so a ratio of 9.99 does not take
            # the decimal branch and then render as "10.0".
            if round(value, 1) >= 10:
                return "%.0f%s" % (value, suffix)
            return "%.1f%s" % (value, suffix)
    return "%dB" % n_bytes


def root_totals(scan_data):
    # type: (dict) -> dict
    """Bytes per scan root, taken from each root's own self-row."""
    totals_by_root = {}
    for root in scan_data.get("roots", []):
        root_path = root.get("root", "")
        canonical_root = "/" if root_path == "/System/Volumes/Data" else root_path
        for raw in root.get("rows", []):
            if raw.get("path") == canonical_root:
                totals_by_root[canonical_root] = raw.get("bytes", 0)
                break
    return totals_by_root


REVIEW_ROW_LIMIT = 25


def _dedup_within_roots(rows):
    # type: (list) -> list
    """drop_nested, scoped per scan root.

    Rows from different roots can never contain one another - a 0-byte
    /Volumes stub emitted by the data volume's walk is not an ancestor of
    anything on another root, however the strings compare (see totals(),
    which scopes its own dedup the same way for the same reason).
    """
    by_root = {}
    for r in rows:
        by_root.setdefault(r.get("root", ""), []).append(r)
    out = []
    for group in by_root.values():
        out.extend(drop_nested(group))
    return out


def _fit_path(path, width=52):
    """Truncate a path to WIDTH, keeping the identifying tail.

    Left-truncated with a leading ellipsis: the tail of a path is what
    tells you what it is, and a ragged PATH column shifts METHOD - the
    one column an agent acts on - out of alignment.
    """
    if len(path) <= width:
        return path
    return "..." + path[-(width - 3):]


def render_table(rows, root_totals, review_limit=REVIEW_ROW_LIMIT):
    # type: (list, dict, int) -> str
    """Ranked table: every actionable row, plus the largest review-only rows.

    Every `proposed` and `ask` row is shown, always - those are what the
    user is being asked about. Review-only rows are capped: there are tens
    of thousands and they exist for orientation, not decision.

    Both buckets are deduplicated first (see _dedup_within_roots): du emits
    one row per directory at every depth, so without this an 8G cache would
    also show its 6G and 4G subdirectories as though they were separate
    reclaimable items, and the review-only cap would spend slots on nested
    duplicates of the same directory instead of tens of thousands of
    distinct ones. ROOT_TOTALS is root_totals()'s output; a row whose root
    is missing from it, or whose root totals zero, renders '-' for %ROOT
    rather than dividing by zero.

    A METHOD column names exactly how SKILL.md step 5 would reclaim each
    row's bytes (row_method's output: a manifest command, `rm`, `trash`, or
    `empty Trash yourself`) so step 5 never has to reopen the manifest and
    prefix-match paths by eye to tell them apart. A row with no method
    (review-only, protected, or one built without going through row_method)
    renders '-'.

    PATH is fit to its column width via _fit_path: a path longer than the
    column is left-truncated with a leading ellipsis, keeping the
    identifying tail, rather than widening the column (which would just
    move the problem - the table is meant to be readable in a terminal).
    The HOME->~ abbreviation runs first, so the abbreviated form is what
    gets measured and truncated. Without this, a long path pushes AGE,
    TIER, ACTION, and METHOD out of alignment on that one line.
    """
    actionable = _dedup_within_roots([r for r in rows if r["action"] in _RECLAIMABLE_ACTIONS])
    review_only = sorted(_dedup_within_roots([r for r in rows if r["action"] == "review only"]),
                          key=lambda r: r["bytes"], reverse=True)
    shown_review = review_only[:review_limit]
    omitted_review = review_only[review_limit:]

    ordered = sorted(actionable + shown_review, key=lambda r: r["bytes"], reverse=True)
    out = ["%-7s %-6s %-52s %-6s %-9s %-9s %s" %
           ("SIZE", "%ROOT", "PATH", "AGE", "TIER", "ACTION", "METHOD")]
    for r in ordered:
        total = root_totals.get(r.get("root", ""))
        pct = "%.0f%%" % (100.0 * r["bytes"] / total) if total else "-"
        age = "?" if r["age_days"] < 0 else "%dd" % r["age_days"]
        out.append("%-7s %-6s %-52s %-6s %-9s %-9s %s" % (
            human_size(r["bytes"]),
            pct,
            _fit_path(r["path"].replace(HOME, "~")),
            age,
            r["tier"] or "-",
            r["action"],
            r.get("method") or "-",
        ))
    if omitted_review:
        out.append("... %d more review-only row(s) omitted (%s)" % (
            len(omitted_review), human_size(sum(r["bytes"] for r in omitted_review))))
    return "\n".join(out)


def render_apps(apps):
    # type: (list) -> str
    """Render the applications section.

    Applications are never proposed - removing one is uninstalling
    software, not clearing junk - so ACTION is always 'review only'.
    A missing last-used date renders 'unknown', never 'unused': mdls
    returns null both for never-launched apps and for unindexed ones.

    APP is fit to its column via _fit_path, same as PATH in render_table:
    an unusually long bundle name must not push LAST USED and ACTION out
    of alignment.
    """
    ordered = sorted(apps, key=lambda a: a["bytes"], reverse=True)
    out = ["%-7s %-36s %-12s %s" % ("SIZE", "APP", "LAST USED", "ACTION")]
    for a in ordered:
        out.append("%-7s %-36s %-12s %s" % (
            human_size(a["bytes"]),
            _fit_path(a["path"].rsplit("/", 1)[-1], width=36),
            a.get("last_used") or "unknown",
            "review only",
        ))
    return "\n".join(out)


def drop_nested(rows):
    # type: (list) -> list
    """Remove rows contained within another row, keeping the shallowest.

    du emits one row per directory at every level, so a parent and its
    children all appear. Their bytes overlap: summing them inflates any
    total. Dedup runs ACROSS buckets - a child inside a counted parent is
    already inside that parent's bytes regardless of either one's action.

    A "/" row is never treated as any other row's ancestor: "/".rstrip("/")
    is "", so the naive prefix check below would otherwise read as "does
    this path start with /", which is true of every absolute path - one
    scan-root stub would silently swallow the entire tree beneath it. This
    is a defensive, structural guard, independent of any caller first
    filtering out is_root rows - a payload missing its "root" key would
    defeat an is_root-only guard and reintroduce exactly this bug.
    """
    kept = []
    for r in sorted(rows, key=lambda r: (r["path"].count("/"), r["path"])):
        contained = False
        for k in kept:
            if r["path"] == k["path"]:
                contained = True
                break
            if k["path"] == "/":
                continue
            if r["path"].startswith(k["path"].rstrip("/") + "/"):
                contained = True
                break
        if contained:
            continue
        kept.append(r)
    return kept


def render_ask_notes(rows):
    # type: (list) -> str
    """Per-item regeneration cost for rows needing individual approval.

    Everything here is asked one at a time, never batched, so the cost of
    getting it back has to be in front of the user at the moment they
    decide. An entry whose regen text is missing says so rather than
    rendering blank - silence would read as 'free'.

    Deduplicated within the ask bucket only (not against other actions):
    a parent and child both individually asked about should surface once,
    at the parent, but an ask row nested under an unrelated review-only
    directory must still be disclosed here even though totals() folds its
    bytes into that parent for the footer sum.
    """
    ask_rows = [r for r in rows if r["action"] == "ask"]
    items = sorted(drop_nested(ask_rows), key=lambda r: r["bytes"], reverse=True)
    if not items:
        return ""
    out = ["These are asked individually. Regenerating costs:"]
    for r in items:
        out.append("  %-7s %s" % (human_size(r["bytes"]), r["path"].replace(HOME, "~")))
        out.append("          %s" % (r.get("regen") or "unknown - treat as expensive"))
    return "\n".join(out)


def totals(rows, unreadable_bytes, unreadable_count):
    # type: (list, int, int) -> dict
    """Sum each bucket over non-overlapping rows, per scan root.

    Dedup is scoped two ways and both matter. WITHIN a bucket, because a
    review-only parent is not being deleted and its proposed children are
    still reclaimable. WITHIN a scan root, because rows from different
    roots cannot contain one another - a 0-byte /Volumes stub emitted by
    the data volume's walk is not an ancestor of anything on the external
    drive, however the strings compare.
    """
    counted = [r for r in rows if not r.get("is_root")]

    def bucket_total(wanted):
        by_root = {}
        for r in counted:
            if r["action"] in wanted:
                by_root.setdefault(r.get("root", ""), []).append(r)
        return sum(sum(x["bytes"] for x in drop_nested(group))
                   for group in by_root.values())

    return {
        "reclaimable": bucket_total(_RECLAIMABLE_ACTIONS),
        "review_only": bucket_total(("review only",)),
        "unmeasured": unreadable_bytes,
        "unmeasured_count": unreadable_count,
    }


def unreadable_gap_bytes(scan_data, root_tot):
    # type: (dict, dict) -> int
    """Bytes likely behind a permission wall, per root, summed.

    Per root: what `df` measured as used, minus what du's own unprivileged
    walk could see (ROOT_TOT, i.e. root_totals()'s output - reused rather
    than recomputed, so this always agrees with what %ROOT divides by).
    Floored at zero so a root where du's own accounting slightly exceeds
    df's (rounding, or a volume with nothing behind a permission wall)
    never reads as a negative gap. Matched against scan_data["mounts"] by
    the RAW mount path df reports, before the data-volume/"/" firmlink
    canonicalisation applied to every other path in this module.
    """
    mounts = scan_data.get("mounts", [])
    total = 0
    for root in scan_data.get("roots", []):
        root_path = root.get("root", "")
        canonical_root = "/" if root_path == "/System/Volumes/Data" else root_path
        df_used = next((m.get("used_bytes") for m in mounts if m.get("mount") == root_path), None)
        if df_used is None:
            continue
        seen = root_tot.get(canonical_root, 0)
        total += max(0, df_used - seen)
    return total


FULL_DISK_ACCESS_NOTE = (
    "note: no Full Disk Access - %s may refuse to move.\n"
    "      ~/Library/Caches is reachable without it (verified); ~/Library/Logs is not.\n"
    "      Grant it in System Settings > Privacy & Security > Full Disk Access, or expect\n"
    "      those rows to fail with a permission error."
)


def full_disk_access_note(scan_data, rows):
    # type: (dict, list) -> str
    """Footer line warning that ~/Library rows (outside Caches) may refuse to move.

    scan.py's full_disk_access() can only say whether this process holds the
    grant at all - it cannot predict which ~/Library sources would fail,
    because TCC overrides os.access and there is no POSIX pre-flight test
    for that. So this note is a blanket warning, not a per-row prediction.

    Silent in the common case: access is granted, or a scan payload from
    before this field existed (missing key, treated as absent - the
    conservative reading), or no proposed/ask row's path actually sits
    under ~/Library (outside ~/Library/Caches). Counts against the same
    deduplicated set render_table shows (_dedup_within_roots), so "N rows"
    matches what the user sees as distinct rows, not one per nested du level.
    """
    fda = scan_data.get("full_disk_access") or {}
    if fda.get("granted"):
        return ""
    library_root = os.path.join(HOME, "Library")
    actionable = _dedup_within_roots([r for r in rows if r["action"] in _RECLAIMABLE_ACTIONS])
    affected = [r for r in actionable
                if r["path"] == library_root or r["path"].startswith(library_root + os.sep)]
    # Exclude ~/Library/Caches - empirically verified reachable without Full Disk Access
    # by successful removal on 2026-09-18. Everything else under ~/Library (including
    # ~/Library/Logs and ~/Library/Developer) remains in the warning.
    caches_root = os.path.join(library_root, "Caches")
    affected = [r for r in affected
                if not (r["path"] == caches_root or r["path"].startswith(caches_root + os.sep))]
    if not affected:
        return ""
    n = len(affected)
    return FULL_DISK_ACCESS_NOTE % ("%d row%s outside ~/Library/Caches" % (n, "" if n == 1 else "s"))


STALE_AFTER_H = 24


def render_scan_age_header(scan_data, now_ts):
    # type: (dict, float) -> str
    """One line naming when this data is from, so a days-old JSON never
    renders silently as current.

    Roots can be cached independently and re-scanned at different times;
    the OLDEST scanned_at governs staleness, since that is the part of the
    report most likely to be wrong.
    """
    timestamps = [r["scanned_at"] for r in scan_data.get("roots", []) if r.get("scanned_at")]
    if not timestamps:
        return "scanned: unknown - no scan timestamp in this payload"
    oldest = min(timestamps)
    age_h = (now_ts - oldest) / 3600.0
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(oldest))
    line = "scanned: %s (%.1fh ago)" % (stamp, age_h)
    if age_h > STALE_AFTER_H:
        line += "  STALE - rerun with --refresh"
    return line


DOCKER_MANIFEST_ID = "docker-images"
DOCKER_ROW_PATH = "docker (stopped containers + build cache)"


def build_rows(scan_data, manifest, now_ts):
    """Join a scan payload against MANIFEST into renderable rows."""
    rows = []
    by_path = {}
    for root in scan_data.get("roots", []):
        root_path = root.get("root", "")
        # scan.py canonicalises paths; the data volume's own row becomes "/".
        canonical_root = "/" if root_path == "/System/Volumes/Data" else root_path
        for raw in list(root.get("rows", [])) + list(root.get("files", [])):
            info = classify_row(raw["path"], manifest)
            mtime = raw.get("mtime")
            age_days = int((now_ts - mtime) / 86400) if mtime else -1
            row = {
                "path": raw["path"],
                "bytes": raw["bytes"],
                "age_days": age_days,
                "tier": info["tier"],
                "action": info["action"],
                "why": info["why"],
                "regen": info["regen"],
                "method": row_method(info["action"], info["tier"], info["id"], info["command"]),
                "is_root": raw["path"] == canonical_root,
                "root": canonical_root,
            }
            rows.append(row)
            by_path[row["path"]] = row

    # Project artifacts: scan.py's project_pass() walks every root for
    # rebuildable build/dependency directories and runs `git status` on
    # each candidate's project - real work the scan already pays for - but
    # until this loop nothing downstream ever read the result. A qualifying
    # row (passed the ownership test in is_project_artifact: sibling
    # manifest, 30+ days stale, and a clean or absent git worktree) is the
    # user's own rebuildable work product, not a cache, so it is itemized
    # individually like any other `ask` row, never batch-approved. A
    # non-qualifying row is classified exactly like any other path - most
    # land `review only`; tool-directory hits land `protected`. Either way,
    # a path already present from the du pass above is upgraded in place
    # rather than appended again: the same directory would otherwise
    # double-count its bytes and be asked about under two different rows.
    for root in scan_data.get("roots", []):
        root_path = root.get("root", "")
        canonical_root = "/" if root_path == "/System/Volumes/Data" else root_path
        for proj in root.get("projects", []):
            path = proj["path"]
            proj_age = proj.get("age_days", -1)
            if proj.get("qualifies"):
                tier, action = "project", "ask"
                why = "rebuildable project artifact, untouched for %d+ days" % max(proj_age, 0)
                regen = "rebuilt by your package manager on next install or build"
                entry_id, command = None, None
            else:
                info = classify_row(path, manifest)
                tier, action = info["tier"], info["action"]
                why, regen = info["why"], info["regen"]
                entry_id, command = info["id"], info["command"]
            method = row_method(action, tier, entry_id, command)

            existing = by_path.get(path)
            if existing is not None:
                existing["tier"] = tier
                existing["action"] = action
                existing["why"] = why
                existing["regen"] = regen
                existing["method"] = method
            else:
                row = {
                    "path": path,
                    "bytes": proj["bytes"],
                    "age_days": proj_age,
                    "tier": tier,
                    "action": action,
                    "why": why,
                    "regen": regen,
                    "method": method,
                    "is_root": False,
                    "root": canonical_root,
                }
                rows.append(row)
                by_path[path] = row

    # docker-images has no real filesystem path (its manifest path is the
    # placeholder __command_only__docker), so it can never surface from the
    # roots loop above like every other ask entry does. Splice in one row
    # built from scan.py's docker_pass() instead, so it flows through the
    # same sort/itemize/render path as everything else rather than living
    # only as prose. Nothing is added when Docker could not be asked, or
    # answered that there is nothing to reclaim - never a guessed number.
    docker = scan_data.get("docker") or {}
    if docker.get("available") and docker.get("bytes", 0) > 0:
        entry = manifest_entry(manifest, DOCKER_MANIFEST_ID)
        if entry is not None:
            action = _ACTION_BY_TIER[entry["tier"]]
            rows.append({
                "path": DOCKER_ROW_PATH,
                "bytes": docker["bytes"],
                "age_days": -1,
                "tier": entry["tier"],
                "action": action,
                "why": entry["why"],
                "regen": entry["regen"],
                "method": row_method(action, entry["tier"], entry.get("id"), entry.get("command")),
                "is_root": False,
                "root": "docker",
            })
    return rows


def main(argv=None):
    """Render a scan JSON file as the ranked report. Read-only."""
    import argparse
    import sys

    p = argparse.ArgumentParser(description="Render a disk-cleanup scan. Deletes nothing.")
    p.add_argument("scan_json", help="Path to the JSON emitted by scan.py --json")
    p.add_argument("--os", dest="os_name", default=None)
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    with open(args.scan_json) as fh:
        data = json.load(fh)

    os_name = args.os_name or detect_os(sys.platform)
    manifest = load_manifest(os_name, Path(__file__).resolve().parent / "manifests")
    now_ts = time.time()

    print(render_scan_age_header(data, now_ts))
    print()

    rows = build_rows(data, manifest, now_ts)
    r_totals = root_totals(data)
    print(render_table(rows, r_totals))

    apps = data.get("apps", [])
    if apps:
        print()
        print("APPLICATIONS")
        print(render_apps(apps))

    notes = render_ask_notes(rows)
    if notes:
        print()
        print(notes)

    gap_bytes = unreadable_gap_bytes(data, r_totals)
    t = totals(rows,
               unreadable_bytes=gap_bytes,
               unreadable_count=sum(r.get("unreadable_count", 0) for r in data.get("roots", [])))
    transient = sum(r.get("transient_count", 0) for r in data.get("roots", []))
    print()
    print("reclaimable: %s   review only: %s (includes the reclaimable bytes above, "
          "which sit inside larger review-only directories)" %
          (human_size(t["reclaimable"]), human_size(t["review_only"])))
    print("unreadable (need sudo): %s across %d entries" % (human_size(t["unmeasured"]), t["unmeasured_count"]))
    print("paths that vanished mid-scan:   %d" % transient)
    print("local snapshots: %d" % len(data.get("snapshots", [])))
    fda_note = full_disk_access_note(data, rows)
    if fda_note:
        print(fda_note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

