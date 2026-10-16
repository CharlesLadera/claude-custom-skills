#!/usr/bin/env python3
"""Read-only Gmail metadata scanner for the gmail-organize skill.

Lists messages, fetches headers only (format=metadata), and writes two files:
summary.json (grouped by sender; small enough to read) and ids.json (group
key -> message ids, plus each message's labels at scan time, for
apply/apply.py). Message snippets are dropped and never written.

Read-only by construction: every gws call goes through gws_read, which
refuses any method outside READ_METHODS. tests/test_safety.py enforces it.
"""
import argparse
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import gws  # noqa: E402

READ_METHODS = frozenset({"list", "get", "getProfile"})
HEADERS = ["From", "List-Id", "List-Unsubscribe", "Subject", "Date"]
PAGE_SIZE = 500
OWN_MAIL = frozenset({"SENT", "DRAFT"})
ABORT_CODES = frozenset({gws.EXIT_AUTH, gws.EXIT_DISCOVERY, gws.EXIT_NOT_FOUND})


class Ctx:
    def __init__(self, exe, runner=None, sleep=None):
        self.exe = exe
        self.kw = {}
        if runner is not None:
            self.kw["runner"] = runner
        if sleep is not None:
            self.kw["sleep"] = sleep


def gws_read(ctx, path, method, params):
    """The only place scan.py reaches gws."""
    if method not in READ_METHODS:
        raise ValueError("scan.py is read-only; refused method %r" % method)
    return gws.run_gws(ctx.exe, path, method, params, **ctx.kw)


def header(msg, name):
    for h in msg.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def parse_list_id(value):
    value = (value or "").strip()
    if "<" in value and ">" in value:
        value = value[value.rindex("<") + 1:value.rindex(">")]
    return value.strip().lower() or None


def sender(msg):
    addr = parseaddr(header(msg, "From"))[1].strip().lower()
    if "@" not in addr:
        return "", ""
    return addr, addr.rsplit("@", 1)[1]


def group_key(msg):
    list_id = parse_list_id(header(msg, "List-Id"))
    if list_id:
        return "list:" + list_id, "list_id"
    addr = sender(msg)[0]
    return "from:" + (addr or "(unknown)"), "from"


def day(msg):
    ms = int(msg.get("internalDate", "0"))
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def summarize(messages):
    """Group MESSAGES by sender. Returns (groups, domains, ids_map, labels_by_id).

    The user's own SENT and DRAFT messages only mark their threads; they are
    never grouped, so no apply op can target them."""
    sent_threads = {m["threadId"] for m in messages if "SENT" in m.get("labelIds", [])}
    buckets = {}
    for m in sorted(messages, key=lambda m: int(m.get("internalDate", "0")), reverse=True):
        if OWN_MAIL & set(m.get("labelIds", [])):
            continue
        buckets.setdefault(group_key(m), []).append(m)
    groups, ids_map, labels_by_id = [], {}, {}
    domain_counts, domain_groups = Counter(), Counter()
    for (key, kind), ms in buckets.items():
        top_from = Counter(sender(m)[0] for m in ms).most_common(1)[0][0]
        domain = top_from.rsplit("@", 1)[1] if "@" in top_from else ""
        label_ids, categories, subjects = Counter(), Counter(), []
        for m in ms:
            for l in m.get("labelIds", []):
                if l.startswith("CATEGORY_"):
                    categories[l] += 1
                elif l != "UNREAD":
                    label_ids[l] += 1
            subject = header(m, "Subject")
            if subject and subject not in subjects and len(subjects) < 2:
                subjects.append(subject)
            labels_by_id[m["id"]] = m.get("labelIds", [])
        groups.append({
            "key": key, "kind": kind, "from": top_from, "domain": domain,
            "count": len(ms),
            "unread": sum(1 for m in ms if "UNREAD" in m.get("labelIds", [])),
            "list_mail": sum(1 for m in ms if header(m, "List-Id") or header(m, "List-Unsubscribe")),
            "newest": day(ms[0]), "oldest": day(ms[-1]),
            "label_ids": dict(label_ids), "categories": dict(categories),
            "has_sent_in_thread": sum(1 for m in ms if m["threadId"] in sent_threads),
            "sample_subjects": subjects,
        })
        ids_map[key] = [m["id"] for m in ms]
        domain_counts[domain] += len(ms)
        domain_groups[domain] += 1
    groups.sort(key=lambda g: (-g["count"], g["key"]))
    domains = [{"domain": d, "count": c, "groups": domain_groups[d]}
               for d, c in domain_counts.most_common()]
    return groups, domains, ids_map, labels_by_id


class MailboxMismatch(Exception):
    pass


def list_ids(ctx, query, max_n=None):
    ids, token = [], None
    while True:
        params = {"userId": "me", "maxResults": PAGE_SIZE}
        if query:
            params["q"] = query
        if token:
            params["pageToken"] = token
        page = gws_read(ctx, "gmail.users.messages", "list", params)
        ids += [m["id"] for m in page.get("messages", [])]
        token = page.get("nextPageToken")
        if not token or (max_n and len(ids) >= max_n):
            return ids[:max_n] if max_n else ids


def fetch_all(ctx, ids, workers):
    """Fetch header metadata for IDS in parallel.
    Returns (messages, error count, first error text or None).

    Auth, discovery, and missing-binary failures abort the scan; any other
    failure (after gws.run_gws's rate-limit retries) counts as one error."""
    def one(mid):
        try:
            m = gws_read(ctx, "gmail.users.messages", "get",
                         {"userId": "me", "id": mid, "format": "metadata", "metadataHeaders": HEADERS})
        except gws.GwsError as e:
            if e.code in ABORT_CODES:
                raise
            return e
        m.pop("snippet", None)
        return m

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(one, ids))
    messages = [m for m in results if not isinstance(m, gws.GwsError)]
    failures = [str(e)[:300] for e in results if isinstance(e, gws.GwsError)]
    return messages, len(failures), (failures[0] if failures else None)


def read_labels(ctx, workers):
    listed = gws_read(ctx, "gmail.users.labels", "list", {"userId": "me"}).get("labels", [])

    def one(label):
        if label.get("type") != "user":
            return {"id": label["id"], "name": label["name"], "type": label.get("type", "system")}
        full = gws_read(ctx, "gmail.users.labels", "get", {"userId": "me", "id": label["id"]})
        return {"id": label["id"], "name": label["name"], "type": "user",
                "messages": full.get("messagesTotal", 0)}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return sorted(pool.map(one, listed), key=lambda l: l["name"].lower())


def read_filters(ctx):
    return gws_read(ctx, "gmail.users.settings.filters", "list", {"userId": "me"}).get("filter", [])


def since_query(since):
    dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return "after:%d" % int(dt.timestamp())


def scan(ctx, out_dir, since=None, workers=8, max_n=None, expect_mailbox=None, now=None):
    profile = gws_read(ctx, "gmail.users", "getProfile", {"userId": "me"})
    mailbox = profile["emailAddress"].lower()
    if expect_mailbox and expect_mailbox.lower() != mailbox:
        raise MailboxMismatch("gws is signed in as %s, not %s" % (mailbox, expect_mailbox.lower()))
    scanned_at = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    query = since_query(since) if since else ""
    ids = list_ids(ctx, query, max_n)
    messages, errors, error_sample = fetch_all(ctx, ids, workers)
    groups, domains, ids_map, labels_by_id = summarize(messages)
    summary = {
        "mailbox": mailbox, "scanned_at": scanned_at, "query": query,
        "total": len(ids), "errors": errors, "error_sample": error_sample,
        "labels": read_labels(ctx, workers), "filters": read_filters(ctx),
        "groups": groups, "domains": domains,
    }
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")
    (out / "ids.json").write_text(json.dumps({"mailbox": mailbox, "groups": ids_map, "labels": labels_by_id}),
                                  encoding="utf-8")
    return summary


def main(argv=None, runner=None, sleep=None, exe=None):
    ap = argparse.ArgumentParser(description="Read-only Gmail metadata scanner for gmail-organize.")
    ap.add_argument("--out", required=True, help="directory for summary.json and ids.json")
    ap.add_argument("--since", help="ISO timestamp; scan only mail after it")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--max", type=int, dest="max_n", help="scan at most N messages")
    ap.add_argument("--expect-mailbox", help="exit 3 if gws is signed in as anyone else")
    a = ap.parse_args(argv)
    if a.since:
        try:
            since_query(a.since)
        except ValueError:
            print("scan.py: --since must be an ISO timestamp, got %r" % a.since, file=sys.stderr)
            return gws.EXIT_VALIDATION
    try:
        ctx = Ctx(exe or gws.resolve_gws(), runner, sleep)
        s = scan(ctx, a.out, a.since, a.workers, a.max_n, a.expect_mailbox)
    except MailboxMismatch as e:
        print("scan.py: %s" % e, file=sys.stderr)
        return gws.EXIT_VALIDATION
    except gws.GwsError as e:
        print("scan.py: %s" % e, file=sys.stderr)
        return e.code
    out = Path(a.out)
    print(json.dumps({"summary": str(out / "summary.json"), "ids": str(out / "ids.json"),
                      "mailbox": s["mailbox"], "total": s["total"], "errors": s["errors"],
                      "groups": len(s["groups"])}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
