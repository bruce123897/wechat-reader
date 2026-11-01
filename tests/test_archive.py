import tempfile
import unittest
from pathlib import Path

from wechat_reader.archive import Archive

BASE = Path(__file__).resolve().parents[1] / "data" / "tests"
BASE.mkdir(parents=True, exist_ok=True)


def msg(local_id=1, ts=100, **kw):
    return dict(chat="room", local_id=local_id, ts=ts, sender="u", kind="text", text="合成 hello", **kw)


def batch(messages=(), account="a", source="s", **kw):
    return dict(account=account, source=source, cursor="1", messages=list(messages), **kw)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=BASE)
        self.path = Path(self.tmp.name) / "archive.db"
        self.a = Archive(self.path)

    def tearDown(self):
        self.a.close()
        self.tmp.cleanup()

    def test_identity_idempotency_and_updates(self):
        for a, s in (("a", "s"), ("b", "s"), ("a", "s2")):
            self.a.ingest(batch([msg()], a, s))
            self.assertEqual(self.a.ingest(batch([msg()], a, s))["unchanged"], 1)
        self.assertEqual(self.a.status()["messages"], 3)
        m = msg(); m["text"] = "changed"
        self.assertEqual(self.a.ingest(batch([m]))["updated"], 1)
        self.assertEqual(self.a.status()["versions"], 4)
        self.assertEqual(self.a.query(account="a", keyword="changed")["messages"][0]["shard"], "s")

    def test_recall_only_preserves_captured_text(self):
        self.a.ingest(batch([msg()]))
        recalled = msg(); recalled.update(kind="recall", text="已撤回", recalled=True)
        self.a.ingest(batch([recalled]))
        stored = self.a.message(1)
        self.assertEqual(stored["text"], "合成 hello")
        self.assertTrue(stored["recalled"])
        self.a.ingest(batch([recalled]))
        self.assertEqual(self.a.status()["versions"], 2)
        recalled["local_id"] = 2
        self.a.ingest(batch([recalled]))
        self.assertEqual(self.a.message(2)["text"], "已撤回")
        # A stale later source cannot replace an initially unknown recall.
        stale = msg(2); stale["text"] = "not captured before recall"
        self.a.ingest(batch([stale]))
        self.assertEqual(self.a.message(2)["text"], "已撤回")

    def test_separate_recall_event_targets_only_its_account_and_chat(self):
        original = msg(server_id="99")
        self.a.ingest(batch([original]))
        self.a.ingest(batch([original], account="b"))
        event = msg(2, 101); event.update(kind="recall", text="已撤回", recalled=True, extra={"target_server_id": "99", "target_chat": "room"})
        self.assertEqual(self.a.ingest(batch([event]))["updated"], 1)
        self.assertTrue(self.a.message(1)["recalled"])
        self.assertEqual(self.a.message(1)["text"], original["text"])
        self.assertFalse(self.a.message(2)["recalled"])
        self.assertEqual(self.a.new("a")["messages"][-1]["id"], 1)
        # Event observed before original content: never claim to recover it later.
        late = msg(3, server_id="100")
        event["local_id"] = 4; event["extra"]["target_server_id"] = "100"
        self.a.ingest(batch([event]))
        self.a.ingest(batch([late]))
        latest = self.a.query(keyword="原文未采集")["messages"]
        self.assertEqual(len(latest), 1)
        self.assertTrue(latest[0]["recalled"])

    def test_original_first_seen_with_recall_is_not_retained_as_captured(self):
        original = msg(server_id="99")
        event = msg(2, 101); event.update(kind="recall",text="已撤回",recalled=True,extra={"target_server_id":"99"})
        self.a.ingest(batch([original, event]))
        self.assertEqual(self.a.query(keyword=original["text"])["messages"], [])
        self.assertFalse(any(m["text"] == original["text"] for m in self.a.new("a")["messages"]))

    def test_precise_user_id_wins_over_another_nickname(self):
        self.a.ingest(batch(contacts=[dict(user="u",nick="same"),dict(user="v",nick="u")]))
        self.assertEqual(self.a.resolve("a", "u"), "u")

    def test_stable_pagination_across_insert_and_update(self):
        self.a.ingest(batch([msg(i, 100+i//2) for i in range(8)]))
        first = self.a.query(limit=3)
        expected = self.a.query(limit=100)["messages"]
        changed = msg(5, 10); changed["text"] = "moved"
        self.a.ingest(batch([msg(99, 99), changed]))
        result = first["messages"]
        cursor = first["next_cursor"]
        while cursor:
            p = self.a.query(limit=2, cursor=cursor)
            result.extend(p["messages"]); cursor = p["next_cursor"]
        self.assertEqual(result, expected)
        with self.assertRaises(ValueError):
            self.a.query(keyword="different", cursor=first["next_cursor"])

    def test_time_boundaries_literal_search_and_context(self):
        self.a.ingest(batch([msg(i, i) for i in range(1, 6)]))
        self.assertEqual([m["ts"] for m in self.a.query(since=2, until=4)["messages"]], [2, 3])
        self.assertEqual(self.a.query(keyword="%_")["messages"], [])
        self.assertEqual([m["id"] for m in self.a.context(3, 1)["messages"]], [2, 3, 4])
        self.a.ingest(batch([msg(1, 3)], "b"))
        self.assertEqual(len(self.a.context(3, 100)["messages"]), 5)

    def test_interruption_rollback_resume_and_reverse_doctor(self):
        messages = [msg(i, i) for i in range(20)]
        with self.assertRaisesRegex(RuntimeError, "controlled"):
            self.a.ingest(batch(messages), fail_after=7)
        self.assertEqual(self.a.status()["messages"], 0)
        self.assertIsNone(self.a.db.execute("SELECT cursor FROM sync_state").fetchone()[0])
        self.assertFalse(self.a.doctor()["ok"])
        self.a.ingest(batch(messages))
        self.assertTrue(self.a.doctor()["ok"])
        with Archive(Path(self.tmp.name)/"complete.db") as other:
            other.ingest(batch(messages))
            self.assertEqual(self.a.query(limit=100)["messages"], other.query(limit=100)["messages"])

    def test_query_while_sync_and_readonly_connection(self):
        self.a.ingest(batch([msg()]))
        with Archive(self.path, readonly=True) as reader:
            reader.db.execute("BEGIN")
            self.assertEqual(len(reader.query()["messages"]), 1)
            self.a.ingest(batch([msg(2)]))
            self.assertEqual(len(reader.query()["messages"]), 1)
            reader.db.rollback()
            self.assertEqual(len(reader.query()["messages"]), 2)
            with self.assertRaises(Exception):
                reader.db.execute("DELETE FROM messages")

    def test_contacts_ambiguity_members_sessions_and_reader(self):
        contacts = [dict(user=u, remark="same", nick="nick"+u) for u in ("u", "v")]
        self.a.ingest(batch([msg()], contacts=contacts, members=[dict(room="room", user="u", nick="group name")], sessions=[dict(chat="room", unread=2)]))
        self.assertEqual(len(self.a.find("same")), 2)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.a.resolve("a", "same")
        self.assertEqual(self.a.members("a", "room")[0]["nick"], "group name")
        self.assertEqual(self.a.inbox(unread=True)[0]["unread"], 2)
        self.a.ingest(batch([msg(2, 1), msg(3, 2)]))
        self.assertEqual(len(self.a.new("a", limit=1, consume=True)["messages"]), 1)
        self.assertEqual(len(self.a.new("a")["messages"]), 2)
        self.assertEqual(len(self.a.new("b")["messages"]), 0)
        self.a.ingest(batch(member_rooms=["room"]))
        self.assertEqual(self.a.members("a", "room"), [])

    def test_cross_account_failure_and_long_message(self):
        m = msg(); m["account"] = "wrong"
        with self.assertRaises(ValueError):
            self.a.ingest(batch([m]))
        m.pop("account"); m["text"] = "长"*100000
        self.a.ingest(batch([m]))
        self.assertEqual(len(self.a.message(1)["text"]), 100000)


if __name__ == "__main__":
    unittest.main()
