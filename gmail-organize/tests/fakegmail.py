"""In-memory stand-in for the gws CLI, used as the subprocess runner in tests.

It parses the argv that lib/gws.py builds, dispatches to a handler named
_<path with dots as underscores>__<method>, and returns a CompletedProcess-like
object. Unknown ids surface as gws exit 1 with a 404 message, like the real API.
"""
import json
from types import SimpleNamespace

SYSTEM_LABELS = [
    "INBOX", "SENT", "DRAFT", "TRASH", "SPAM", "STARRED", "IMPORTANT", "UNREAD",
    "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS", "CATEGORY_UPDATES",
    "CATEGORY_FORUMS",
]


def msg(mid, frm, subject="s", labels=("INBOX",), thread=None, ts_ms=1750000000000,
        list_id=None, unsubscribe=None):
    headers = [{"name": "From", "value": frm}, {"name": "Subject", "value": subject}]
    if list_id:
        headers.append({"name": "List-Id", "value": list_id})
    if unsubscribe:
        headers.append({"name": "List-Unsubscribe", "value": unsubscribe})
    return {"id": mid, "threadId": thread or mid, "labelIds": list(labels),
            "internalDate": str(ts_ms), "payload": {"headers": headers}}


class _ApiError(Exception):
    pass


class FakeGmail:
    def __init__(self, mailbox="user@example.com", messages=(), labels=(), filters=()):
        self.mailbox = mailbox
        self.messages = {m["id"]: m for m in messages}
        self.labels = {l: {"id": l, "name": l, "type": "system"} for l in SYSTEM_LABELS}
        for l in labels:
            self.labels[l["id"]] = dict(l, type="user")
        self.filters = {f["id"]: dict(f) for f in filters}
        self.calls = []
        self.failures = {}
        self._next = 1000

    def _check_labels(self, ids):
        bad = [l for l in ids if l not in self.labels]
        if bad:
            raise _ApiError("400 Invalid label: %s" % bad[0])

    def fail(self, path, method, code, output, times=1):
        self.failures.setdefault((path, method), []).extend([(code, output)] * times)

    def __call__(self, argv, **kwargs):
        rest = argv[1:]
        i = rest.index("--params")
        path, method = ".".join(rest[:i - 1]), rest[i - 1]
        params = json.loads(rest[i + 1])
        body = json.loads(rest[rest.index("--json") + 1]) if "--json" in rest else None
        self.calls.append((path, method, params, body))
        queued = self.failures.get((path, method))
        if queued:
            code, output = queued.pop(0)
            return SimpleNamespace(returncode=code, stdout="", stderr=output)
        handler = getattr(self, "_" + path.replace(".", "_") + "__" + method, None)
        if handler is None:
            return SimpleNamespace(returncode=3, stdout="", stderr="fake gws: no %s %s" % (path, method))
        try:
            result = handler(params, body)
        except KeyError as e:
            return SimpleNamespace(returncode=1, stdout="", stderr="404 Requested entity was not found: %s" % e)
        except _ApiError as e:
            return SimpleNamespace(returncode=1, stdout="", stderr=str(e))
        out = json.dumps(result) if result is not None else ""
        return SimpleNamespace(returncode=0, stdout=out, stderr="Using keyring backend: keyring\n")

    # --- profile / labels -------------------------------------------------
    def _gmail_users__getProfile(self, p, b):
        return {"emailAddress": self.mailbox, "messagesTotal": len(self.messages)}

    def _gmail_users_labels__list(self, p, b):
        return {"labels": [{"id": l["id"], "name": l["name"], "type": l["type"]}
                           for l in self.labels.values()]}

    def _gmail_users_labels__get(self, p, b):
        l = self.labels[p["id"]]
        total = sum(1 for m in self.messages.values() if l["id"] in m["labelIds"])
        return dict(l, messagesTotal=total)

    def _gmail_users_labels__create(self, p, b):
        if any(l["name"] == b["name"] for l in self.labels.values()):
            raise _ApiError("409 Label name exists or conflicts")
        lid = "Label_%d" % self._next
        self._next += 1
        self.labels[lid] = dict(b, id=lid, type="user")
        return self.labels[lid]

    def _gmail_users_labels__patch(self, p, b):
        self.labels[p["id"]].update(b)
        return self.labels[p["id"]]

    def _gmail_users_labels__delete(self, p, b):
        del self.labels[p["id"]]
        for m in self.messages.values():
            if p["id"] in m["labelIds"]:
                m["labelIds"].remove(p["id"])
        return None

    # --- messages -----------------------------------------------------------
    def _gmail_users_messages__list(self, p, b):
        ms = [m for m in self.messages.values()
              if "TRASH" not in m["labelIds"] and "SPAM" not in m["labelIds"]]
        for lid in p.get("labelIds", []):
            ms = [m for m in ms if lid in m["labelIds"]]
        start, size = int(p.get("pageToken") or 0), p.get("maxResults", 100)
        page = ms[start:start + size]
        out = {"resultSizeEstimate": len(ms)}
        if page:
            out["messages"] = [{"id": m["id"], "threadId": m["threadId"]} for m in page]
        if start + size < len(ms):
            out["nextPageToken"] = str(start + size)
        return out

    def _gmail_users_messages__get(self, p, b):
        m = self.messages[p["id"]]
        out = {"id": m["id"], "threadId": m["threadId"], "labelIds": list(m["labelIds"]),
               "internalDate": m["internalDate"], "snippet": "secret snippet"}
        if p.get("format") == "metadata":
            want = {h.lower() for h in p.get("metadataHeaders", [])}
            out["payload"] = {"headers": [h for h in m["payload"]["headers"]
                                          if h["name"].lower() in want]}
        return out

    def _gmail_users_messages__batchModify(self, p, b):
        self._check_labels(b.get("addLabelIds", []) + b.get("removeLabelIds", []))
        for mid in b["ids"]:
            self.messages[mid]  # validate every id first, like the real API
        for mid in b["ids"]:
            m = self.messages[mid]
            for l in b.get("addLabelIds", []):
                if l not in m["labelIds"]:
                    m["labelIds"].append(l)
            m["labelIds"] = [l for l in m["labelIds"] if l not in b.get("removeLabelIds", [])]
        return None

    def _gmail_users_messages__trash(self, p, b):
        m = self.messages[p["id"]]
        if "TRASH" not in m["labelIds"]:
            m["labelIds"].append("TRASH")
        return {"id": m["id"], "labelIds": m["labelIds"]}

    def _gmail_users_messages__untrash(self, p, b):
        m = self.messages[p["id"]]
        m["labelIds"] = [l for l in m["labelIds"] if l != "TRASH"]
        return {"id": m["id"], "labelIds": m["labelIds"]}

    def _gmail_users_threads__get(self, p, b):
        ms = [m for m in self.messages.values() if m["threadId"] == p["id"]]
        if not ms:
            raise KeyError(p["id"])
        return {"id": p["id"], "messages": [{"id": m["id"], "labelIds": list(m["labelIds"])} for m in ms]}

    # --- filters ------------------------------------------------------------
    def _gmail_users_settings_filters__list(self, p, b):
        return {"filter": list(self.filters.values())} if self.filters else {}

    def _gmail_users_settings_filters__create(self, p, b):
        self._check_labels(b["action"].get("addLabelIds", []) + b["action"].get("removeLabelIds", []))
        fid = "F%d" % self._next
        self._next += 1
        self.filters[fid] = dict(b, id=fid)
        return self.filters[fid]

    def _gmail_users_settings_filters__delete(self, p, b):
        del self.filters[p["id"]]
        return None
