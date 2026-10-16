#!/usr/bin/env python3
"""disk-cleanup scanner - READ ONLY.

Collects sizes and emits JSON. Contains no deletion code of any kind;
tests/test_safety.py enforces that. Removal is performed by SKILL.md
steps, only after the user approves specific items.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Mounts that are never scan roots, and why.
_SKIP_MOUNTS = {
    "/": "sealed read-only system volume",
    "/System/Volumes/VM": "swap and sleepimage, OS-managed",
    "/System/Volumes/Preboot": "boot support, OS-managed",
    "/System/Volumes/Update": "OS update staging",
    "/System/Volumes/xarts": "secure element storage",
    "/System/Volumes/iSCPreboot": "secure element storage",
    "/System/Volumes/Hardware": "firmware support",
}

# Pseudo-filesystems that are never worth walking. `map` catches autofs
# lines, which also parse badly: "map auto_home" shifts every field.
_SKIP_DEVICES = {"devfs", "map", "autofs", "none"}

_SIZE_RE = re.compile(r"^([\d.]+)([KMGTPi]*)$")
_MULT = {"Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4}

# du's block-size flag and the matching multiplier. These two MUST agree:
# reading -k output as bytes under-reports every size by 1024x, and BSD du
# silently ignores GNU's -B, which is how that bug appears without an error.
_DU_BLOCK_FLAG = "-k"
_DU_BLOCK_SIZE = 1024


def _to_bytes(token):
    m = _SIZE_RE.match(token)
    if not m:
        return 0
    value, unit = m.groups()
    return int(float(value) * _MULT.get(unit, 1))


def parse_df(df_output):
    mounts = []
    for line in df_output.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 9:
            continue
        mounts.append({
            "device": parts[0],
            "mount": parts[-1],
            "used_bytes": _to_bytes(parts[2]),
        })
    return mounts


def choose_roots(mounts):
    """Pick the mounts worth walking, without double-counting firmlinks."""
    roots = []
    for m in mounts:
        if m["mount"] in _SKIP_MOUNTS:
            continue
        if m["device"] in _SKIP_DEVICES:
            continue
        roots.append(m["mount"])
    return roots


def read_mounts():
    out = subprocess.run(["df", "-h"], capture_output=True, text=True).stdout
    return parse_df(out)


CACHE_MAX_AGE_H = 24


def parse_du(du_output, block_size=None):
    """Parse du output. Sizes are in BLOCK_SIZE-byte blocks (du -k gives 1024)."""
    if block_size is None:
        block_size = _DU_BLOCK_SIZE
    rows = []
    for line in du_output.splitlines():
        if not line.strip():
            continue
        size, _, path = line.partition("\t")
        if not path:
            continue
        rows.append({"path": path, "bytes": int(size) * block_size})
    return rows


def cache_path(root, cache_dir):
    slug = root.strip("/").replace("/", "_") or "rootfs"
    return Path(cache_dir) / ("du_%s.json" % slug)


# A cached payload from an older schema is stale regardless of its age:
# returning it would make a later pass look like it found nothing.
_REQUIRED_CACHE_KEYS = ("rows", "files", "projects")


def is_stale(meta, now_ts, max_age_h=CACHE_MAX_AGE_H):
    scanned_at = meta.get("scanned_at")
    if scanned_at is None:
        return True
    if any(k not in meta for k in _REQUIRED_CACHE_KEYS):
        return True
    return (now_ts - scanned_at) > max_age_h * 3600


def _classify_stderr(stderr):
    """Split du's stderr into permission-blocked and transient failures.

    A vanished path during a 60s walk is not a blind spot; a permission
    denial is. The footer reports them differently, so do not conflate.
    "Operation not permitted" is macOS's TCC wording for the same denial
    (e.g. ~/.Trash without Full Disk Access) - the classic "Permission
    denied" text never appears for that class of block, so both count.
    """
    blocked = transient = 0
    for line in (stderr or "").splitlines():
        if not line.strip():
            continue
        if "Permission denied" in line or "Operation not permitted" in line:
            blocked += 1
        else:
            transient += 1
    return blocked, transient


_DATA_VOLUME = "/System/Volumes/Data"


def canonical_path(path):
    """Map a data-volume path to its firmlinked, user-visible form.

    macOS mounts the data volume at /System/Volumes/Data and firmlinks
    /Users and /Applications onto it. du reports paths relative to the
    mount, but the manifest, the denylist patterns and every path a user
    recognises use the firmlink form. Emitting the mount form makes
    prefix matching fail silently against both.
    """
    if path == _DATA_VOLUME:
        return "/"
    if path.startswith(_DATA_VOLUME + "/"):
        return path[len(_DATA_VOLUME):]
    return path


def _with_mtime(rows):
    """Attach each row's mtime. None when the path cannot be stat'd."""
    for row in rows:
        try:
            row["mtime"] = os.path.getmtime(row["path"])
        except OSError:
            row["mtime"] = None
    return rows


def dir_pass(root, depth=6):
    """Walk ROOT with du. -k gives 1024-byte blocks; -x stays on one filesystem.

    Default depth is 6, not 3: main()'s real roots are volume mounts like
    /System/Volumes/Data, not $HOME. From a volume root, ~/Library/Caches/*
    sits at depth 5 and ~/.cache/* at depth 4 - depth 3 could not reach
    either, so the tool could not see its own largest cleanup targets.
    du walks the entire tree regardless of -d; the flag only limits what
    prints, so this costs output size, not scan time.
    """
    started = time.time()
    proc = subprocess.run(
        ["du", "-x", _DU_BLOCK_FLAG, "-d", str(depth), root],
        capture_output=True, text=True,
    )
    blocked, transient = _classify_stderr(proc.stderr)
    rows = parse_du(proc.stdout, _DU_BLOCK_SIZE)
    for row in rows:
        row["path"] = canonical_path(row["path"])
    return {
        "rows": _with_mtime(rows),
        "unreadable_count": blocked,
        "transient_count": transient,
        "seconds": round(time.time() - started, 1),
    }


def write_cache(root, cache_dir, payload):
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    payload = dict(payload, scanned_at=time.time(), root=root)
    cache_path(root, cache_dir).write_text(json.dumps(payload))
    return payload


def read_cache(root, cache_dir):
    p = cache_path(root, cache_dir)
    if not p.exists():
        return None
    return json.loads(p.read_text())


DEFAULT_MIN_FILE_BYTES = 500 * 1024 * 1024
_SPARSE_RATIO = 2.0


def is_sparse(allocated, apparent):
    """True when a file reserves far less disk than its nominal length."""
    if allocated <= 0:
        return apparent > 0
    return (float(apparent) / allocated) >= _SPARSE_RATIO


def file_row(path, allocated, apparent):
    return {
        "path": path,
        "bytes": allocated,
        "apparent_bytes": apparent,
        "sparse": is_sparse(allocated, apparent),
    }


def file_pass(root, min_bytes=DEFAULT_MIN_FILE_BYTES):
    proc = subprocess.run(
        ["find", root, "-xdev", "-type", "f", "-size", "+%dc" % min_bytes],
        capture_output=True, text=True,
    )
    rows = []
    for path in proc.stdout.splitlines():
        try:
            st = os.stat(path)
        except OSError:
            continue
        rows.append(file_row(canonical_path(path), st.st_blocks * 512, st.st_size))
    return _with_mtime(rows)


def parse_last_used(raw):
    """Return YYYY-MM-DD, or None when the date is unknown.

    mdls prints (null) both for never-launched apps and for apps Spotlight
    has not indexed. The two are indistinguishable here, so the caller must
    render None as 'unknown' and never as 'unused'.
    """
    raw = (raw or "").strip()
    if not raw or raw == "(null)":
        return None
    return raw.split(" ")[0]


def parse_snapshots(raw):
    """Parse tmutil listlocalsnapshots output, returning snapshot names."""
    lines = [l.strip() for l in (raw or "").splitlines()[1:]]
    return [l for l in lines if l]


def app_pass(apps_dir="/Applications"):
    """Scan /Applications and return metadata about each .app bundle."""
    proc = subprocess.run(
        ["du", "-x", _DU_BLOCK_FLAG, "-d", "1", apps_dir],
        capture_output=True, text=True,
    )
    rows = []
    for row in parse_du(proc.stdout, _DU_BLOCK_SIZE):
        if row["path"] == apps_dir or not row["path"].endswith(".app"):
            continue
        mdls = subprocess.run(
            ["mdls", "-name", "kMDItemLastUsedDate", "-raw", row["path"]],
            capture_output=True, text=True,
        ).stdout
        rows.append({
            "path": canonical_path(row["path"]),
            "bytes": row["bytes"],
            "last_used": parse_last_used(mdls),
        })
    return _with_mtime(rows)


def read_snapshots():
    """Fetch local Time Machine snapshot names."""
    proc = subprocess.run(
        ["tmutil", "listlocalsnapshots", "/"], capture_output=True, text=True
    )
    return parse_snapshots(proc.stdout)


# `docker system df` reports sizes in decimal (1000-based) units, unlike
# du's binary Ki/Mi/Gi. "1.1MB (91%)", "8.2GB", "0B" - leading size, unit
# glued on with no space, an optional " (NN%)" that a whitespace split
# already isolates into its own token.
_DOCKER_SIZE_RE = re.compile(r"^([\d.]+)\s*(kB|MB|GB|TB|B)")
_DOCKER_MULT = {"B": 1, "kB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3, "TB": 1000 ** 4}

# Row-type labels are one or two words ("Containers" vs "Build Cache");
# two-word types are checked first so a two-word row is never mistaken
# for a one-word type plus a stray column.
_DOCKER_TWO_WORD_TYPES = {("Local", "Volumes"), ("Build", "Cache")}
_DOCKER_ONE_WORD_TYPES = {"Images", "Containers"}

# What a flagless prune actually reclaims: stopped containers and the
# build cache. NOT Images - that RECLAIMABLE figure counts unused but
# still-TAGGED images, which a bare prune leaves alone (it would need
# `-a`). NOT Local Volumes - untouched without a separate --volumes flag.
# Counting either would promise the user space they will not get back.
_DOCKER_RECLAIM_TYPES = {"Containers", "Build Cache"}


def _docker_size_bytes(token):
    """Bytes in one `docker system df` size token, e.g. '1.1MB'."""
    m = _DOCKER_SIZE_RE.match((token or "").strip())
    if not m:
        return 0
    value, unit = m.groups()
    try:
        return int(float(value) * _DOCKER_MULT[unit])
    except ValueError:
        return 0


def parse_docker_df(text):
    """Bytes a bare `docker system prune` would reclaim.

    Only Containers and Build Cache count. The Images RECLAIMABLE column
    includes unused TAGGED images, which bare prune does not remove, and
    Local Volumes needs an explicit --volumes flag. Counting either would
    promise the user more space than they will actually get back.
    """
    total = 0
    for line in (text or "").splitlines():
        words = line.split()
        if len(words) < 2:
            continue
        two = tuple(words[:2])
        if two in _DOCKER_TWO_WORD_TYPES:
            row_type, rest = " ".join(two), words[2:]
        elif words[0] in _DOCKER_ONE_WORD_TYPES:
            row_type, rest = words[0], words[1:]
        else:
            continue
        if row_type not in _DOCKER_RECLAIM_TYPES or len(rest) < 4:
            continue
        # rest = [TOTAL, ACTIVE, SIZE, RECLAIMABLE, (PERCENT)?]
        total += _docker_size_bytes(rest[3])
    return total


def docker_pass():
    """What Docker's cleanup command would reclaim - never guessed.

    Runs only `docker system df` (read-only; see parse_docker_df's own
    docstring for why Images and Local Volumes are excluded from the
    total). Returns available: False, with a bytes of 0, whenever that
    cannot be asked - docker missing from PATH, the call failing, or the
    daemon not running (routine: Docker Desktop is frequently closed) -
    rather than ever reporting a number that was not actually measured.
    """
    if shutil.which("docker") is None:
        return {"available": False, "reason": "docker not found on PATH", "bytes": 0}
    try:
        proc = subprocess.run(
            ["docker", "system", "df"], capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "reason": str(exc), "bytes": 0}
    if proc.returncode != 0:
        lines = [l.strip() for l in (proc.stderr or "").splitlines() if l.strip()]
        reason = lines[0] if lines else "docker system df exited %d" % proc.returncode
        return {"available": False, "reason": reason, "bytes": 0}
    return {"available": True, "reason": "", "bytes": parse_docker_df(proc.stdout)}


# Two ordinary-looking directories that are TCC-gated on a stock macOS
# install: Safari's support data and the user's own Trash. Listing either
# one needs Full Disk Access; listing neither is possible without it.
FULL_DISK_ACCESS_PROBES = ("~/Library/Safari", "~/.Trash")


def full_disk_access():
    """Whether this process can read TCC-gated locations.

    Probes by listing two protected directories. No Full Disk Access does
    not mean removal will fail - trash works for ordinary paths - but it
    does mean some ~/Library sources will refuse to move, and there is no
    POSIX test that predicts which, because TCC overrides os.access.
    """
    probed = []
    for rel in FULL_DISK_ACCESS_PROBES:
        path = os.path.expanduser(rel)
        try:
            os.listdir(path)
            probed.append({"path": path, "readable": True})
        except OSError as exc:
            probed.append({"path": path, "readable": False, "error": str(exc)})
    return {"granted": all(p["readable"] for p in probed), "probed": probed}


def parse_args(argv):
    """Parse command-line arguments."""
    p = argparse.ArgumentParser(description="Scan storage. Read only; deletes nothing.")
    p.add_argument("--root", action="append", default=None,
                   help="Root to scan; repeatable. Defaults to discovered volumes.")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    p.add_argument("--refresh", action="store_true", help="Ignore cache and re-walk.")
    p.add_argument("--depth", type=int, default=6)
    p.add_argument("--min-file-size", type=int, default=DEFAULT_MIN_FILE_BYTES)
    p.add_argument("--cache-dir", default=None)
    return p.parse_args(argv)


def main(argv=None):
    """Scan storage and emit JSON report."""
    args = parse_args(argv if argv is not None else sys.argv[1:])
    mounts = read_mounts()
    roots = args.root or choose_roots(mounts)
    cache_dir = Path(args.cache_dir) if args.cache_dir else Path.home() / ".cache" / "disk-cleanup"
    now_ts = time.time()

    result = {"roots": [], "apps": app_pass(), "snapshots": read_snapshots(),
              "mounts": mounts, "docker": docker_pass(),
              "full_disk_access": full_disk_access()}
    for root in roots:
        cached = None if args.refresh else read_cache(root, cache_dir)
        if cached and not is_stale(cached, now_ts):
            payload = cached
        else:
            payload = dir_pass(root, args.depth)
            payload["files"] = file_pass(root, args.min_file_size)
            payload["projects"] = project_pass(root, now_ts)
            payload = write_cache(root, cache_dir, payload)
        result["roots"].append(payload)

    json.dump(result, sys.stdout, indent=2)
    return 0


# Artifact directory name -> sibling files that prove it belongs to a real project.
ARTIFACT_MANIFESTS = {
    "node_modules": ["package.json"],
    "target": ["Cargo.toml"],
    ".venv": ["pyproject.toml", "requirements.txt", "setup.py"],
    "venv": ["pyproject.toml", "requirements.txt", "setup.py"],
}


def _git(project_dir, *args):
    return subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-C", project_dir] + list(args),
        capture_output=True, text=True,
    )


def git_dirty(project_dir):
    """True when the worktree has changes, False when clean or not a repo,
    None when git could not answer (treated as disqualifying downstream).

    The repo is untrusted (an extracted archive can carry .git/config), so this
    avoids `git status`: it runs the repo's fsmonitor hook and pipes files
    through its clean filters. Plumbing that never re-hashes the worktree is
    used instead; a stat-only change therefore reads as dirty, the safe side."""
    try:
        probe = _git(project_dir, "rev-parse", "--git-dir")
        if probe.returncode != 0:
            # "not a git repository" is a definite answer: nothing to lose.
            if "not a git repository" in probe.stderr.lower():
                return False
            return None
        has_head = _git(project_dir, "rev-parse", "--verify", "--quiet", "HEAD").returncode == 0
        checks = [
            ("diff-files", "--name-only"),
            ("ls-files", "--others", "--exclude-standard"),
            ("diff-index", "--cached", "--name-only", "HEAD") if has_head else ("ls-files",),
        ]
        for args in checks:
            proc = _git(project_dir, *args)
            if proc.returncode != 0:
                return None
            if proc.stdout.strip():
                return True
    except OSError:
        return None
    return False


def project_facts(artifact_path, now_ts):
    """Gather facts about a project artifact for the classifier."""
    project_dir = os.path.dirname(artifact_path)
    name = os.path.basename(artifact_path)
    manifests = ARTIFACT_MANIFESTS.get(name, [])

    found = [m for m in manifests if os.path.exists(os.path.join(project_dir, m))]

    # Age is the project's, not the artifact's: a cache directory's own mtime
    # changes on every read, which would make a dead project look active.
    stamps = [os.path.getmtime(project_dir)]
    for m in found:
        stamps.append(os.path.getmtime(os.path.join(project_dir, m)))
    age_days = int((now_ts - max(stamps)) / 86400)

    return {
        "has_sibling_manifest": bool(found),
        "age_days": age_days,
        "git_dirty": git_dirty(project_dir),
    }


def project_pass(root, now_ts=None):
    """Find rebuildable project artifacts. Returns rows, proposes nothing."""
    import classify

    now_ts = now_ts if now_ts is not None else time.time()
    names = []
    for name in ARTIFACT_MANIFESTS:
        names += ["-name", name, "-o"]
    names = names[:-1]

    proc = subprocess.run(
        ["find", root, "-xdev", "-type", "d", "("] + names + [")", "-prune", "-print"],
        capture_output=True, text=True,
    )
    rows = []
    for raw_path in proc.stdout.splitlines():
        path = canonical_path(raw_path)
        facts = project_facts(path, now_ts)
        try:
            size = 0
            for dirpath, _, filenames in os.walk(path):
                for name in filenames:
                    f = os.path.join(dirpath, name)
                    try:
                        size += os.stat(f).st_blocks * 512
                    except OSError:
                        continue
        except OSError:
            continue
        rows.append({
            "path": path,
            "bytes": size,
            "age_days": facts["age_days"],
            # Canonicalize BEFORE this call: is_project_artifact -> is_protected
            # matches denylist patterns anchored on the firmlink form
            # (^/(Users|home)/...). Against the raw /System/Volumes/Data/Users/...
            # path those anchors silently fail to match, reopening the
            # installed-tooling hole closed in the prior denylist fix.
            "qualifies": classify.is_project_artifact(path, facts),
        })
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
