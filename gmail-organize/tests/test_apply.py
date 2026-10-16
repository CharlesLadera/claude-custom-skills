import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))
sys.path.insert(0, str(HERE.parent / "apply"))
import apply as applier  # noqa: E402
import scan  # noqa: E402
from fakegmail import FakeGmail, msg  # noqa: E402

NOW = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)
OLD = int((NOW.timestamp() - 200 * 86400) * 1000)
NEW = int((NOW.timestamp() - 5 * 86400) * 1000)
LIST = "list:r.github.com"
NEWS = "from:news@shop.com"


class Harness(unittest.TestCase):
    """Runs the real scanner against a fake mailbox, then apply.main on batches."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.fake = FakeGmail(messages=[
            msg("g1", "bot@github.com", "a", ["INBOX", "UNREAD"], "t1", OLD, list_id="<r.github.com>"),
            msg("g2", "bot@github.com", "b", ["INBOX"], "t2", OLD, list_id="<r.github.com>"),
            msg("g3", "bot@github.com", "c", ["Label_1"], "t3", OLD, list_id="<r.github.com>"),
            msg("n1", "news@shop.com", "sale", ["INBOX", "CATEGORY_PROMOTIONS"], "t4", OLD),
            msg("n2", "news@shop.com", "sale", ["INBOX", "STARRED"], "t5", OLD),
            msg("n3", "news@shop.com", "sale", ["INBOX"], "t6", NEW),
            msg("n4", "news@shop.com", "sale", ["INBOX"], "t7", OLD),
            msg("n5", "news@shop.com", "sale", ["INBOX", "Label_2"], "t8", OLD),
            msg("me", "me@example.com", "re", ["SENT"], "t7", OLD),
        ], labels=[{"id": "Label_1", "name": "Bots/GitHub"}, {"id": "Label_2", "name": "Clients/Acme"}])
        self.scan()

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self, now=NOW):
        scan.scan(scan.Ctx("gws", runner=self.fake, sleep=lambda s: None), self.dir / "scan", now=now)

    def batch(self, ops, state=None):
        b = {"scan_summary": str(self.dir / "scan" / "summary.json"),
             "scan_ids": str(self.dir / "scan" / "ids.json"), "ops": ops}
        if state is not None:
            b["state"] = state
        p = self.dir / "batch.json"
        p.write_text(json.dumps(b), encoding="utf-8")
        return p

    def apply(self, ops=None, state=None, now=NOW, undo=False, exe="gws"):
        args = ["--mailbox", "user@example.com", "--state-dir", str(self.dir / "state")]
        args += ["--undo-last"] if undo else ["--batch", str(self.batch(ops, state))]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = applier.main(args, runner=self.fake, sleep=lambda s: None, exe=exe, now=now)
        return code, json.loads(buf.getvalue())

    def writes(self):
        return [(p, m) for (p, m, _, _) in self.fake.calls if (p, m) in applier.WRITE_CALLS]

    def labels_of(self, mid):
        return self.fake.messages[mid]["labelIds"]

    def label_named(self, name):
        return [l["id"] for l in self.fake.labels.values() if l["name"] == name]

    def state(self):
        return json.loads((self.dir / "state" / "user@example.com.json").read_text(encoding="utf-8"))

    def log(self):
        text = (self.dir / "state" / "user@example.com.log.jsonl").read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines()]


ARCHIVE_LIST = {"op": "modify", "groups": [LIST], "remove": ["INBOX"]}


class TestValidation(Harness):
    def assertRefused(self, ops, state=None, now=NOW):
        code, out = self.apply(ops, state, now)
        self.assertEqual(code, 3, out)
        self.assertEqual(self.writes(), [])
        return out

    def test_unknown_group(self):
        self.assertRefused([{"op": "modify", "groups": ["from:nobody@x.com"], "add": ["Bots/GitHub"]}])

    def test_unknown_label(self):
        self.assertRefused([{"op": "modify", "groups": [LIST], "add": ["Notify/GitHub"]}])

    # Protections are re-checked live at trash time, so no approved batch may drop one
    # for a later batch to exploit. Label_2 is Clients/Acme.
    def test_modify_cannot_remove_star_or_important(self):
        for lab in ("STARRED", "IMPORTANT"):
            with self.subTest(lab=lab):
                self.assertRefused([{"op": "modify", "groups": [NEWS], "remove": [lab]}])

    def test_modify_cannot_drop_a_protected_label(self):
        self.assertRefused([{"op": "modify", "from_label": "Clients/Acme", "add": ["Bots/GitHub"],
                             "remove": ["Clients/Acme"]}])

    def test_protected_label_cannot_be_renamed_out_of_protection(self):
        self.assertRefused([{"op": "label_rename", "id": "Label_2", "name": "Misc/Acme"}])

    def test_protected_label_cannot_be_deleted_bare(self):
        self.assertRefused([{"op": "label_delete", "id": "Label_2"}])

    def test_protected_label_can_move_within_protection(self):
        code, out = self.apply([{"op": "label_rename", "id": "Label_2", "name": "Clients/AcmeCo"}])
        self.assertEqual(code, 0, out)

    def test_protected_label_can_be_merged_then_deleted(self):
        code, out = self.apply([
            {"op": "label_create", "name": "Clients/AcmeCo"},
            {"op": "modify", "from_label": "Clients/Acme", "add": ["Clients/AcmeCo"], "remove": ["Clients/Acme"]},
            {"op": "label_delete", "id": "Label_2"},
        ])
        self.assertEqual(code, 0, out)

    def test_partial_move_does_not_license_deleting_a_protected_label(self):
        # groups covers only some of Clients/Acme's mail; deleting it would strip the rest.
        self.assertRefused([
            {"op": "label_create", "name": "Clients/AcmeCo"},
            {"op": "modify", "groups": [NEWS], "add": ["Clients/AcmeCo"], "remove": ["Clients/Acme"]},
            {"op": "label_delete", "id": "Label_2"},
        ])

    def test_merge_target_cannot_be_deleted_in_the_same_batch(self):
        self.fake.labels["Label_9"] = {"id": "Label_9", "name": "Team", "type": "user"}
        self.scan()
        self.assertRefused([
            {"op": "modify", "from_label": "Clients/Acme", "add": ["Team"], "remove": ["Clients/Acme"]},
            {"op": "label_delete", "id": "Label_2"},
            {"op": "label_delete", "id": "Label_9"},
        ])

    def test_label_that_receives_mail_after_merging_out_cannot_be_deleted(self):
        self.fake.labels["Label_9"] = {"id": "Label_9", "name": "Team", "type": "user"}
        self.scan()
        self.assertRefused([
            {"op": "label_create", "name": "Clients/AcmeCo"},
            {"op": "modify", "from_label": "Team", "add": ["Clients/AcmeCo"], "remove": ["Team"]},
            {"op": "modify", "from_label": "Clients/Acme", "add": ["Team"], "remove": ["Clients/Acme"]},
            {"op": "label_delete", "id": "Label_9"},
        ])

    def test_protected_label_with_members_is_never_deleted_even_past_validation(self):
        # Runtime backstop: n5 still carries Clients/Acme (Label_2).
        with mock.patch.object(applier, "validate", lambda *a, **k: None):
            code, out = self.apply([{"op": "label_delete", "id": "Label_2"}])
        self.assertNotEqual(code, 0, out)
        self.assertIn("Label_2", self.fake.labels)
        self.assertEqual(self.writes(), [])

    def test_trash_must_be_alone_in_its_batch(self):
        # An earlier op could strip a protection (e.g. STARRED) that trash re-checks live.
        out = self.assertRefused([{"op": "modify", "groups": [NEWS], "remove": ["STARRED"]},
                                  {"op": "trash", "groups": [NEWS]}])
        self.assertIn("own batch", out["error"])

    def test_filter_cannot_forward_to_a_new_address(self):
        self.assertRefused([{"op": "filter_create", "criteria": {"query": "*"},
                             "action": {"add": ["Bots/GitHub"], "forward": "evil@attacker.test"}}])

    def test_forward_allowlist_comes_from_live_filters_not_the_summary(self):
        # The summary file is named by the batch, so it must not vouch for an address.
        self.fake.filters["F1"] = {"id": "F1", "criteria": {"from": "bot@github.com"},
                                   "action": {"forward": "evil@attacker.test"}}
        self.scan()
        del self.fake.filters["F1"]
        self.assertRefused([{"op": "filter_create", "criteria": {"query": "*"},
                             "action": {"add": ["Bots/GitHub"], "forward": "evil@attacker.test"}}])

    def test_filter_refuses_unknown_action_keys(self):
        self.assertRefused([{"op": "filter_create", "criteria": {"from": "bot@github.com"},
                             "action": {"add": ["Bots/GitHub"], "addLabelIds": ["TRASH"]}}])

    def test_filter_cannot_trash_or_spam(self):
        for action in ({"add": ["TRASH"]}, {"add": ["SPAM"]}, {"remove": ["SENT"]}):
            with self.subTest(action=action):
                self.assertRefused([{"op": "filter_create", "criteria": {"query": "*"}, "action": action}])

    def test_label_created_earlier_in_batch_resolves(self):
        code, out = self.apply([{"op": "label_create", "name": "Notify/GitHub"},
                                {"op": "modify", "groups": [LIST], "add": ["Notify/GitHub"]}])
        self.assertEqual(code, 0, out)

    def test_renamed_away_name_no_longer_resolves(self):
        self.assertRefused([{"op": "label_rename", "id": "Label_1", "name": "Notify/GitHub"},
                            {"op": "modify", "groups": [LIST], "add": ["Bots/GitHub"]}])

    def test_system_labels_untouchable(self):
        self.assertRefused([{"op": "modify", "groups": [LIST], "add": ["TRASH"]}])
        self.assertRefused([{"op": "modify", "groups": [LIST], "remove": ["SENT"]}])

    def test_system_label_rename_refused(self):
        self.assertRefused([{"op": "label_rename", "id": "INBOX", "name": "X"}])

    def test_groups_xor_from_label(self):
        self.assertRefused([{"op": "modify", "groups": [LIST], "from_label": "Bots/GitHub", "add": ["Clients/Acme"]}])

    def test_empty_batch_refused(self):
        self.assertRefused([])

    def test_stale_scan(self):
        self.assertRefused([ARCHIVE_LIST], now=NOW + timedelta(hours=2))

    def test_mailbox_mismatch(self):
        self.fake.mailbox = "other@example.com"
        self.assertRefused([ARCHIVE_LIST])

    def test_trash_blocked_by_scan_errors(self):
        p = self.dir / "scan" / "summary.json"
        s = json.loads(p.read_text(encoding="utf-8"))
        s["errors"] = 1
        p.write_text(json.dumps(s), encoding="utf-8")
        self.assertRefused([{"op": "trash", "groups": [NEWS]}])

    def test_bad_rule(self):
        self.assertRefused([ARCHIVE_LIST], state={"rules_add": [
            {"match": {"from": "a@b.com", "domain": "b.com"}, "label": "X", "archive": True}]})

    def test_invalid_state_file_untouched(self):
        sp = self.dir / "state" / "user@example.com.json"
        sp.parent.mkdir(parents=True)
        sp.write_text("{oops", encoding="utf-8")
        self.assertRefused([ARCHIVE_LIST])
        self.assertEqual(sp.read_text(encoding="utf-8"), "{oops")

    def test_cmd_shim_metachar_refused_before_any_write(self):
        code, out = self.apply([{"op": "label_rename", "id": "Label_1", "name": "Notify/GitHub"},
                                {"op": "label_create", "name": "R&D"}], exe="gws.cmd")
        self.assertEqual(code, 3, out)
        self.assertEqual(self.writes(), [])

    def test_validation_error_after_a_write_is_not_exit_3(self):
        self.fake.fail("gmail.users.labels", "create", 3, "bad argument")
        code, out = self.apply([{"op": "label_rename", "id": "Label_1", "name": "Notify/GitHub"},
                                {"op": "label_create", "name": "News"}])
        self.assertEqual(code, 1, out)

    def test_gws_call_refuses_calls_outside_allowlists(self):
        gm = applier.Gmail("gws", runner=self.fake)
        before = len(self.fake.calls)
        for path, method in [("gmail.users.messages", "delete"), ("gmail.users.messages", "batchDelete"),
                             ("gmail.users.messages", "send"), ("gmail.users.drafts", "create"),
                             ("gmail.users.threads", "trash")]:
            with self.assertRaises(ValueError):
                applier.gws_call(gm, path, method, {"id": "g1"})
        self.assertEqual(len(self.fake.calls), before)


class TestModify(Harness):
    def test_label_and_archive_group(self):
        code, out = self.apply([{"op": "modify", "groups": [LIST], "add": ["Bots/GitHub"], "remove": ["INBOX"]}])
        self.assertEqual(code, 0, out)
        for mid in ("g1", "g2", "g3"):
            self.assertIn("Label_1", self.labels_of(mid))
            self.assertNotIn("INBOX", self.labels_of(mid))
        r = out["results"][0]
        self.assertEqual((r["requested"], r["applied"], r["verified"], r["status"]), (3, 2, 2, "ok"))
        self.assertEqual(r["skipped"], 1)  # g3 already labeled and archived

    def test_log_records_only_real_changes(self):
        self.apply([{"op": "modify", "groups": [LIST], "add": ["Bots/GitHub"], "remove": ["INBOX"]}])
        added, removed = {}, {}
        for e in self.log():
            for l, ids in e.get("added", {}).items():
                added.setdefault(l, []).extend(ids)
            for l, ids in e.get("removed", {}).items():
                removed.setdefault(l, []).extend(ids)
        self.assertEqual(sorted(added["Label_1"]), ["g1", "g2"])
        self.assertEqual(sorted(removed["INBOX"]), ["g1", "g2"])

    def test_chunking(self):
        old = applier.CHUNK
        applier.CHUNK = 2
        try:
            self.apply([{"op": "modify", "groups": [NEWS], "remove": ["INBOX"]}])
        finally:
            applier.CHUNK = old
        sizes = [len(b["ids"]) for (p, m, _, b) in self.fake.calls if m == "batchModify"]
        self.assertEqual(sizes, [2, 2, 1])

    def test_vanished_id_dropped_and_rest_applied(self):
        del self.fake.messages["n1"]
        code, out = self.apply([{"op": "modify", "groups": [NEWS], "remove": ["INBOX"]}])
        self.assertEqual(code, 0, out)
        self.assertEqual(out["results"][0]["vanished"], 1)
        self.assertNotIn("INBOX", self.labels_of("n2"))

    def test_verification_mismatch_stops_and_skips_state(self):
        self.fake._gmail_users_messages__batchModify = lambda p, b: None  # accepts, changes nothing
        code, out = self.apply([ARCHIVE_LIST, {"op": "label_create", "name": "News"}], state={"finalize": True})
        self.assertEqual(code, 1)
        self.assertEqual(out["results"][0]["status"], "mismatch")
        self.assertEqual(len(out["results"]), 1)
        self.assertFalse(out["state_written"])
        self.assertFalse((self.dir / "state" / "user@example.com.json").exists())

    def test_from_label_merge(self):
        code, out = self.apply([
            {"op": "label_create", "name": "Notify/GitHub"},
            {"op": "modify", "from_label": "Bots/GitHub", "add": ["Notify/GitHub"], "remove": ["Bots/GitHub"]},
        ])
        self.assertEqual(code, 0, out)
        new = self.label_named("Notify/GitHub")[0]
        self.assertIn(new, self.labels_of("g3"))
        self.assertNotIn("Label_1", self.labels_of("g3"))


class TestState(Harness):
    RULE = {"match": {"list_id": "r.github.com"}, "label": "Bots/GitHub", "archive": True}

    def test_rules_dedupe_and_finalize(self):
        self.apply([ARCHIVE_LIST], state={"rules_add": [self.RULE]})
        s = self.state()
        self.assertEqual((s["rules"], s["last_run"], s["trash_min_age_days"]), ([self.RULE], None, 90))
        self.scan()
        rule2 = dict(self.RULE, label="Notify/GitHub")
        self.apply([{"op": "modify", "groups": [LIST], "add": ["Bots/GitHub"]}],
                   state={"rules_add": [rule2], "finalize": True})
        s = self.state()
        self.assertEqual(s["rules"], [rule2])
        self.assertEqual(s["last_run"], "2026-10-05T10:00:00Z")

    def test_finalize_only_batch(self):
        code, out = self.apply([], state={"finalize": True})
        self.assertEqual(code, 0, out)
        self.assertEqual(self.state()["last_run"], "2026-10-05T10:00:00Z")
        self.assertEqual(self.writes(), [])


class TestLabelOps(Harness):
    def test_create_with_color_and_skip_existing(self):
        code, out = self.apply([
            {"op": "label_create", "name": "@Action", "color": {"backgroundColor": "#fb4c2f", "textColor": "#ffffff"}},
            {"op": "label_create", "name": "Bots/GitHub"},
        ])
        self.assertEqual(code, 0, out)
        made = self.fake.labels[self.label_named("@Action")[0]]
        self.assertEqual(made["color"]["backgroundColor"], "#fb4c2f")
        self.assertEqual(made["labelListVisibility"], "labelShow")
        self.assertEqual([r["skipped"] for r in out["results"]], [0, 1])

    def test_rename_keeps_id_and_skips_when_already_named(self):
        code, out = self.apply([{"op": "label_rename", "id": "Label_1", "name": "Notify/GitHub"},
                                {"op": "label_rename", "id": "Label_1", "name": "Notify/GitHub"}])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.fake.labels["Label_1"]["name"], "Notify/GitHub")
        self.assertEqual([r["applied"] for r in out["results"]], [1, 0])
        self.assertIn("Label_1", self.labels_of("g3"))

    def test_delete_logs_members(self):
        code, out = self.apply([{"op": "label_delete", "id": "Label_1"}])
        self.assertEqual(code, 0, out)
        self.assertNotIn("Label_1", self.fake.labels)
        e = [e for e in self.log() if e["op"] == "label_delete"][0]
        self.assertEqual((e["name"], e["members"]), ("Bots/GitHub", ["g3"]))
        self.assertEqual(self.labels_of("g3"), [])


class TestFilterOps(Harness):
    CREATE = {"op": "filter_create", "criteria": {"from": "bot@github.com"},
              "action": {"add": ["Bots/GitHub"], "remove": ["INBOX"]}}

    def test_create_resolves_names_and_records_state(self):
        code, out = self.apply([self.CREATE])
        self.assertEqual(code, 0, out)
        f = list(self.fake.filters.values())[0]
        self.assertEqual(f["action"], {"addLabelIds": ["Label_1"], "removeLabelIds": ["INBOX"]})
        self.assertEqual(self.state()["filters_created"], [{"id": f["id"], "criteria": {"from": "bot@github.com"}}])

    def test_delete_logs_definition_and_updates_state(self):
        self.apply([self.CREATE])
        fid = list(self.fake.filters)[0]
        self.scan()
        code, out = self.apply([{"op": "filter_delete", "id": fid}])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.fake.filters, {})
        self.assertEqual(self.state()["filters_created"], [])
        e = [e for e in self.log() if e["op"] == "filter_delete"][0]
        self.assertEqual(e["criteria"], {"from": "bot@github.com"})

    def test_create_keeps_non_label_actions(self):
        # A merge carries an existing filter's forward over to the new filter.
        self.fake.filters["F1"] = {"id": "F1", "criteria": {"from": "bot@github.com"},
                                   "action": {"addLabelIds": ["Label_1"], "forward": "me@example.com"}}
        self.scan()
        code, out = self.apply([{"op": "filter_create", "criteria": {"from": "bot@github.com"},
                                 "action": {"add": ["Bots/GitHub"], "forward": "me@example.com"}}])
        self.assertEqual(code, 0, out)
        f = list(self.fake.filters.values())[0]
        self.assertEqual(f["action"]["forward"], "me@example.com")
        self.assertEqual(f["action"]["addLabelIds"], ["Label_1"])

    def test_missing_settings_scope_reports_api_error(self):
        self.fake.fail("gmail.users.settings.filters", "create", 1, "403 Request had insufficient authentication scopes.")
        code, out = self.apply([self.CREATE])
        self.assertEqual(code, 1)
        self.assertIn("insufficient authentication scopes", out["error"])
        self.assertFalse(out["state_written"])


class TestTrash(Harness):
    def trashed(self):
        return {mid for mid, m in self.fake.messages.items() if "TRASH" in m["labelIds"]}

    def test_protections(self):
        code, out = self.apply([{"op": "trash", "groups": [NEWS]}])
        self.assertEqual(code, 0, out)
        # n2 starred, n3 too new, n4 in a thread with a SENT reply, n5 under Clients/
        self.assertEqual(self.trashed(), {"n1"})
        r = out["results"][0]
        self.assertEqual((r["requested"], r["applied"], r["skipped"], r["status"]), (5, 1, 4, "ok"))

    def test_min_age_override(self):
        self.apply([{"op": "trash", "groups": [NEWS], "min_age_days": 1}])
        self.assertEqual(self.trashed(), {"n1", "n3"})

    def test_state_min_age_used_by_default(self):
        sp = self.dir / "state" / "user@example.com.json"
        sp.parent.mkdir(parents=True)
        sp.write_text(json.dumps({"version": 1, "mailbox": "user@example.com", "created": "2026-10-01",
                                  "last_run": None, "trash_min_age_days": 1, "rules": [],
                                  "filters_created": []}), encoding="utf-8")
        self.apply([{"op": "trash", "groups": [NEWS]}])
        self.assertEqual(self.trashed(), {"n1", "n3"})

    def test_trash_logged_per_chunk_so_a_crash_is_undoable(self):
        orig = self.fake._gmail_users_messages__trash

        def flaky(p, b):
            if p["id"] == "n1":  # eligible ids run newest first: n3, then n1
                raise OSError("subprocess died")
            return orig(p, b)

        self.fake._gmail_users_messages__trash = flaky
        old = applier.TRASH_CHUNK
        applier.TRASH_CHUNK = 1
        try:
            with self.assertRaises(OSError):
                self.apply([{"op": "trash", "groups": [NEWS], "min_age_days": 1}])
        finally:
            applier.TRASH_CHUNK = old
        self.fake._gmail_users_messages__trash = orig
        self.assertIn("TRASH", self.labels_of("n3"))
        code, out = self.apply(undo=True)
        self.assertEqual(code, 0, out)
        self.assertNotIn("TRASH", self.labels_of("n3"))

    def test_trash_never_deletes(self):
        self.apply([{"op": "trash", "groups": [NEWS]}])
        methods = {m for (p, m, _, _) in self.fake.calls if p == "gmail.users.messages"}
        self.assertIn("trash", methods)
        self.assertFalse(methods & {"delete", "batchDelete"})
        self.assertIn("n1", self.fake.messages)


class TestUndo(Harness):
    def names(self):
        return {l["name"] for l in self.fake.labels.values()}

    def test_nothing_to_undo(self):
        code, out = self.apply(undo=True)
        self.assertEqual(code, 3, out)

    def test_undo_modify_restores_exact_prior_state(self):
        before = {m: sorted(self.labels_of(m)) for m in ("g1", "g2", "g3")}
        self.apply([{"op": "modify", "groups": [LIST], "add": ["Bots/GitHub"], "remove": ["INBOX"]}])
        code, out = self.apply(undo=True)
        self.assertEqual(code, 0, out)
        for m, labels in before.items():
            self.assertEqual(sorted(self.labels_of(m)), labels)

    def test_undo_trash(self):
        self.apply([{"op": "trash", "groups": [NEWS]}])
        self.assertIn("TRASH", self.labels_of("n1"))
        code, out = self.apply(undo=True)
        self.assertEqual(code, 0, out)
        self.assertNotIn("TRASH", self.labels_of("n1"))

    def test_undo_label_ops_and_filters(self):
        code, out = self.apply([
            {"op": "label_create", "name": "Notify/GitHub"},
            {"op": "label_rename", "id": "Label_2", "name": "Clients/Acme Corp"},
            {"op": "label_delete", "id": "Label_1"},
            {"op": "filter_create", "criteria": {"from": "x@y.com"}, "action": {"add": ["Notify/GitHub"]}},
        ])
        self.assertEqual(code, 0, out)
        code, out = self.apply(undo=True)
        self.assertEqual(code, 0, out)
        self.assertNotIn("Notify/GitHub", self.names())
        self.assertEqual(self.fake.labels["Label_2"]["name"], "Clients/Acme")
        restored = self.label_named("Bots/GitHub")
        self.assertEqual(len(restored), 1)
        self.assertIn(restored[0], self.labels_of("g3"))
        self.assertEqual(self.fake.filters, {})

    def test_undo_restores_state_then_steps_back(self):
        rule = {"match": {"list_id": "r.github.com"}, "label": "Bots/GitHub", "archive": True}
        rule2 = {"match": {"from": "news@shop.com"}, "label": "News", "archive": True}
        self.apply([ARCHIVE_LIST], state={"rules_add": [rule], "finalize": True})
        self.scan()
        self.apply([{"op": "label_create", "name": "News"}], state={"rules_add": [rule2]})
        self.assertEqual(self.state()["rules"], [rule, rule2])

        self.assertEqual(self.apply(undo=True)[0], 0)
        self.assertEqual(self.state()["rules"], [rule])
        self.assertNotIn("News", self.names())

        self.assertEqual(self.apply(undo=True)[0], 0)
        s = self.state()
        self.assertEqual((s["rules"], s["last_run"]), ([], None))
        self.assertIn("INBOX", self.labels_of("g1"))

        self.assertEqual(self.apply(undo=True)[0], 3)

    def test_undo_merge_translates_recreated_label_ids(self):
        self.fake.filters["F1"] = {"id": "F1", "criteria": {"from": "bot@github.com"},
                                   "action": {"addLabelIds": ["Label_1"], "removeLabelIds": ["INBOX"]}}
        self.scan()
        code, out = self.apply([
            {"op": "label_create", "name": "Notify/GitHub"},
            {"op": "modify", "from_label": "Bots/GitHub", "add": ["Notify/GitHub"], "remove": ["Bots/GitHub"]},
            {"op": "filter_delete", "id": "F1"},
            {"op": "filter_create", "criteria": {"from": "bot@github.com"},
             "action": {"add": ["Notify/GitHub"], "remove": ["INBOX"]}},
            {"op": "label_delete", "id": "Label_1"},
        ])
        self.assertEqual(code, 0, out)
        code, out = self.apply(undo=True)
        self.assertEqual(code, 0, out)
        src = self.label_named("Bots/GitHub")[0]
        self.assertIn(src, self.labels_of("g3"))
        self.assertEqual(self.label_named("Notify/GitHub"), [])
        self.assertEqual([f["action"]["addLabelIds"] for f in self.fake.filters.values()], [[src]])

    def test_undo_across_batches_after_label_delete(self):
        self.apply([{"op": "modify", "groups": [LIST], "add": ["Bots/GitHub"]}])
        self.scan()
        self.apply([{"op": "label_delete", "id": "Label_1"}])
        self.assertEqual(self.apply(undo=True)[0], 0)
        code, out = self.apply(undo=True)
        self.assertEqual(code, 0, out)
        src = self.label_named("Bots/GitHub")[0]
        self.assertNotIn(src, self.labels_of("g1"))
        self.assertIn(src, self.labels_of("g3"))

    def test_undo_skips_labels_deleted_outside_the_skill(self):
        self.apply([{"op": "label_create", "name": "News"},
                    {"op": "modify", "groups": [NEWS], "add": ["News"]}])
        del self.fake.labels[self.label_named("News")[0]]
        code, out = self.apply(undo=True)
        self.assertEqual(code, 0, out)

    def test_interrupted_batch_is_undoable(self):
        self.fake.fail("gmail.users.labels", "create", 1, "500 backend error")
        code, out = self.apply([ARCHIVE_LIST, {"op": "label_create", "name": "News"}])
        self.assertEqual(code, 1)
        self.assertFalse(out["state_written"])
        self.assertNotIn("INBOX", self.labels_of("g1"))
        code, out = self.apply(undo=True)
        self.assertEqual(code, 0, out)
        self.assertIn("INBOX", self.labels_of("g1"))
        self.assertIn("INBOX", self.labels_of("g2"))
        self.assertNotIn("INBOX", self.labels_of("g3"))

    def test_failed_undo_can_be_retried(self):
        self.apply([ARCHIVE_LIST])
        self.fake.fail("gmail.users.messages", "batchModify", 1, "500 backend error")
        self.assertEqual(self.apply(undo=True)[0], 1)
        code, out = self.apply(undo=True)
        self.assertEqual(code, 0, out)
        self.assertIn("INBOX", self.labels_of("g1"))


if __name__ == "__main__":
    unittest.main()
