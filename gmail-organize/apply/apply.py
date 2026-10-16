#!/usr/bin/env python3
"""Apply one approved gmail-organize batch, or undo the last one.

SKILL.md writes a batch file only after the user approves a table. This
script validates the whole batch before any write, runs its ops in order,
appends each write to the undo log right after the API call, verifies it,
and updates the mailbox state file once every op is verified.

It never deletes mail: only the calls in READ_CALLS and WRITE_CALLS can be
made, and tests/test_safety.py fails the suite if WRITE_CALLS changes.
"""
import argparse
import json
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import gws  # noqa: E402

READ_CALLS = frozenset({
    ("gmail.users", "getProfile"),
    ("gmail.users.labels", "list"),
    ("gmail.users.messages", "list"),
    ("gmail.users.messages", "get"),
    ("gmail.users.threads", "get"),
    ("gmail.users.settings.filters", "list"),
})
WRITE_CALLS = frozenset({
    ("gmail.users.labels", "create"),
    ("gmail.users.labels", "patch"),
    ("gmail.users.labels", "delete"),
    ("gmail.users.messages", "batchModify"),
    ("gmail.users.messages", "trash"),
    ("gmail.users.messages", "untrash"),
    ("gmail.users.settings.filters", "create"),
    ("gmail.users.settings.filters", "delete"),
})
OPS = ("label_create", "label_rename", "label_delete", "modify", "trash", "filter_create", "filter_delete")
CHUNK = 300
TRASH_CHUNK = 50
VERIFY_SAMPLE = 50
MAX_SCAN_AGE_S = 3600
WORKERS = 8
PROTECTED_SYSTEM = frozenset({"STARRED", "IMPORTANT", "SENT", "DRAFT"})
PROTECTED_ROOTS = ("Clients", "Team", "Finance")
UNTOUCHABLE = frozenset({"SENT", "DRAFT", "TRASH", "SPAM"})
RULE_KEYS = ("list_id", "from", "domain")
STATE_KEYS = ("rules", "filters_created", "last_run")
TS_FMT = "%Y-%m-%dT%H:%M:%SZ"
DEFAULT_STATE_DIR = Path.home() / ".config" / "gmail-organize"


class ApplyError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Gmail:
    def __init__(self, exe, runner=None, sleep=None):
        self.exe = exe
        self.kw = {k: v for k, v in (("runner", runner), ("sleep", sleep)) if v is not None}
        self.labels = {}  # label id -> name, refreshed from Gmail


def gws_call(gm, path, method, params, body=None):
    """The only place apply.py reaches gws. Refuses anything outside the allowlists."""
    if (path, method) not in READ_CALLS and (path, method) not in WRITE_CALLS:
        raise ValueError("apply.py refuses %s %s" % (path, method))
    return gws.run_gws(gm.exe, path, method, dict(params, userId="me"), body, **gm.kw)


class Run:
    """One apply or undo invocation: its id, undo log, and collected results."""

    def __init__(self, log_path, now, ids_doc=None, undo_of=None):
        self.batch_id = uuid.uuid4().hex[:12]
        self.log_path = Path(log_path)
        self.now = now
        self.ids_doc = ids_doc
        self.undo_of = undo_of
        self.results = []
        self.filters_created = []
        self.filters_deleted = []
        self.trash_min_age_days = 90
        self.state_path = None
        self.state_written = False
        self.wrote = False
        self.id_map = {}  # deleted label id -> id of the label recreated by undo

    def remap(self, lid):
        seen = set()
        while lid in self.id_map and lid not in seen:
            seen.add(lid)
            lid = self.id_map[lid]
        return lid

    def log(self, entry):
        entry = dict(entry, ts=self.now.strftime(TS_FMT), batch_id=self.batch_id)
        if self.undo_of:
            entry["undo_of"] = self.undo_of
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        self.wrote = True


# --- lookups ----------------------------------------------------------------

def refresh_labels(gm):
    listed = gws_call(gm, "gmail.users.labels", "list", {}).get("labels", [])
    gm.labels = {l["id"]: l["name"] for l in listed}
    return gm.labels


def label_id(gm, name):
    for lid, n in gm.labels.items():
        if n == name:
            return lid
    raise ApplyError(gws.EXIT_VALIDATION, "unknown label %r" % name)


def protected_name(name):
    return name.startswith("@") or any(name == r or name.startswith(r + "/") for r in PROTECTED_ROOTS)


def list_filters(gm):
    return gws_call(gm, "gmail.users.settings.filters", "list", {}).get("filter", [])


def ids_with_label(gm, lid):
    ids, token = [], None
    while True:
        params = {"labelIds": [lid], "maxResults": 500}
        if token:
            params["pageToken"] = token
        page = gws_call(gm, "gmail.users.messages", "list", params)
        ids += [m["id"] for m in page.get("messages", [])]
        token = page.get("nextPageToken")
        if not token:
            return ids


def get_minimal(gm, ids):
    """Fetch labelIds / threadId / internalDate for IDS. Vanished ids map to None."""
    def one(mid):
        try:
            return mid, gws_call(gm, "gmail.users.messages", "get", {"id": mid, "format": "minimal"})
        except gws.GwsError as e:
            if e.not_found:
                return mid, None
            raise

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return dict(pool.map(one, ids))


def sample(ids, n=VERIFY_SAMPLE):
    ids = list(ids)
    if len(ids) <= n:
        return ids
    step = len(ids) / n
    return [ids[int(i * step)] for i in range(n)]


def group_ids(ids_doc, groups):
    seen, out = set(), []
    for g in groups:
        for mid in ids_doc["groups"][g]:
            if mid not in seen:
                seen.add(mid)
                out.append(mid)
    return out


def result(op, requested, applied, checked=0, bad=(), vanished=0, skipped=0):
    bad = list(bad)
    return {"op": op, "requested": requested, "applied": applied, "checked": checked,
            "verified": checked - len(bad), "vanished": vanished, "skipped": skipped,
            "status": "mismatch" if bad else "ok", "mismatched": bad[:10]}


# --- label changes ----------------------------------------------------------

def modify_chunk(gm, run, chunk, label, add):
    body = {"ids": chunk, "addLabelIds": [label] if add else [], "removeLabelIds": [] if add else [label]}
    gws_call(gm, "gmail.users.messages", "batchModify", {}, body)
    run.log({"op": "modify", ("added" if add else "removed"): {label: chunk}})


def batch_modify(gm, run, ids, label, add):
    """Add or remove LABEL on IDS in CHUNK-sized calls. Returns the ids that vanished."""
    gone = set()
    for start in range(0, len(ids), CHUNK):
        chunk = ids[start:start + CHUNK]
        try:
            modify_chunk(gm, run, chunk, label, add)
        except gws.GwsError as e:
            if not e.not_found:
                raise
            missing = {mid for mid, m in get_minimal(gm, chunk).items() if m is None}
            if not missing:
                raise
            gone |= missing
            rest = [mid for mid in chunk if mid not in missing]
            if rest:
                modify_chunk(gm, run, rest, label, add)
    return gone


def apply_changes(gm, run, added, removed):
    """ADDED / REMOVED map label id -> ids. Applies them, then verifies a sample.
    Returns (touched ids, checked count, mismatched ids, vanished ids)."""
    vanished = set()
    for label, ids in added.items():
        vanished |= batch_modify(gm, run, ids, label, True)
    for label, ids in removed.items():
        vanished |= batch_modify(gm, run, ids, label, False)
    touched = sorted({mid for ids in list(added.values()) + list(removed.values()) for mid in ids} - vanished)
    got = get_minimal(gm, sample(touched))
    bad = []
    for mid, m in got.items():
        if m is None:
            vanished.add(mid)
            continue
        have = set(m.get("labelIds", []))
        if (any(mid in ids and l not in have for l, ids in added.items())
                or any(mid in ids and l in have for l, ids in removed.items())):
            bad.append(mid)
    checked = sum(1 for m in got.values() if m is not None)
    return touched, checked, bad, vanished


def op_modify(gm, op, run):
    add = [label_id(gm, n) for n in op.get("add", [])]
    remove = [label_id(gm, n) for n in op.get("remove", [])]
    if op.get("from_label"):
        source = label_id(gm, op["from_label"])
        ids = ids_with_label(gm, source)
        has = {l: set(ids_with_label(gm, l)) for l in add + remove if l != source}
        has[source] = set(ids)
    else:
        ids = group_ids(run.ids_doc, op["groups"])
        before = run.ids_doc["labels"]
        has = {l: {mid for mid in ids if l in before.get(mid, [])} for l in add + remove}
    added = {l: [mid for mid in ids if mid not in has[l]] for l in add}
    removed = {l: [mid for mid in ids if mid in has[l]] for l in remove}
    added = {l: v for l, v in added.items() if v}
    removed = {l: v for l, v in removed.items() if v}
    touched, checked, bad, vanished = apply_changes(gm, run, added, removed)
    skipped = len(set(ids) - set(touched) - vanished)
    return result("modify", len(ids), len(touched), checked, bad, len(vanished), skipped)


# --- label and filter ops ---------------------------------------------------

def op_label_create(gm, op, run):
    if op["name"] in gm.labels.values():
        return result("label_create", 1, 0, skipped=1)
    body = {"name": op["name"], "labelListVisibility": "labelShow", "messageListVisibility": "show"}
    if op.get("color"):
        body["color"] = op["color"]
    made = gws_call(gm, "gmail.users.labels", "create", {}, body)
    run.log({"op": "label_create", "id": made["id"], "name": op["name"]})
    ok = refresh_labels(gm).get(made["id"]) == op["name"]
    return result("label_create", 1, 1, 1, [] if ok else [made["id"]])


def op_label_rename(gm, op, run):
    old = gm.labels.get(op["id"])
    if old is None:
        raise ApplyError(gws.EXIT_API, "label %s no longer exists" % op["id"])
    if old == op["name"]:
        return result("label_rename", 1, 0, skipped=1)
    gws_call(gm, "gmail.users.labels", "patch", {"id": op["id"]}, {"name": op["name"]})
    run.log({"op": "label_rename", "id": op["id"], "old": old, "new": op["name"]})
    ok = refresh_labels(gm).get(op["id"]) == op["name"]
    return result("label_rename", 1, 1, 1, [] if ok else [op["id"]])


def op_label_delete(gm, op, run):
    name = gm.labels.get(op["id"])
    if name is None:
        raise ApplyError(gws.EXIT_API, "label %s no longer exists" % op["id"])
    members = ids_with_label(gm, op["id"])
    if protected_name(name) and members:
        # Backstop for validate(): deleting it would strip protection from its mail.
        raise ApplyError(gws.EXIT_VALIDATION, "%s is protected and still has %d messages" % (name, len(members)))
    gws_call(gm, "gmail.users.labels", "delete", {"id": op["id"]})
    run.log({"op": "label_delete", "id": op["id"], "name": name, "members": members})
    ok = op["id"] not in refresh_labels(gm)
    return result("label_delete", 1, 1, 1, [] if ok else [op["id"]])


def filter_action(gm, action):
    """Resolve label names to ids; pass forward through (validate() limits it)."""
    out = {k: v for k, v in action.items() if k not in ("add", "remove")}
    out["addLabelIds"] = [label_id(gm, n) for n in action.get("add", [])]
    out["removeLabelIds"] = [label_id(gm, n) for n in action.get("remove", [])]
    return out


def create_filter(gm, run, criteria, action_ids):
    made = gws_call(gm, "gmail.users.settings.filters", "create", {},
                    {"criteria": criteria, "action": action_ids})
    run.log({"op": "filter_create", "id": made["id"], "criteria": criteria, "action": action_ids})
    ok = made["id"] in {f["id"] for f in list_filters(gm)}
    return made["id"], result("filter_create", 1, 1, 1, [] if ok else [made["id"]])


def op_filter_create(gm, op, run):
    fid, r = create_filter(gm, run, op["criteria"], filter_action(gm, op["action"]))
    run.filters_created.append({"id": fid, "criteria": op["criteria"]})
    return r


def op_filter_delete(gm, op, run):
    current = {f["id"]: f for f in list_filters(gm)}
    if op["id"] not in current:
        raise ApplyError(gws.EXIT_API, "filter %s no longer exists" % op["id"])
    f = current[op["id"]]
    gws_call(gm, "gmail.users.settings.filters", "delete", {"id": op["id"]})
    run.log({"op": "filter_delete", "id": op["id"], "criteria": f.get("criteria", {}), "action": f.get("action", {})})
    run.filters_deleted.append(op["id"])
    ok = op["id"] not in {x["id"] for x in list_filters(gm)}
    return result("filter_delete", 1, 1, 1, [] if ok else [op["id"]])


# --- trash ------------------------------------------------------------------

def trash_eligible(gm, ids, min_age_days, now):
    """Return the IDS that pass every protective check, re-read live from Gmail.

    Deciding what is junk is Claude's call in the approved trash table; this
    only refuses messages that must never be trashed regardless."""
    cutoff_ms = (now.timestamp() - min_age_days * 86400) * 1000
    live = get_minimal(gm, ids)
    thread_of = {}
    for mid in ids:
        m = live.get(mid)
        if m is None:
            continue
        labels = set(m.get("labelIds", []))
        if labels & PROTECTED_SYSTEM:
            continue
        if any(protected_name(gm.labels.get(l, l)) for l in labels):
            continue
        if int(m.get("internalDate", "0")) > cutoff_ms:
            continue
        thread_of[mid] = m["threadId"]

    def sent_in(tid):
        t = gws_call(gm, "gmail.users.threads", "get", {"id": tid, "format": "minimal"})
        return tid, any("SENT" in x.get("labelIds", []) for x in t.get("messages", []))

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        sent = {tid for tid, has_sent in pool.map(sent_in, set(thread_of.values())) if has_sent}
    return [mid for mid in ids if mid in thread_of and thread_of[mid] not in sent]


def run_each(gm, method, ids):
    """Call messages.<METHOD> once per id in parallel.
    Returns (done ids, vanished ids, first error other than not-found)."""
    def one(mid):
        try:
            gws_call(gm, "gmail.users.messages", method, {"id": mid})
            return mid, None
        except gws.GwsError as e:
            return mid, ("gone" if e.not_found else e)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        outcomes = list(pool.map(one, ids))
    done = [mid for mid, status in outcomes if status is None]
    gone = [mid for mid, status in outcomes if status == "gone"]
    errors = [status for mid, status in outcomes if isinstance(status, gws.GwsError)]
    return done, gone, (errors[0] if errors else None)


def each_logged(gm, run, method, ids, log_op):
    """run_each in TRASH_CHUNK slices, logging each slice as soon as it lands,
    so an interruption loses at most one unlogged slice."""
    done_all, gone_all = [], []
    for start in range(0, len(ids), TRASH_CHUNK):
        done, gone, err = run_each(gm, method, ids[start:start + TRASH_CHUNK])
        if done:
            run.log({"op": log_op, "ids": done})
        done_all += done
        gone_all += gone
        if err:
            raise err
    return done_all, gone_all


def op_trash(gm, op, run):
    ids = group_ids(run.ids_doc, op["groups"])
    eligible = trash_eligible(gm, ids, op.get("min_age_days", run.trash_min_age_days), run.now)
    done, gone = each_logged(gm, run, "trash", eligible, "trash")
    got = get_minimal(gm, sample(done))
    bad = [mid for mid, m in got.items() if m is not None and "TRASH" not in m.get("labelIds", [])]
    checked = sum(1 for m in got.values() if m is not None)
    return result("trash", len(ids), len(done), checked, bad, len(gone), len(ids) - len(eligible))


OP_FUNCS = {
    "label_create": op_label_create,
    "trash": op_trash,
    "label_rename": op_label_rename,
    "label_delete": op_label_delete,
    "modify": op_modify,
    "filter_create": op_filter_create,
    "filter_delete": op_filter_delete,
}


# --- validation -------------------------------------------------------------

def validate(batch, summary, ids_doc, mailbox, live_labels, now, live_forwards=frozenset()):
    """Raise ApplyError(3) unless every op in BATCH can run. Writes nothing.

    LIVE_FORWARDS: forward addresses on the mailbox's current filters, read live."""
    def fail(message):
        return ApplyError(gws.EXIT_VALIDATION, message)

    if summary.get("mailbox") != mailbox or ids_doc.get("mailbox") != mailbox:
        raise fail("scan files are for %s, not %s" % (summary.get("mailbox"), mailbox))
    scanned = datetime.strptime(summary["scanned_at"], TS_FMT).replace(tzinfo=timezone.utc)
    if (now - scanned).total_seconds() > MAX_SCAN_AGE_S:
        raise fail("scan is more than 1 hour old; rescan before applying")
    ops = batch.get("ops")
    if not isinstance(ops, list) or (not ops and not batch.get("state")):
        raise fail("batch has no ops")
    # Trash re-checks protections live, so an earlier op in the same batch
    # (e.g. removing STARRED) could strip one first.
    if len(ops) > 1 and any(isinstance(op, dict) and op.get("op") == "trash" for op in ops):
        raise fail("trash must run in its own batch")
    id_names = dict(live_labels)
    names = set(id_names.values())
    merged_out = set()  # protected labels whose mail a modify moved into another protected label
    for i, op in enumerate(ops):
        kind = op.get("op")
        where = "op %d (%s)" % (i, kind)

        def need(*keys):
            missing = [k for k in keys if not op.get(k)]
            if missing:
                raise fail("%s: missing %s" % (where, ", ".join(missing)))

        def known(label_names):
            unknown = [n for n in label_names if n not in names]
            if unknown:
                raise fail("%s: unknown labels %s" % (where, unknown))

        def user_label():
            if not op["id"].startswith("Label_") or op["id"] not in id_names:
                raise fail("%s: %s is not an existing user label" % (where, op["id"]))

        if kind not in OPS:
            raise fail("%s: unknown op" % where)
        if kind == "label_create":
            need("name")
            names.add(op["name"])
        elif kind == "label_rename":
            need("id", "name")
            user_label()
            if protected_name(id_names[op["id"]]) and not protected_name(op["name"]):
                raise fail("%s: %s is protected; it can only be renamed within protection" % (where, id_names[op["id"]]))
            names.discard(id_names[op["id"]])
            id_names[op["id"]] = op["name"]
            names.add(op["name"])
        elif kind == "label_delete":
            need("id")
            user_label()
            if protected_name(id_names[op["id"]]) and id_names[op["id"]] not in merged_out:
                raise fail("%s: %s is protected; merge its mail into another protected label first" % (where, id_names[op["id"]]))
            names.discard(id_names.pop(op["id"]))
        elif kind == "modify":
            if bool(op.get("groups")) == bool(op.get("from_label")):
                raise fail("%s: give exactly one of groups or from_label" % where)
            touched = op.get("add", []) + op.get("remove", [])
            if not touched:
                raise fail("%s: nothing to add or remove" % where)
            if UNTOUCHABLE & set(touched):
                raise fail("%s: %s cannot be added or removed" % (where, sorted(UNTOUCHABLE & set(touched))))
            known(touched + ([op["from_label"]] if op.get("from_label") else []))
            # Trash re-checks protections live, so a batch may move protection but never drop it.
            if {"STARRED", "IMPORTANT"} & set(op.get("remove", [])):
                raise fail("%s: STARRED and IMPORTANT cannot be removed" % where)
            dropped = [n for n in op.get("remove", []) if protected_name(n)]
            if dropped and not any(protected_name(n) for n in op.get("add", [])):
                raise fail("%s: removing protected %s needs a protected label added in the same op" % (where, dropped))
            # Only a whole-label move (from_label) empties the source; a groups move
            # leaves the rest of its mail relying on it. A label that receives mail
            # is no longer empty, even if it was merged out earlier.
            merged_out.difference_update(n for n in op.get("add", []) if protected_name(n))
            if op.get("from_label") in dropped:
                merged_out.add(op["from_label"])
        elif kind == "trash":
            need("groups")
            if summary.get("errors"):
                raise fail("%s: scan had %d errors, so trash is blocked; rescan" % (where, summary["errors"]))
        elif kind == "filter_create":
            need("criteria", "action")
            extra = set(op["action"]) - {"add", "remove", "forward"}
            if extra:
                raise fail("%s: unsupported filter actions %s" % (where, sorted(extra)))
            # forward only carries an existing filter's address over (a merge); a new
            # destination would quietly copy mail out. Checked live: the summary file
            # is named by the batch, so it can't vouch for an address.
            fwd = op["action"].get("forward")
            if fwd is not None and fwd not in live_forwards:
                raise fail("%s: forward to %s is not on an existing filter" % (where, fwd))
            touched = op["action"].get("add", []) + op["action"].get("remove", [])
            if UNTOUCHABLE & set(touched):
                raise fail("%s: %s cannot be added or removed" % (where, sorted(UNTOUCHABLE & set(touched))))
            known(touched)
        elif kind == "filter_delete":
            need("id")
        if kind in ("modify", "trash") and op.get("groups"):
            missing = [g for g in op["groups"] if g not in ids_doc["groups"]]
            if missing:
                raise fail("%s: groups not in this scan: %s" % (where, missing))
    for rule in batch.get("state", {}).get("rules_add", []):
        match = rule.get("match", {})
        if (len(match) != 1 or next(iter(match)) not in RULE_KEYS
                or not isinstance(rule.get("label"), str) or not isinstance(rule.get("archive"), bool)):
            raise fail("bad rule %r" % (rule,))


# --- state ------------------------------------------------------------------

def state_paths(state_dir, mailbox):
    d = Path(state_dir).expanduser()
    return d / (mailbox + ".json"), d / (mailbox + ".log.jsonl")


def load_state(path, mailbox, now):
    if not path.exists():
        return {"version": 1, "mailbox": mailbox, "created": now.strftime("%Y-%m-%d"),
                "last_run": None, "trash_min_age_days": 90, "rules": [], "filters_created": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ApplyError(gws.EXIT_VALIDATION, "state file is not valid JSON (%s); fix or move it: %s" % (e, path))


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def update_state(state, batch, run, summary):
    upd = batch.get("state", {})
    rules = state["rules"]
    for rule in upd.get("rules_add", []):
        rules = [r for r in rules if r["match"] != rule["match"]] + [rule]
    state["rules"] = rules
    state["filters_created"] = ([f for f in state["filters_created"] if f["id"] not in run.filters_deleted]
                                + run.filters_created)
    if upd.get("finalize"):
        state["last_run"] = summary["scanned_at"]


# --- entry points -----------------------------------------------------------

def load_json(path, what):
    if not path:
        raise ApplyError(gws.EXIT_VALIDATION, "%s path missing from batch" % what)
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ApplyError(gws.EXIT_VALIDATION, "%s not found: %s" % (what, path))
    except json.JSONDecodeError as e:
        raise ApplyError(gws.EXIT_VALIDATION, "%s is not valid JSON (%s): %s" % (what, e, path))


def check_mailbox(gm, mailbox):
    got = gws_call(gm, "gmail.users", "getProfile", {})["emailAddress"].lower()
    if got != mailbox:
        raise ApplyError(gws.EXIT_VALIDATION, "gws is signed in as %s, not %s; export "
                         "GOOGLE_WORKSPACE_CLI_CONFIG_DIR or GMAIL_ORGANIZE_GWS for the right account" % (got, mailbox))


def report(run, state_written, error=None):
    out = {"batch_id": run.batch_id, "results": run.results, "state_written": state_written}
    if run.undo_of:
        out["undo_of"] = run.undo_of
    if error:
        out["error"] = str(error)
    return out


def after_write_code(run, err):
    """Exit 3 promises nothing was written; once something was, report 1."""
    return gws.EXIT_API if err.code == gws.EXIT_VALIDATION and run.wrote else err.code


def apply_batch(gm, batch_path, state_dir, mailbox, now):
    batch = load_json(batch_path, "batch file")
    summary = load_json(batch.get("scan_summary"), "scan summary")
    ids_doc = load_json(batch.get("scan_ids"), "scan ids")
    state_path, log_path = state_paths(state_dir, mailbox)
    state = load_state(state_path, mailbox, now)
    check_mailbox(gm, mailbox)
    refresh_labels(gm)
    live_forwards = frozenset()
    if any(isinstance(op, dict) and isinstance(op.get("action"), dict) and "forward" in op["action"]
           for op in batch.get("ops") or []):
        live_forwards = frozenset(f.get("action", {}).get("forward") for f in list_filters(gm)) - {None}
    validate(batch, summary, ids_doc, mailbox, gm.labels, now, live_forwards)
    if gws.is_cmd_shim(gm.exe):
        for i, op in enumerate(batch["ops"]):
            sent = {k: v for k, v in op.items() if k != "groups"}
            bad = sorted(set(json.dumps(sent, ensure_ascii=False)) & gws.CMD_METACHARS)
            if bad:
                raise ApplyError(gws.EXIT_VALIDATION, "op %d contains %s, which the gws .cmd shim would "
                                 "mangle; set GMAIL_ORGANIZE_GWS to gws.exe" % (i, " ".join(bad)))
    run = Run(log_path, now, ids_doc)
    run.trash_min_age_days = state.get("trash_min_age_days", 90)
    try:
        for op in batch["ops"]:
            r = OP_FUNCS[op["op"]](gm, op, run)
            run.results.append(r)
            if r["status"] != "ok":
                return report(run, False, "verification mismatch in %s; stopped before the next op"
                              % op["op"]), gws.EXIT_API
    except (ApplyError, gws.GwsError) as e:
        return report(run, False, e), after_write_code(run, e)
    prev = {k: state.get(k) for k in STATE_KEYS}
    update_state(state, batch, run, summary)
    run.log({"op": "state", "prev": prev})
    save_state(state_path, state)
    return report(run, True), 0


# --- undo -------------------------------------------------------------------

def read_log(log_path):
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def live_label_map(gm, run, by_label):
    """Remap BY_LABEL's label ids through run.id_map and drop labels no longer
    in Gmail. Returns (mapped, number of ids dropped)."""
    mapped, dropped = {}, 0
    for lid, ids in by_label.items():
        lid = run.remap(lid)
        if lid in gm.labels:
            mapped.setdefault(lid, []).extend(ids)
        else:
            dropped += len(ids)
    return mapped, dropped


def undo_modify(gm, e, run):
    add, dropped_a = live_label_map(gm, run, e.get("removed", {}))
    remove, dropped_r = live_label_map(gm, run, e.get("added", {}))
    touched, checked, bad, vanished = apply_changes(gm, run, add, remove)
    return result("undo modify", len(touched) + len(vanished), len(touched), checked, bad, len(vanished),
                  dropped_a + dropped_r)


def undo_trash(gm, e, run):
    done, gone = each_logged(gm, run, "untrash", e["ids"], "untrash")
    got = get_minimal(gm, sample(done))
    bad = [mid for mid, m in got.items() if m is not None and "TRASH" in m.get("labelIds", [])]
    checked = sum(1 for m in got.values() if m is not None)
    return result("undo trash", len(e["ids"]), len(done), checked, bad, len(gone))


def undo_label_create(gm, e, run):
    lid = run.remap(e["id"])
    if lid not in gm.labels:
        return result("undo label_create", 1, 0, skipped=1)
    return op_label_delete(gm, {"id": lid}, run)


def undo_label_rename(gm, e, run):
    return op_label_rename(gm, {"id": run.remap(e["id"]), "name": e["old"]}, run)


def undo_label_delete(gm, e, run):
    r = op_label_create(gm, {"name": e["name"]}, run)
    if r["status"] != "ok":
        return r
    restored = label_id(gm, e["name"])
    if restored != e["id"]:
        run.id_map[e["id"]] = restored
        run.log({"op": "relabel", "old": e["id"], "new": restored})
    added = {restored: e["members"]} if e["members"] else {}
    touched, checked, bad, vanished = apply_changes(gm, run, added, {})
    return result("undo label_delete", len(e["members"]), len(touched), checked, bad, len(vanished))


def undo_filter_create(gm, e, run):
    if e["id"] not in {f["id"] for f in list_filters(gm)}:
        return result("undo filter_create", 1, 0, skipped=1)
    return op_filter_delete(gm, {"id": e["id"]}, run)


def undo_filter_delete(gm, e, run):
    action = dict(e["action"])
    for key in ("addLabelIds", "removeLabelIds"):
        action[key] = [run.remap(l) for l in action.get(key, [])]
    if any(l not in gm.labels for l in action["addLabelIds"] + action["removeLabelIds"]):
        return result("undo filter_delete", 1, 0, skipped=1)
    return create_filter(gm, run, e["criteria"], action)[1]


def undo_state(gm, e, run):
    state = load_state(run.state_path, None, run.now)
    state.update(e["prev"])
    save_state(run.state_path, state)
    run.state_written = True
    run.log({"op": "state_restore"})
    return result("undo state", 1, 1)


UNDO_FUNCS = {
    "modify": undo_modify,
    "trash": undo_trash,
    "label_create": undo_label_create,
    "label_rename": undo_label_rename,
    "label_delete": undo_label_delete,
    "filter_create": undo_filter_create,
    "filter_delete": undo_filter_delete,
    "state": undo_state,
}


def undo_last(gm, state_dir, mailbox, now):
    """Reverse the most recent batch that has no completed undo, newest write first."""
    state_path, log_path = state_paths(state_dir, mailbox)
    entries = read_log(log_path)
    completed = {e["undo_of"] for e in entries if e.get("op") == "undo_complete"}
    originals = [e for e in entries if not e.get("undo_of")]
    target = next((e["batch_id"] for e in reversed(originals) if e["batch_id"] not in completed), None)
    if target is None:
        raise ApplyError(gws.EXIT_VALIDATION, "nothing to undo")
    check_mailbox(gm, mailbox)
    refresh_labels(gm)
    run = Run(log_path, now, undo_of=target)
    run.state_path = state_path
    run.id_map = {e["old"]: e["new"] for e in entries if e.get("op") == "relabel"}
    try:
        for e in reversed([e for e in originals if e["batch_id"] == target]):
            r = UNDO_FUNCS[e["op"]](gm, e, run)
            run.results.append(r)
            if r["status"] != "ok":
                return report(run, run.state_written, "verification mismatch while undoing %s" % e["op"]), gws.EXIT_API
    except (ApplyError, gws.GwsError) as e:
        return report(run, run.state_written, e), after_write_code(run, e)
    run.log({"op": "undo_complete"})
    return report(run, run.state_written), 0


def main(argv=None, runner=None, sleep=None, exe=None, now=None):
    ap = argparse.ArgumentParser(description="Apply an approved gmail-organize batch, or undo the last one.")
    ap.add_argument("--batch", help="batch file written by SKILL.md after approval")
    ap.add_argument("--undo-last", action="store_true", help="reverse the most recent batch not yet undone")
    ap.add_argument("--mailbox", required=True, help="mailbox the batch is for; checked against gws")
    ap.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    a = ap.parse_args(argv)
    if bool(a.batch) == a.undo_last:
        ap.error("give exactly one of --batch or --undo-last")
    now = now or datetime.now(timezone.utc)
    mailbox = a.mailbox.lower()
    try:
        gm = Gmail(exe or gws.resolve_gws(), runner, sleep)
        if a.undo_last:
            out, code = undo_last(gm, a.state_dir, mailbox, now)
        else:
            out, code = apply_batch(gm, a.batch, a.state_dir, mailbox, now)
    except (ApplyError, gws.GwsError) as e:
        out, code = {"error": str(e)}, e.code
    print(json.dumps(out, indent=1))
    return code



if __name__ == "__main__":
    sys.exit(main())
