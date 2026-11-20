#!/usr/bin/env python3
"""disk-cleanup scanner - READ ONLY.

Collects sizes and emits JSON. Contains no deletion code of any kind;
tests/test_safety.py enforces that. Removal is performed by SKILL.md
steps, only after the user approves specific items.
"""

import argparse
import codecs
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")

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

# BSD du by absolute path. A Homebrew coreutils `gnubin` ahead on PATH makes
# bare `du` GNU's, which aborts the whole walk of /System/Volumes/Data with
# "fts_read failed: .../usr/local: No such file or directory" about 1s in.
_DU = "/usr/bin/du"


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
    if IS_WINDOWS:
        return win_mounts()
    # Absolute path on purpose: a Homebrew coreutils `gnubin` ahead on PATH
    # makes bare `df` GNU's, whose 6-column, "G"-suffixed lines parse_df
    # drops (it needs BSD's 9 columns and "Gi" units) - zero roots, empty scan.
    out = subprocess.run(["/bin/df", "-h"], capture_output=True, text=True).stdout
    return parse_df(out)


# GetDriveTypeW's answer for a local fixed disk. Removable, network, CD-ROM
# and RAM drives are never scan roots: a USB stick or a mapped share is not
# what is filling the machine, and walking a network drive can take hours.
_DRIVE_FIXED = 3


def win_mounts():
    """Fixed drives as mount rows, in the same shape parse_df produces.

    Windows has no df. Drive letters come from GetLogicalDrives, the type
    from GetDriveTypeW, and used bytes from shutil.disk_usage (total minus
    free, which is what Explorer's bar shows). The mount is "C:/" - forward
    slashes, like every other path this module emits on Windows.
    """
    import ctypes

    k32 = ctypes.windll.kernel32
    mask = k32.GetLogicalDrives()
    mounts = []
    for i in range(26):
        if not mask & (1 << i):
            continue
        letter = chr(ord("A") + i)
        if k32.GetDriveTypeW("%s:\\" % letter) != _DRIVE_FIXED:
            continue
        mount = "%s:/" % letter
        try:
            used = shutil.disk_usage(mount).used
        except OSError:
            continue
        mounts.append({"device": "%s:" % letter, "mount": mount, "used_bytes": used})
    return mounts


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
    # ":" is illegal in a Windows file name, so "C:/" must slug to "C", not "C:".
    slug = root.strip("/").replace("/", "_").replace(":", "") or "rootfs"
    return Path(cache_dir) / ("du_%s.json" % slug)


# A cached payload from an older schema is stale regardless of its age:
# returning it would make a later pass look like it found nothing.
_REQUIRED_CACHE_KEYS = ("rows", "files", "projects")
# Bumped when a pass adds a field nested inside rows (which the key check
# above cannot see). 2: project rows carry pnpm_store / pnpm_store_shared.
CACHE_SCHEMA = 2


def is_stale(meta, now_ts, max_age_h=CACHE_MAX_AGE_H):
    scanned_at = meta.get("scanned_at")
    if scanned_at is None:
        return True
    if any(k not in meta for k in _REQUIRED_CACHE_KEYS):
        return True
    if meta.get("schema", 1) < CACHE_SCHEMA:
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
    if IS_WINDOWS:
        return win_path(path)
    return path


def win_path(path):
    """A Windows path in the forward-slash form classify.py matches against.

    classify.py's denylist and manifest are written with "/" separators, so
    every Windows path is emitted as "C:/Users/..." - never "C:\\Users\\...".
    A bare drive ("C:") becomes its root ("C:/"), the form win_mounts uses.
    Only called on Windows: a backslash is a legal file-name character on
    macOS and Linux, so rewriting it there would corrupt real paths.
    """
    path = path.replace("\\", "/")
    if re.match(r"^[A-Za-z]:$", path):
        path += "/"
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
        [_DU, "-x", _DU_BLOCK_FLAG, "-d", str(depth), root],
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


# FILE_ATTRIBUTE_* bits, as found on os.stat_result.st_file_attributes.
_FA_DIRECTORY = 0x10
_FA_SPARSE = 0x200
_FA_REPARSE_POINT = 0x400
_FA_COMPRESSED = 0x800
_FA_OFFLINE = 0x1000
_FA_RECALL_ON_OPEN = 0x40000
_FA_RECALL_ON_DATA_ACCESS = 0x400000

# Files whose st_size is not what they occupy on disk: NTFS-compressed,
# sparse, WOF/CompactOS-compressed (stored as a reparse point), and cloud
# placeholders (OneDrive, Google Drive) whose content is not downloaded.
# Only these pay for a GetCompressedFileSizeW call; every other file's
# st_size is used as is, which keeps the walk at one listing per directory.
_FA_ALLOC_DIFFERS = (_FA_SPARSE | _FA_REPARSE_POINT | _FA_COMPRESSED | _FA_OFFLINE
                     | _FA_RECALL_ON_OPEN | _FA_RECALL_ON_DATA_ACCESS)

# IsReparseTagNameSurrogate: the reparse point names another location -
# a symlink, a junction or a volume mount point. Never followed, the same
# rule as du -x not crossing into what a link points at: the target is
# counted where it actually lives, once.
_REPARSE_NAME_SURROGATE = 0x20000000

# ERROR_ACCESS_DENIED and ERROR_CANT_ACCESS_FILE: a permission wall, not a
# path that vanished. The second is what WindowsApps' app links raise.
_BLOCKED_WINERRORS = {5, 1920}


# Past MAX_PATH (260), Win32 file calls fail with "path not found" unless
# the path carries the extended-length prefix - and without this, every
# deep path (Claude's own MCP logs reach 285 chars here) silently drops out
# of the walk and is miscounted as having vanished mid-scan. The prefix form
# needs backslashes; it is used only for the call, never emitted.
_LONG_PATH_AT = 240


def _os_path(path):
    if len(path) < _LONG_PATH_AT or path.startswith("\\\\"):
        return path
    return "\\\\?\\" + path.replace("/", "\\")


def _is_blocked(exc):
    return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in _BLOCKED_WINERRORS


_get_compressed_size = None


def _compressed_file_size(path):
    """Bytes PATH occupies on disk, per GetCompressedFileSizeW; None on failure."""
    global _get_compressed_size
    import ctypes
    from ctypes import wintypes

    if _get_compressed_size is None:
        fn = ctypes.WinDLL("kernel32", use_last_error=True).GetCompressedFileSizeW
        fn.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        fn.restype = wintypes.DWORD
        _get_compressed_size = fn
    high = wintypes.DWORD(0)
    ctypes.set_last_error(0)
    low = _get_compressed_size(_os_path(path), ctypes.byref(high))
    if low == 0xFFFFFFFF and ctypes.get_last_error():
        return None
    return (high.value << 32) | low


def win_cluster_size(root):
    """The volume's allocation unit in bytes; 4096 (the NTFS default) if unknown."""
    import ctypes
    from ctypes import wintypes

    spc, bps, free, total = (wintypes.DWORD() for _ in range(4))
    drive = root[:3].replace("/", "\\")
    ok = ctypes.windll.kernel32.GetDiskFreeSpaceW(
        drive, ctypes.byref(spc), ctypes.byref(bps), ctypes.byref(free), ctypes.byref(total))
    size = spc.value * bps.value if ok else 0
    return size or 4096


def win_allocated_size(path, st, measure=None, cluster=4096):
    """On-disk bytes for one file, rounded up to whole clusters like du.

    st_size is used unless the attributes say the file occupies something
    else (compressed, sparse, placeholder), in which case MEASURE - the
    allocated-size probe, GetCompressedFileSizeW by default, injectable so
    this is testable on any OS - answers instead. Rounding matters: a tree
    of tiny files (node_modules) occupies far more than the sum of its
    lengths - 0.73G more across this machine's repos, measured against du.
    """
    size = st.st_size
    if st.st_file_attributes & _FA_ALLOC_DIFFERS:
        measured = (measure or _compressed_file_size)(path)
        if measured is not None:
            size = measured
    if size <= 0:
        return 0
    return -(-size // cluster) * cluster


def win_walk(root, depth=6, min_file_bytes=None):
    """One read-only pass over ROOT that does the work of du, find and the
    project search together. Windows has none of those tools (Git Bash's
    du exists, but cannot be relied on, and `find` resolves to find.exe,
    a text-search tool), so this walks with os.scandir.

    Returns the same keys dir_pass returns, plus "files" (file_pass's rows)
    and "artifacts" (every node_modules/target/.venv/venv directory with its
    size, outermost only, the way find -prune reports them).

    Sizes match du's semantics, not Explorer's "Size":
      - a hard-linked file is counted once per walk, at the first path seen
        (C:/Windows alone carries ~9.5G of WinSxS links that a naive sum
        counts twice; measured 2026-11-16);
      - compressed, sparse and cloud-placeholder files count what they
        occupy on disk (win_allocated_size);
      - symlinks, junctions and mount points are never followed.
    Directory rows are emitted down to DEPTH levels below ROOT; deeper
    directories are still walked and summed into their ancestors.
    """
    if min_file_bytes is None:
        min_file_bytes = DEFAULT_MIN_FILE_BYTES
    started = time.time()
    root = win_path(root)
    if not re.match(r"^[A-Za-z]:/$", root):
        root = root.rstrip("/")
    try:
        root_mtime = os.path.getmtime(root)
    except OSError:
        root_mtime = None
    cluster = win_cluster_size(root)

    # Breadth-first, so every parent's index is lower than its children's:
    # one reverse sweep afterwards rolls each directory's size into its parent.
    paths, levels, parents, sizes, mtimes = [root], [0], [-1], [0], [root_mtime]
    in_artifact = [False]      # this directory is an artifact or inside one
    is_artifact_dir = [False]  # this directory is itself an outermost artifact
    artifact_idx = []
    files = []
    seen_links = set()
    blocked = transient = 0

    i = 0
    while i < len(paths):
        d, lvl = paths[i], levels[i]
        base = d.rstrip("/") + "/"
        try:
            with os.scandir(_os_path(d)) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError as exc:
                        if _is_blocked(exc):
                            blocked += 1
                        else:
                            transient += 1
                        continue
                    attrs = st.st_file_attributes
                    if attrs & _FA_REPARSE_POINT and st.st_reparse_tag & _REPARSE_NAME_SURROGATE:
                        continue
                    path = base + e.name
                    if attrs & _FA_DIRECTORY:
                        is_artifact = not in_artifact[i] and e.name in ARTIFACT_MANIFESTS
                        paths.append(path)
                        levels.append(lvl + 1)
                        parents.append(i)
                        sizes.append(0)
                        mtimes.append(st.st_mtime)
                        in_artifact.append(in_artifact[i] or is_artifact)
                        is_artifact_dir.append(is_artifact)
                        if is_artifact:
                            artifact_idx.append(len(paths) - 1)
                        continue
                    # DirEntry.stat() leaves st_nlink at 0 on Windows; only a
                    # full lstat says whether this file has other names.
                    try:
                        nlink_st = os.lstat(_os_path(path))
                    except OSError:
                        nlink_st = None
                    if nlink_st is not None and nlink_st.st_nlink > 1:
                        key = (nlink_st.st_dev, nlink_st.st_ino)
                        if key in seen_links:
                            continue
                        seen_links.add(key)
                    size = win_allocated_size(path, st, cluster=cluster)
                    sizes[i] += size
                    if st.st_size >= min_file_bytes:
                        row = file_row(path, size, st.st_size)
                        row["mtime"] = st.st_mtime
                        files.append(row)
        except OSError as exc:
            if _is_blocked(exc):
                blocked += 1
            else:
                transient += 1
        # Free the path string once it can no longer be emitted: a large
        # drive holds hundreds of thousands of directories below DEPTH.
        if lvl > depth and not is_artifact_dir[i]:
            paths[i] = None
        i += 1

    for j in range(len(paths) - 1, 0, -1):
        sizes[parents[j]] += sizes[j]

    rows = [{"path": paths[j], "bytes": sizes[j], "mtime": mtimes[j]}
            for j in range(len(paths)) if levels[j] <= depth]
    artifacts = [{"path": paths[j], "bytes": sizes[j]} for j in artifact_idx]
    return {
        "rows": rows,
        "files": files,
        "artifacts": artifacts,
        "unreadable_count": blocked,
        "transient_count": transient,
        "seconds": round(time.time() - started, 1),
    }


def write_cache(root, cache_dir, payload):
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    payload = dict(payload, scanned_at=time.time(), root=root, schema=CACHE_SCHEMA)
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
        [_DU, "-x", _DU_BLOCK_FLAG, "-d", "1", apps_dir],
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


_UNINSTALL_KEYS = (
    ("HKEY_LOCAL_MACHINE", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKEY_LOCAL_MACHINE", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKEY_CURRENT_USER", r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
)
_USERASSIST_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\UserAssist"

# KNOWNFOLDERID prefixes UserAssist writes in place of a real directory.
# Values are environment variables, read at call time; a missing one leaves
# the GUID in place, so that launch simply matches no app.
_KNOWN_FOLDER_ENV = {
    "{6D809377-6AF0-444B-8957-A3773F02200E}": ("ProgramW6432", ""),
    "{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}": ("ProgramFiles(x86)", ""),
    "{F38BF404-1D43-42F2-9305-67DE0B28FC23}": ("windir", ""),
    "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}": ("windir", "/System32"),
    "{D65231B0-B2F1-4857-A4CE-A8E7C6EA7D27}": ("windir", "/SysWOW64"),
}

# Seconds between the FILETIME epoch (1601-01-01) and the Unix epoch.
_FILETIME_UNIX_OFFSET = 11644473600


def parse_userassist_name(raw, env):
    """The program path behind one ROT13-encoded UserAssist value name.

    Returns a forward-slash path, with a leading KNOWNFOLDERID swapped for
    the directory ENV names - or the decoded name unchanged when it is not
    a path at all (UserAssist also records shortcuts and app IDs).
    """
    name = codecs.decode(raw, "rot_13")
    for guid, (var, suffix) in _KNOWN_FOLDER_ENV.items():
        if name.upper().startswith(guid) and env.get(var):
            name = env[var] + suffix + name[len(guid):]
            break
    return name.replace("\\", "/")


def parse_userassist_data(data):
    """Unix time of the last launch in one UserAssist value, or None.

    Windows 7+ writes a 72-byte record with the last-run FILETIME at offset
    60. Anything shorter, or a zero FILETIME, means no recorded launch.
    """
    if not data or len(data) < 68:
        return None
    filetime = struct.unpack_from("<Q", data, 60)[0]
    if filetime == 0:
        return None
    return filetime / 1e7 - _FILETIME_UNIX_OFFSET


def read_userassist(env=None):
    """{program path (casefolded): last launch unix time} from UserAssist."""
    import winreg

    env = os.environ if env is None else env
    launches = {}
    try:
        top = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _USERASSIST_KEY)
    except OSError:
        return launches
    with top:
        i = 0
        while True:
            try:
                guid = winreg.EnumKey(top, i)
            except OSError:
                break
            i += 1
            try:
                count = winreg.OpenKey(top, guid + r"\Count")
            except OSError:
                continue
            with count:
                j = 0
                while True:
                    try:
                        name, data, _ = winreg.EnumValue(count, j)
                    except OSError:
                        break
                    j += 1
                    ts = parse_userassist_data(data if isinstance(data, bytes) else None)
                    if ts is None:
                        continue
                    key = parse_userassist_name(name, env).casefold()
                    launches[key] = max(ts, launches.get(key, 0))
    return launches


def read_uninstall_entries():
    """Every Uninstall registry entry as a plain dict. Read only."""
    import winreg

    fields = ("DisplayName", "InstallLocation", "DisplayIcon", "EstimatedSize",
              "SystemComponent", "ParentKeyName", "ReleaseType")
    entries = []
    for hive_name, path in _UNINSTALL_KEYS:
        hive = getattr(winreg, hive_name)
        try:
            top = winreg.OpenKey(hive, path)
        except OSError:
            continue
        with top:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(top, i)
                except OSError:
                    break
                i += 1
                try:
                    k = winreg.OpenKey(top, sub)
                except OSError:
                    continue
                with k:
                    entry = {}
                    for f in fields:
                        try:
                            entry[f] = winreg.QueryValueEx(k, f)[0]
                        except OSError:
                            entry[f] = None
                entries.append(entry)
    return entries


def _install_dir(entry):
    """An app's install directory: InstallLocation, else DisplayIcon's folder.

    Many installers (Brave, Chrome, Zoom) leave InstallLocation empty but
    point DisplayIcon at the program itself, e.g. "C:/.../brave.exe,0"
    (written with backslashes in the registry).
    """
    location = (entry.get("InstallLocation") or "").strip().strip('"')
    if not location:
        icon = (entry.get("DisplayIcon") or "").strip().strip('"')
        icon = re.sub(r",\s*-?\d+$", "", icon).strip().strip('"')
        if icon.lower().endswith(".exe") and re.match(r"^[A-Za-z]:[\\/]", icon):
            location = icon.replace("\\", "/").rsplit("/", 1)[0]
    return win_path(location).rstrip("/") if location else ""


def _shortcut_launches(launches):
    """{shortcut name: last launch} for UserAssist's .lnk records.

    Explorer records most Start-menu and taskbar launches by shortcut
    ("taskbar/visual studio code.lnk"), not by program path.
    """
    out = {}
    for path, ts in launches.items():
        if path.endswith(".lnk"):
            stem = path.rsplit("/", 1)[-1][:-4].strip()
            out[stem] = max(ts, out.get(stem, 0))
    return out


def win_app_rows(entries, dir_sizes, launches):
    """Installed applications, shaped like app_pass's rows plus a "name".

    ENTRIES is read_uninstall_entries()'s output, DIR_SIZES maps casefolded
    directory paths to measured bytes (the scan's own rows), and LAUNCHES is
    read_userassist()'s output. Pure, so it is testable on any OS.

    Size is the install directory as this scan measured it, falling back to
    the installer's own EstimatedSize, which is often stale or missing; an
    app with neither is left out. last_used is the newest Explorer-recorded
    launch of any program inside the install directory, or of a shortcut
    whose name appears as whole words in the app's name ("visual studio
    code.lnk" for "Microsoft Visual Studio Code (User)"). A name match can
    only make a date newer, never older, so a loose match errs toward
    "recently used" - the safe side for a report that informs uninstalls.
    None when nothing matches, which must render as "unknown", never "unused":
    UserAssist only sees launches from the Start menu, taskbar or Explorer,
    so a game started by Steam or a tool started from a terminal has none.
    """
    shortcuts = _shortcut_launches(launches)
    by_name = {}
    for e in entries:
        name = (e.get("DisplayName") or "").strip()
        if not name or e.get("SystemComponent") == 1:
            continue
        if e.get("ParentKeyName") or e.get("ReleaseType"):
            continue  # an update or hotfix for another entry, not an app
        location = _install_dir(e)
        measured = dir_sizes.get(location.casefold()) if location else None
        estimated = e.get("EstimatedSize")
        if measured:
            size, source = measured, "measured"
        elif isinstance(estimated, int) and estimated > 0:
            size, source = estimated * 1024, "estimated"
        else:
            continue
        stamps = []
        if location:
            prefix = location.casefold() + "/"
            stamps += [ts for p, ts in launches.items() if p.startswith(prefix)]
        name_cf = name.casefold()
        stamps += [ts for stem, ts in shortcuts.items()
                   if len(stem) >= 4
                   and re.search(r"(?<!\w)" + re.escape(stem) + r"(?!\w)", name_cf)]
        last = time.strftime("%Y-%m-%d", time.localtime(max(stamps))) if stamps else None
        row = {"name": name, "path": location or name, "bytes": size,
               "size_source": source, "last_used": last}
        prior = by_name.get(name.casefold())
        if prior is None or row["bytes"] > prior["bytes"]:
            by_name[name.casefold()] = row
    return list(by_name.values())


def win_app_pass(roots_payloads):
    """Installed applications on Windows. Read only: registry reads only."""
    dir_sizes = {}
    for payload in roots_payloads:
        for row in payload.get("rows", []):
            dir_sizes[row["path"].casefold()] = row["bytes"]
    return win_app_rows(read_uninstall_entries(), dir_sizes, read_userassist())


WINDOWS_SNAPSHOT_NOTE = ("restore points need an administrator - run "
                         "`vssadmin list shadowstorage` in an admin terminal")


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
    # On Windows, run the path which() resolved: CreateProcess resolves a
    # bare "docker" to docker.exe only, skipping the PATHEXT lookup which()
    # just did (docker.cmd, docker.bat). Elsewhere, unchanged: the bare name.
    docker = shutil.which("docker")
    if docker is None:
        return {"available": False, "reason": "docker not found on PATH", "bytes": 0}
    if not IS_WINDOWS:
        docker = "docker"
    try:
        proc = subprocess.run(
            [docker, "system", "df"], capture_output=True, text=True, timeout=15,
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

    if IS_WINDOWS:
        roots = [win_path(r) for r in roots]
        result = {"roots": [], "apps": [], "snapshots": None,
                  "snapshots_note": WINDOWS_SNAPSHOT_NOTE}
    else:
        result = {"roots": [], "apps": app_pass(), "snapshots": read_snapshots()}
    # Full Disk Access is a macOS grant. Elsewhere the answer is "not
    # applicable", never a granted/denied guess from probing macOS paths.
    result["mounts"] = mounts
    # Never cached: a symlink or config can change between runs, and the
    # lookup is a few dozen lstat calls.
    result["relocations"] = [] if IS_WINDOWS else relocations()
    result["docker"] = docker_pass()
    result["full_disk_access"] = (full_disk_access() if sys.platform == "darwin"
                                  else {"applicable": False, "granted": None, "probed": []})
    for root in roots:
        cached = None if args.refresh else read_cache(root, cache_dir)
        if cached and not is_stale(cached, now_ts):
            payload = cached
        elif IS_WINDOWS:
            payload = win_walk(root, args.depth, args.min_file_size)
            payload["projects"] = project_pass(root, now_ts, found=payload.pop("artifacts"))
            payload = write_cache(root, cache_dir, payload)
        else:
            payload = dir_pass(root, args.depth)
            payload["files"] = file_pass(root, args.min_file_size)
            payload["projects"] = project_pass(root, now_ts)
            payload = write_cache(root, cache_dir, payload)
        result["roots"].append(payload)

    if IS_WINDOWS:
        # Needs every root's measured rows: an app's size is its install
        # directory as this scan measured it, on whichever drive it lives.
        result["apps"] = win_app_pass(result["roots"])

    json.dump(result, sys.stdout, indent=2)
    return 0


# Tool caches a user can move off the home directory by configuration
# instead of a symlink. ALIAS is the manifest's home-form path; the moved
# location comes from ENV first, then KEY in FILE.
CONFIGURED_CACHES = [
    {"alias": "~/.npm", "env": "npm_config_cache", "file": "~/.npmrc", "key": "cache"},
    {"alias": "~/.cache/uv", "env": "UV_CACHE_DIR", "file": "~/.config/uv/uv.toml", "key": "cache-dir"},
]
# Where a moved cache or tool directory leaves its symlink behind:
# ~/.npm, ~/.cache/whisper, ~/.devin/extensions, ~/Library/Caches/<app>.
RELOCATION_GLOBS = ("*", ".*", ".*/*", "Library/Caches/*")


def _config_value(path, key):
    """KEY's value in an ini- or TOML-style FILE (`key = value`), or None."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read(65536)
    except OSError:
        return None
    m = re.search(r"""^\s*%s\s*=\s*["']?([^"'#\n]+?)["']?\s*$""" % re.escape(key), text, re.M)
    return m.group(1).strip() if m else None


def _inside(path, parent):
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def relocations(home=None, env=None):
    """Home-directory paths whose data lives somewhere else.

    Returns [{"target": real location, "alias": home-form path}], so the
    classifier can match a moved cache (~/.npm -> /Volumes/X/caches/npm)
    against the manifest and denylist, which name only home-form paths.
    Read only: lstat, realpath and two small config reads.
    """
    import glob

    home = os.path.realpath(home or os.path.expanduser("~"))
    env = os.environ if env is None else env
    found = {}

    def add(target, alias):
        target = os.path.realpath(target)
        if not os.path.isdir(target) or os.path.ismount(target):
            return
        # A link within home, or one to an ancestor of home, moves nothing.
        if _inside(target, home) or _inside(home, target):
            return
        found.setdefault(canonical_path(target), alias)

    for pattern in RELOCATION_GLOBS:
        for link in glob.glob(os.path.join(home, pattern)):
            if os.path.islink(link):
                add(link, canonical_path(link))
    for c in CONFIGURED_CACHES:
        value = env.get(c["env"]) or _config_value(
            os.path.join(home, c["file"][2:]), c["key"])
        if value:
            value = os.path.expanduser(value)
            if os.path.isabs(value):
                add(value, canonical_path(os.path.join(home, c["alias"][2:])))
    return [{"target": t, "alias": a} for t, a in sorted(found.items())]


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


_STORE_DIR_RE = re.compile(r"""^\s*["']?storeDir["']?\s*:\s*["']?([^"'\n,]+?)["']?\s*,?\s*$""", re.M)


def pnpm_facts(artifact_path):
    """Whether pnpm built this node_modules from a store on the same volume.

    pnpm fills node_modules with hard links or copy-on-write clones of the
    files in its store (`packageImportMethod: auto`), and records the store
    in node_modules/.modules.yaml. The walk counts every linked or cloned
    file at full size, but removing the directory frees almost none of it
    while the store still holds the same blocks. In one measured case, ten
    pnpm worktrees the walk put at 29G freed 2G. Only a store on the same
    volume shares blocks; a store elsewhere means real copies.
    """
    meta = os.path.join(artifact_path, ".modules.yaml")
    try:
        # storeDir sits near the end, after the hoisted-dependency map, which
        # runs past 160K in a large monorepo. Capped so a hostile file cannot
        # make the scan read gigabytes.
        with open(meta, encoding="utf-8", errors="replace") as fh:
            text = fh.read(16 * 1024 * 1024)
    except OSError:
        return {"pnpm_store": None, "pnpm_store_shared": False}
    m = _STORE_DIR_RE.search(text)
    if not m:
        return {"pnpm_store": None, "pnpm_store_shared": False}
    store = m.group(1).strip()
    if not os.path.isabs(store):
        store = os.path.normpath(os.path.join(os.path.dirname(artifact_path), store))
    try:
        shared = os.stat(store).st_dev == os.stat(artifact_path).st_dev
    except OSError:
        shared = False
    return {"pnpm_store": store, "pnpm_store_shared": shared}


def project_pass(root, now_ts=None, found=None):
    """Find rebuildable project artifacts. Returns rows, proposes nothing.

    FOUND, when given, is win_walk's "artifacts" list - paths and sizes the
    walk already measured - and replaces the find + size walk below, which
    has no Windows equivalent.
    """
    import classify

    now_ts = now_ts if now_ts is not None else time.time()
    if found is not None:
        rows = []
        for art in found:
            try:
                facts = project_facts(art["path"], now_ts)
            except OSError:
                continue
            rows.append(dict({
                "path": art["path"],
                "bytes": art["bytes"],
                "age_days": facts["age_days"],
                "qualifies": classify.is_project_artifact(art["path"], facts),
            }, **pnpm_facts(art["path"])))
        return rows
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
            **pnpm_facts(path),
        })
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
