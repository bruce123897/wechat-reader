"""Transactional archive and snapshot-consistent, keyset-paginated queries.

Source identity is (account, shard, chat, local_id), never server_id alone.
Version rows preserve both captured history and a stable view for pagination.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import time
from pathlib import Path


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
 id INTEGER PRIMARY KEY, account TEXT NOT NULL, shard TEXT NOT NULL,
 chat TEXT NOT NULL, local_id TEXT NOT NULL, server_id TEXT NOT NULL,
 ts INTEGER NOT NULL, sender TEXT NOT NULL, is_me INTEGER NOT NULL,
 kind TEXT NOT NULL, text TEXT NOT NULL, extra TEXT NOT NULL,
 recalled INTEGER NOT NULL, first_seq INTEGER, last_seq INTEGER, digest TEXT NOT NULL,
 UNIQUE(account,shard,chat,local_id));
CREATE TABLE IF NOT EXISTS versions(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL,
 account TEXT NOT NULL, chat TEXT NOT NULL, ts INTEGER NOT NULL,
 sender TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, payload TEXT NOT NULL,
 FOREIGN KEY(message_id) REFERENCES messages(id));
CREATE INDEX IF NOT EXISTS version_message ON versions(message_id,seq);
CREATE INDEX IF NOT EXISTS version_query ON versions(account,chat,ts,message_id);
CREATE TABLE IF NOT EXISTS sync_state(
 account TEXT NOT NULL, source TEXT NOT NULL, cursor TEXT,
 attempted REAL, succeeded REAL, error TEXT, PRIMARY KEY(account,source));
CREATE TABLE IF NOT EXISTS contacts(
 account TEXT NOT NULL,user TEXT NOT NULL,remark TEXT NOT NULL,nick TEXT NOT NULL,
 alias TEXT NOT NULL,kind TEXT NOT NULL,PRIMARY KEY(account,user));
CREATE TABLE IF NOT EXISTS members(
 account TEXT NOT NULL,room TEXT NOT NULL,user TEXT NOT NULL,nick TEXT NOT NULL,
 PRIMARY KEY(account,room,user));
CREATE TABLE IF NOT EXISTS sessions(
 account TEXT NOT NULL,chat TEXT NOT NULL,unread INTEGER NOT NULL,
 last_ts INTEGER NOT NULL,summary TEXT NOT NULL,PRIMARY KEY(account,chat));
CREATE TABLE IF NOT EXISTS readers(
 account TEXT NOT NULL,reader TEXT NOT NULL,seq INTEGER NOT NULL,
 PRIMARY KEY(account,reader));
PRAGMA user_version=1;
"""


class Archive:
    def __init__(self, path: Path | str, *, readonly=False):
        self.path = Path(path).resolve()
        self.readonly = readonly
        if readonly:
            self.db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=30)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(self.path, timeout=30)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.executescript(SCHEMA)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=30000")

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def watermark(self):
        return self.db.execute("SELECT coalesce(max(seq),0) FROM versions").fetchone()[0]

    def failure(self, account, source, error):
        # Store only a fixed, safe error code, never exception text containing paths or data.
        with self.db:
            self.db.execute("""INSERT INTO sync_state(account,source,attempted,error) VALUES(?,?,?,?)
             ON CONFLICT(account,source) DO UPDATE SET attempted=excluded.attempted,error=excluded.error""",
                            (account, source, time.time(), str(error)))

    def ingest(self, batch, *, fail_after=None):
        account, source = str(batch["account"]), str(batch["source"])
        result = {"inserted": 0, "updated": 0, "unchanged": 0}
        try:
            with self.db:
                for n, item in enumerate(batch.get("messages", [])):
                    if fail_after is not None and n == fail_after:
                        raise RuntimeError("controlled_batch_interruption")
                    m = self.normalize(item, account, source)
                    identity = tuple(m[k] for k in ("account", "shard", "chat", "local_id"))
                    old = self.db.execute("SELECT * FROM messages WHERE account=? AND shard=? AND chat=? AND local_id=?", identity).fetchone()
                    if old and (old["recalled"] or m["recalled"]):
                        m["recalled"] = True
                        if old["kind"] != "recall":
                            # Preserve only text actually captured before the source rewrite.
                            m.update(kind=old["kind"], text=old["text"], extra=json.loads(old["extra"]))
                        elif not m["text"]:
                            m["text"] = old["text"]
                    digest = hashlib.sha256(dumps(m).encode()).hexdigest()
                    if old and old["digest"] == digest:
                        result["unchanged"] += 1
                        continue
                    columns = tuple(m.keys())
                    values = [dumps(v) if k == "extra" else v for k, v in m.items()]
                    if old:
                        mid = old["id"]
                        self.db.execute("UPDATE messages SET " + ",".join(k + "=?" for k in columns) + ",digest=? WHERE id=?", (*values, digest, mid))
                        result["updated"] += 1
                    else:
                        mid = self.db.execute("INSERT INTO messages(" + ",".join(columns) + ",digest) VALUES(" + ",".join("?" for _ in range(len(columns)+1)) + ")", (*values, digest)).lastrowid
                        result["inserted"] += 1
                    payload = dict(m, id=mid)
                    seq = self.db.execute("INSERT INTO versions(message_id,account,chat,ts,sender,kind,text,payload) VALUES(?,?,?,?,?,?,?,?)", (mid, account, m["chat"], m["ts"], m["sender"], m["kind"], m["text"], dumps(payload))).lastrowid
                    self.db.execute("UPDATE messages SET first_seq=coalesce(first_seq,?),last_seq=? WHERE id=?", (seq, seq, mid))
                for c in batch.get("contacts", []):
                    self.db.execute("INSERT OR REPLACE INTO contacts VALUES(?,?,?,?,?,?)", (account, str(c["user"]), c.get("remark", ""), c.get("nick", ""), c.get("alias", ""), c.get("kind", "other")))
                # Explicit complete room list also clears rooms whose membership became empty.
                rooms = set(batch.get("member_rooms", [])) | {m["room"] for m in batch.get("members", [])}
                for room in rooms:
                    self.db.execute("DELETE FROM members WHERE account=? AND room=?", (account, room))
                for m in batch.get("members", []):
                    self.db.execute("INSERT OR REPLACE INTO members VALUES(?,?,?,?)", (account, m["room"], m["user"], m.get("nick", "")))
                for s in batch.get("sessions", []):
                    self.db.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?)", (account, s["chat"], int(s.get("unread", 0)), int(s.get("last_ts", 0)), s.get("summary", "")))
                now = time.time()
                self.db.execute("""INSERT INTO sync_state VALUES(?,?,?,?,?,NULL)
                 ON CONFLICT(account,source) DO UPDATE SET cursor=excluded.cursor,
                 attempted=excluded.attempted,succeeded=excluded.succeeded,error=NULL""",
                                (account, source, str(batch.get("cursor", "")), now, now))
        except Exception as exc:
            self.failure(account, source, type(exc).__name__)
            raise
        return result

    @staticmethod
    def normalize(item, account, source):
        if str(item.get("account", account)) != account:
            raise ValueError("batch_account_mismatch")
        m = {"account": account, "shard": str(item.get("shard", source)),
             "chat": str(item["chat"]), "local_id": str(item["local_id"]),
             "server_id": str(item.get("server_id", "")), "ts": int(item["ts"]),
             "sender": str(item.get("sender", "")), "is_me": bool(item.get("is_me", False)),
             "kind": str(item.get("kind", "other")), "text": str(item.get("text", "")),
             "extra": item.get("extra", {}), "recalled": bool(item.get("recalled", False))}
        if not m["account"] or not m["chat"] or not m["local_id"] or not isinstance(m["extra"], dict):
            raise ValueError("invalid_message_identity_or_extra")
        return m

    def query(self, *, account=None, chat=None, sender=None, kind=None,
              since=None, until=None, keyword=None, limit=50, cursor=None):
        """[since, until), literal case-insensitive substring, stable snapshot across pages."""
        if not 1 <= limit <= 1000:
            raise ValueError("limit_must_be_1_to_1000")
        filters = dict(account=account, chat=chat, sender=sender, kind=kind,
                       since=since, until=until, keyword=keyword)
        signature = hashlib.sha256(dumps(filters).encode()).hexdigest()
        mark = self.watermark()
        last = None
        if cursor:
            try:
                page = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                if page["filter"] != signature:
                    raise ValueError()
                mark, last = int(page["watermark"]), page["last"]
                if mark < 0 or mark > self.watermark() or len(last) != 2:
                    raise ValueError()
            except (ValueError, KeyError, TypeError, UnicodeError) as exc:
                raise ValueError("invalid_or_mismatched_cursor") from exc
        where, args = ["1=1"], [mark]
        for key in ("account", "chat", "sender", "kind"):
            if filters[key] is not None:
                where.append("v." + key + "=?")
                args.append(filters[key])
        if since is not None:
            where.append("v.ts>=?"); args.append(int(since))
        if until is not None:
            where.append("v.ts<?"); args.append(int(until))
        if keyword is not None:
            where.append("instr(lower(v.text),lower(?))>0"); args.append(keyword)
        if last:
            where.append("(v.ts,v.message_id)>(?,?)"); args.extend(last)
        rows = self.db.execute("""WITH latest AS(
          SELECT message_id,max(seq) AS seq FROM versions WHERE seq<=? GROUP BY message_id)
          SELECT v.* FROM versions v JOIN latest l ON v.seq=l.seq WHERE """ + " AND ".join(where) + " ORDER BY v.ts,v.message_id LIMIT ?", (*args, limit+1)).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        token = None
        if more:
            token = base64.urlsafe_b64encode(dumps({"filter": signature, "watermark": mark, "last": [rows[-1]["ts"], rows[-1]["message_id"]]}).encode()).decode()
        return {"messages": [dict(json.loads(r["payload"]), seq=r["seq"]) for r in rows], "next_cursor": token, "watermark": mark}

    def message(self, mid):
        row = self.db.execute("SELECT payload,seq FROM versions WHERE message_id=? ORDER BY seq DESC LIMIT 1", (mid,)).fetchone()
        if row is None:
            raise ValueError("message_not_found")
        return dict(json.loads(row["payload"]), seq=row["seq"])

    def context(self, mid, radius=2):
        if not 0 <= radius <= 100:
            raise ValueError("radius_must_be_0_to_100")
        # Single read transaction prevents anchor and surrounding rows seeing different syncs.
        self.db.execute("BEGIN")
        try:
            anchor = self.message(mid)
            query = """SELECT v.payload,v.seq FROM versions v JOIN messages m ON v.seq=m.last_seq
              WHERE m.account=? AND m.chat=? AND (m.ts,m.id) %s (?,?) ORDER BY m.ts %s,m.id %s LIMIT ?"""
            before = self.db.execute(query % ("<", "DESC", "DESC"), (anchor["account"], anchor["chat"], anchor["ts"], mid, radius)).fetchall()
            after = self.db.execute(query % (">", "ASC", "ASC"), (anchor["account"], anchor["chat"], anchor["ts"], mid, radius)).fetchall()
            return {"messages": [dict(json.loads(r["payload"]), seq=r["seq"]) for r in reversed(before)] + [dict(anchor, match=True)] + [dict(json.loads(r["payload"]), seq=r["seq"]) for r in after]}
        finally:
            self.db.rollback()

    def find(self, term="", account=None):
        return [dict(r) for r in self.db.execute("SELECT * FROM contacts WHERE (? IS NULL OR account=?) AND (instr(lower(user),lower(?)) OR instr(lower(remark),lower(?)) OR instr(lower(nick),lower(?)) OR instr(lower(alias),lower(?))) ORDER BY account,user", (account, account, term, term, term, term))]

    def resolve(self, account, name):
        rows = self.db.execute("SELECT user FROM contacts WHERE account=? AND (user=? OR remark=? OR nick=? OR alias=?)", (account, name, name, name, name)).fetchall()
        if len(rows) > 1:
            raise ValueError("ambiguous_contact_use_exact_user_id")
        return rows[0][0] if rows else name

    def members(self, account, room):
        return [dict(r) for r in self.db.execute("SELECT m.*,c.remark,c.nick AS contact_nick,c.alias FROM members m LEFT JOIN contacts c ON c.account=m.account AND c.user=m.user WHERE m.account=? AND m.room=? ORDER BY m.user", (account, room))]

    def inbox(self, account=None, unread=False):
        return [dict(r) for r in self.db.execute("SELECT s.*,c.remark,c.nick FROM sessions s LEFT JOIN contacts c ON c.account=s.account AND c.user=s.chat WHERE (? IS NULL OR s.account=?) AND (?=0 OR s.unread>0) ORDER BY s.last_ts DESC,s.account,s.chat", (account, account, unread))]

    def new(self, account, reader="default", limit=100, consume=False):
        if not 1 <= limit <= 1000:
            raise ValueError("limit_must_be_1_to_1000")
        with self.db:
            row = self.db.execute("SELECT seq FROM readers WHERE account=? AND reader=?", (account, reader)).fetchone()
            start = row[0] if row else 0
            # Event sequence includes captured updates and recalls, so a timestamp rollback cannot hide new data.
            rows = self.db.execute("SELECT payload,seq FROM versions WHERE account=? AND seq>? ORDER BY seq LIMIT ?", (account, start, limit+1)).fetchall()
            more = len(rows) > limit
            rows = rows[:limit]
            end = rows[-1]["seq"] if rows else start
            if consume:
                self.db.execute("INSERT INTO readers VALUES(?,?,?) ON CONFLICT(account,reader) DO UPDATE SET seq=max(seq,excluded.seq)", (account, reader, end))
            return {"messages": [dict(json.loads(r["payload"]), seq=r["seq"]) for r in rows], "has_more": more, "read_seq": end, "consumed": consume}

    def status(self):
        return {"messages": self.db.execute("SELECT count(*) FROM messages").fetchone()[0],
                "versions": self.watermark(), "contacts": self.db.execute("SELECT count(*) FROM contacts").fetchone()[0],
                "members": self.db.execute("SELECT count(*) FROM members").fetchone()[0],
                "sync": [dict(r) for r in self.db.execute("SELECT * FROM sync_state ORDER BY account,source")]}

    def doctor(self, *, max_age=None):
        check = self.db.execute("PRAGMA quick_check").fetchone()[0]
        bad = self.db.execute("SELECT count(*) FROM sync_state WHERE error IS NOT NULL").fetchone()[0]
        dangling = self.db.execute("SELECT count(*) FROM messages m LEFT JOIN versions v ON m.last_seq=v.seq WHERE v.seq IS NULL OR v.message_id!=m.id").fetchone()[0]
        latest = self.db.execute("SELECT max(succeeded) FROM sync_state WHERE source='__sync__'").fetchone()[0]
        stale = max_age is not None and (latest is None or time.time()-latest > max_age)
        return {"ok": check == "ok" and bad == 0 and dangling == 0 and not stale,
                "integrity": check, "sync_errors": bad, "dangling_versions": dangling,
                "stale": stale, "last_complete_sync": latest}
