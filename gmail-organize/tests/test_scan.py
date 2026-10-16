import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))
import scan  # noqa: E402
from fakegmail import FakeGmail, msg  # noqa: E402

DAY = 86400000
T0 = 1750000000000  # 2025-06-15 UTC


class TestParsing(unittest.TestCase):
    def test_list_id_angle_brackets(self):
        self.assertEqual(scan.parse_list_id('"Dev News" <News.Example.COM>'), "news.example.com")

    def test_list_id_bare_and_empty(self):
        self.assertEqual(scan.parse_list_id(" news.example.com "), "news.example.com")
        self.assertIsNone(scan.parse_list_id(""))

    def test_sender_lowercases_and_splits_domain(self):
        self.assertEqual(scan.sender(msg("1", "Ann <Ann@Example.COM>")), ("ann@example.com", "example.com"))

    def test_malformed_from_groups_as_unknown(self):
        self.assertEqual(scan.group_key(msg("1", "")), ("from:(unknown)", "from"))
        self.assertEqual(scan.group_key(msg("2", "undisclosed-recipients:;")), ("from:(unknown)", "from"))

    def test_list_id_beats_from(self):
        self.assertEqual(scan.group_key(msg("1", "a@x.com", list_id="<l.x.com>")), ("list:l.x.com", "list_id"))


class TestSummarize(unittest.TestCase):
    def setUp(self):
        self.ms = [
            msg("1", "Bot <bot@github.com>", "PR opened", ["INBOX", "UNREAD", "CATEGORY_UPDATES"], "t1", T0, list_id="<repo.github.com>"),
            msg("2", "bot@github.com", "PR merged", ["INBOX", "CATEGORY_UPDATES"], "t2", T0 + DAY, list_id="<repo.github.com>"),
            msg("3", "bot@github.com", "PR merged", ["INBOX"], "t3", T0 + 2 * DAY, list_id="<repo.github.com>"),
            msg("4", "Ann <ann@acme.com>", "Quote", ["INBOX"], "t4", T0),
            msg("5", "me@example.com", "Re: Quote", ["SENT"], "t4", T0 + DAY),
            msg("6", "me@example.com", "draft", ["DRAFT"], "t6", T0),
            msg("7", "news@acme.com", "Weekly", ["INBOX"], "t7", T0, unsubscribe="<mailto:u@acme.com>"),
        ]
        self.groups, self.domains, self.ids, self.labels = scan.summarize(self.ms)
        self.by_key = {g["key"]: g for g in self.groups}

    def test_own_sent_and_drafts_never_grouped(self):
        self.assertNotIn("from:me@example.com", self.by_key)
        self.assertNotIn("5", self.labels)
        self.assertNotIn("6", self.labels)

    def test_list_group_counts_and_dates(self):
        g = self.by_key["list:repo.github.com"]
        self.assertEqual((g["count"], g["unread"], g["list_mail"]), (3, 1, 3))
        self.assertEqual((g["oldest"], g["newest"]), ("2025-06-15", "2025-06-17"))
        self.assertEqual(g["categories"], {"CATEGORY_UPDATES": 2})
        self.assertEqual(g["label_ids"], {"INBOX": 3})
        self.assertEqual(g["sample_subjects"], ["PR merged", "PR opened"])
        self.assertEqual((g["from"], g["domain"], g["kind"]), ("bot@github.com", "github.com", "list_id"))

    def test_has_sent_in_thread(self):
        self.assertEqual(self.by_key["from:ann@acme.com"]["has_sent_in_thread"], 1)
        self.assertEqual(self.by_key["list:repo.github.com"]["has_sent_in_thread"], 0)

    def test_unsubscribe_marks_list_mail_without_list_id(self):
        g = self.by_key["from:news@acme.com"]
        self.assertEqual((g["kind"], g["list_mail"]), ("from", 1))

    def test_sorted_by_count_and_domain_rollup(self):
        self.assertEqual(self.groups[0]["key"], "list:repo.github.com")
        rollup = {d["domain"]: (d["count"], d["groups"]) for d in self.domains}
        self.assertEqual(rollup, {"github.com": (3, 1), "acme.com": (2, 2)})

    def test_ids_and_scan_time_labels(self):
        self.assertEqual(sorted(self.ids["list:repo.github.com"]), ["1", "2", "3"])
        self.assertEqual(self.labels["1"], ["INBOX", "UNREAD", "CATEGORY_UPDATES"])


class TestScan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        mail = [msg(str(i), "bot@github.com", "n%d" % i, ts_ms=T0 + i) for i in range(5)]
        mail.append(msg("s", "me@example.com", labels=["SENT"]))
        self.fake = FakeGmail(messages=mail, labels=[{"id": "Label_1", "name": "Bots/GitHub"}],
                              filters=[{"id": "F1", "criteria": {"from": "x"}, "action": {}}])
        self.fake.messages["0"]["labelIds"].append("Label_1")

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, *args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return scan.main(["--out", str(self.out), *args], runner=self.fake, sleep=lambda s: None, exe="gws")

    def summary(self):
        return json.loads((self.out / "summary.json").read_text(encoding="utf-8"))

    def list_calls(self):
        return [p for (path, m, p, b) in self.fake.calls if path == "gmail.users.messages" and m == "list"]

    def test_writes_summary_and_ids(self):
        self.assertEqual(self.run_main(), 0)
        s = self.summary()
        ids = json.loads((self.out / "ids.json").read_text(encoding="utf-8"))
        self.assertEqual((s["mailbox"], s["total"], s["errors"]), ("user@example.com", 6, 0))
        self.assertEqual(s["groups"][0]["count"], 5)
        self.assertEqual(sorted(ids["groups"]["from:bot@github.com"]), ["0", "1", "2", "3", "4"])
        self.assertEqual(ids["mailbox"], "user@example.com")
        self.assertIn("Label_1", ids["labels"]["0"])
        user = [l for l in s["labels"] if l["type"] == "user"]
        self.assertEqual(user, [{"id": "Label_1", "name": "Bots/GitHub", "type": "user", "messages": 1}])
        self.assertEqual(s["filters"][0]["id"], "F1")

    def test_snippets_never_written(self):
        self.run_main()
        for name in ("summary.json", "ids.json"):
            self.assertNotIn("secret snippet", (self.out / name).read_text(encoding="utf-8"))

    def test_only_metadata_requested(self):
        self.run_main()
        gets = [p for (path, m, p, b) in self.fake.calls if path == "gmail.users.messages" and m == "get"]
        self.assertTrue(gets)
        self.assertTrue(all(p["format"] == "metadata" for p in gets))

    def test_pagination(self):
        old = scan.PAGE_SIZE
        scan.PAGE_SIZE = 2
        try:
            self.run_main()
        finally:
            scan.PAGE_SIZE = old
        self.assertEqual(len(self.list_calls()), 3)
        self.assertEqual(self.summary()["total"], 6)

    def test_since_becomes_after_epoch(self):
        self.run_main("--since", "2026-10-05T10:00:00Z")
        expected = int(datetime(2026, 10, 5, 10, tzinfo=timezone.utc).timestamp())
        self.assertEqual(self.list_calls()[0]["q"], "after:%d" % expected)

    def test_max_truncates(self):
        self.run_main("--max", "2")
        self.assertEqual(self.summary()["total"], 2)

    def test_empty_mailbox(self):
        self.fake.messages.clear()
        self.assertEqual(self.run_main("--since", "2026-10-05T10:00:00Z"), 0)
        s = self.summary()
        self.assertEqual((s["total"], s["groups"], s["domains"]), (0, [], []))

    def test_mailbox_mismatch_exits_3_and_writes_nothing(self):
        self.assertEqual(self.run_main("--expect-mailbox", "other@example.com"), 3)
        self.assertFalse((self.out / "summary.json").exists())

    def test_fetch_error_counted(self):
        self.fake.fail("gmail.users.messages", "get", 1, "500 backend error")
        self.assertEqual(self.run_main(), 0)
        self.assertEqual((self.summary()["total"], self.summary()["errors"]), (6, 1))
        self.assertIn("500 backend error", self.summary()["error_sample"])

    def test_rate_limit_retried_not_counted(self):
        self.fake.fail("gmail.users.messages", "get", 1, "429 rateLimitExceeded")
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.summary()["errors"], 0)

    def test_bad_since_exits_3(self):
        self.assertEqual(self.run_main("--since", "null"), 3)
        self.assertFalse((self.out / "summary.json").exists())

    def test_auth_error_aborts(self):
        self.fake.fail("gmail.users.messages", "get", 2, "auth expired")
        self.assertEqual(self.run_main(), 2)
        self.assertFalse((self.out / "summary.json").exists())


if __name__ == "__main__":
    unittest.main()
