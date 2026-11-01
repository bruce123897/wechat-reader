"""Independent, read-only adapter for the observed Windows WeChat 4.x schema."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import sqlite3
import time

from .crypto import MAGIC, PAGE, Cipher, SourceIntegrityError, decrypt_database
from .keys import database_key


class SourceFormatError(ValueError):
    pass


def discover(roots: list[Path] | None = None) -> list[Path]:
    if roots is None:
        roots = [Path.home() / "Documents/xwechat_files"]
        roots.extend(Path(f"{letter}:/xwechat_files") for letter in "CDEFG")
        settings = Path(os.environ.get("APPDATA", "")) / "Tencent/xwechat/config"
        for ini in sorted(settings.glob("*.ini")):
            try:
                value = ini.read_text(encoding="utf-8", errors="strict").strip().strip("\0")
                if value and "\n" not in value:
                    roots.extend((Path(value), Path(value) / "xwechat_files"))
            except (OSError, UnicodeError):
                continue
    result = {}
    for root in roots:
        if root.is_dir():
            for database_dir in root.glob("*/db_storage"):
                if database_dir.is_dir():
                    account = database_dir.parent.resolve()
                    result[os.path.normcase(str(account))] = account
    return list(result.values())


def _signature(path: Path):
    try:
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns, stat.st_ino
    except FileNotFoundError:
        return None


def capture(path: Path, attempts=3) -> tuple[bytes, bytes, str]:
    """Optimistic consistent capture; reject a source that changes while copied."""
    wal = path.with_name(path.name + "-wal")
    for attempt in range(attempts):
        before = _signature(path), _signature(wal)
        if before[0] is None:
            raise SourceFormatError("Required source database is missing")
        try:
            database = path.read_bytes()
            journal = wal.read_bytes() if before[1] is not None else b""
        except FileNotFoundError:
            continue
        if before == (_signature(path), _signature(wal)):
            digest = hashlib.sha256(database)
            digest.update(journal)
            # Windows can defer a last-write timestamp while a writer keeps its
            # handle open. A second streaming content pass also detects same-size
            # in-place checkpoint writes that metadata alone could miss.
            second = hashlib.sha256()
            try:
                for source in (path, wal) if before[1] is not None else (path,):
                    with source.open("rb") as handle:
                        while block := handle.read(1024 * 1024):
                            second.update(block)
            except FileNotFoundError:
                continue
            if digest.digest() == second.digest() and before == (_signature(path), _signature(wal)):
                return database, journal, digest.hexdigest()
        if attempt + 1 < attempts:
            time.sleep(0.05)
    raise SourceIntegrityError("Source changed during all snapshot attempts; retry next sync")


def _fields(blob: bytes):
    """Minimal protobuf wire reader for the contact room-members record."""
    offset = 0

    def integer():
        nonlocal offset
        value = 0
        for shift in range(0, 70, 7):
            if offset >= len(blob):
                raise SourceFormatError("Truncated room-members record")
            byte = blob[offset]
            offset += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise SourceFormatError("Invalid room-members integer")

    while offset < len(blob):
        tag = integer()
        number, wire = tag >> 3, tag & 7
        if number == 0:
            raise SourceFormatError("Invalid room-members field")
        if wire == 0:
            value = integer()
        elif wire in (1, 5):
            length = 8 if wire == 1 else 4
            value = blob[offset:offset + length]
            offset += length
        elif wire == 2:
            length = integer()
            value = blob[offset:offset + length]
            offset += length
            if len(value) != length:
                raise SourceFormatError("Truncated room-members field")
        else:
            raise SourceFormatError("Unsupported room-members wire type")
        if offset > len(blob):
            raise SourceFormatError("Truncated room-members field")
        yield number, wire, value


class Source:
    def __init__(self, root: Path, key_file: Path | None = None):
        self.root = Path(root).resolve()
        self.storage = self.root / "db_storage"
        if not self.storage.is_dir():
            raise SourceFormatError("Selected account has no db_storage directory")
        self.account = self.root.name
        self.user = re.sub(r"_[0-9a-f]{4}$", "", self.account)
        self.key_file = Path(key_file) if key_file is not None else Path.home() / ".wxdb" / self.account / "keys.bin"
        self._key = None

    def _paths(self):
        paths = [self.storage / "contact/contact.db", self.storage / "session/session.db"]
        paths.extend(sorted((self.storage / "message").glob("message_*.db")))
        paths = [p for p in paths if not p.name.startswith("message_") or re.fullmatch(r"message_\d+\.db", p.name)]
        if len(paths) < 3:
            raise SourceFormatError("No supported message shards were found")
        return paths

    def validate(self) -> dict:
        """Check current source availability and key even when every cursor matches.

        Authentication reads one contact page, avoiding a redundant full decrypt.
        Cached key material is discarded first so a removed or changed key file
        cannot be hidden by a long-running daemon's previous successful scan.
        """
        self._key = None
        paths = self._paths()
        for path in paths:
            if not path.is_file():
                raise SourceFormatError("Required source database is missing")
            size = path.stat().st_size
            if size < PAGE or size % PAGE:
                raise SourceIntegrityError("Source database length is not aligned to 4096-byte pages")
        with paths[0].open("rb") as handle:
            page = handle.read(PAGE)
        if len(page) != PAGE:
            raise SourceIntegrityError("Contact source page is incomplete")
        encrypted = not page.startswith(MAGIC)
        if encrypted:
            current = database_key(self.key_file)
            Cipher(current, page[:16]).decrypt_page(page, 1)
            self._key = current
        elif int.from_bytes(page[16:18], "big") != PAGE:
            raise SourceIntegrityError("Only 4096-byte source pages are supported")
        return {"ok": True, "databases": len(paths), "encrypted": encrypted, "page_size": PAGE}

    def _open(self, database: bytes, wal: bytes):
        if not database.startswith(MAGIC) and self._key is None:
            self._key = database_key(self.key_file)
        plain = decrypt_database(database, self._key, wal)
        connection = sqlite3.connect(":memory:")
        try:
            connection.deserialize(plain)
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA temp_store=MEMORY")
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise SourceIntegrityError("Private source snapshot failed SQLite integrity check")
            connection.row_factory = sqlite3.Row
            return connection
        except BaseException:
            connection.close()
            raise

    def _contacts(self, connection):
        contacts = []
        for row in connection.execute("SELECT username, remark, nick_name, alias FROM contact"):
            user = row["username"]
            contacts.append(dict(account=self.account, user=user, remark=row["remark"] or "",
                nick=row["nick_name"] or "", alias=row["alias"] or "",
                kind="group" if user.endswith("@chatroom") else "contact"))
        room_nicks = {}
        for row in connection.execute("SELECT username, ext_buffer FROM chat_room"):
            for number, wire, value in _fields(row["ext_buffer"] or b""):
                if number == 1 and wire == 2:
                    entry = {num: item for num, typ, item in _fields(value) if typ == 2}
                    user, nick = entry.get(1, b""), entry.get(2, b"")
                    if user:
                        room_nicks[row["username"], user.decode("utf-8")] = nick.decode("utf-8")
        members = []
        for row in connection.execute("""SELECT r.username AS room, u.username AS user
            FROM chatroom_member m JOIN name2id r ON r.rowid=m.room_id
            JOIN name2id u ON u.rowid=m.member_id"""):
            members.append(dict(account=self.account, room=row["room"], user=row["user"],
                nick=room_nicks.get((row["room"], row["user"]), "")))
        # Ext-buffer may include members no longer present in the relational list.
        # The relational membership is authoritative; nicknames enrich it only.
        return contacts, members

    def _sessions(self, connection):
        return [dict(account=self.account, chat=row["username"], unread=row["unread_count"] or 0,
            last_ts=row["last_timestamp"] or 0, summary=row["summary"] or "")
            for row in connection.execute("SELECT username,unread_count,last_timestamp,summary FROM SessionTable")]

    def _messages(self, connection, shard):
        from .parsing import parse_message
        names = dict(connection.execute("SELECT rowid,user_name FROM Name2Id"))
        chats = {"Msg_" + hashlib.md5(name.encode("utf-8")).hexdigest(): name for name in names.values()}
        messages = []
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if re.fullmatch(r"Msg_[0-9a-f]{32}", row[0])]
        for table in tables:
            if table not in chats:
                raise SourceFormatError("Message conversation cannot be resolved from Name2Id")
            chat = chats[table]
            for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY local_id'):
                sender = names.get(row["real_sender_id"], "")
                content = row["message_content"]
                if content in (None, "", b"") and row["compress_content"]:
                    content = row["compress_content"]
                parsed = parse_message(content or "", row["local_type"], chat=chat,
                    sender=sender, is_me=sender == self.user, packed=row["packed_info_data"])
                sender = parsed.get("extra", {}).get("sender", sender)
                if not sender:
                    parsed.setdefault("extra", {})["sender_unresolved"] = True
                messages.append(dict(account=self.account, shard=shard, chat=chat,
                    local_id=str(row["local_id"]), server_id=str(row["server_id"] or ""),
                    ts=int(row["create_time"]), sender=sender, is_me=sender == self.user, **parsed))
        return messages

    def scan(self, cursors: dict[str, str] | None = None):
        """Full changed-shard reconciliation discovers even old in-place edits."""
        self.validate()
        for path in self._paths():
            source = path.relative_to(self.storage).as_posix()
            database, wal, fingerprint = capture(path)
            if cursors is not None and cursors.get(source) == fingerprint:
                continue
            connection = self._open(database, wal)
            try:
                batch = dict(account=self.account, source=source, cursor=fingerprint,
                    messages=[], contacts=[], members=[], sessions=[])
                if path.name == "contact.db":
                    batch["contacts"], batch["members"] = self._contacts(connection)
                    batch["members_complete"] = True
                    batch["member_rooms"] = [row[0] for row in connection.execute("SELECT username FROM chat_room")]
                elif path.name == "session.db":
                    batch["sessions"] = self._sessions(connection)
                    batch["sessions_complete"] = True
                else:
                    batch["messages"] = self._messages(connection, source)
                yield batch
            except sqlite3.DatabaseError:
                raise SourceFormatError("Source schema differs from the supported WeChat 4.x profile") from None
            finally:
                connection.close()

    def probe(self):
        counts = dict(messages=0, contacts=0, members=0, sessions=0, shards=0)
        for batch in self.scan():
            counts["shards"] += 1
            for key in ("messages", "contacts", "members", "sessions"):
                counts[key] += len(batch[key])
        return dict(account_hash=hashlib.sha256(self.account.encode()).hexdigest()[:12], **counts)

    def read_voice(self, server_id: str, chat: str | None = None) -> bytes | None:
        """Return the locally downloaded voice payload; no network or source writes."""
        try:
            value = int(server_id)
        except (ValueError, TypeError):
            raise SourceFormatError("Voice lookup requires a numeric server id") from None
        for path in sorted((self.storage / "message").glob("media_*.db")):
            if not re.fullmatch(r"media_\d+\.db", path.name):
                continue
            database, wal, _ = capture(path)
            connection = self._open(database, wal)
            try:
                sql = "SELECT v.voice_data FROM VoiceInfo v JOIN Name2Id n ON n.rowid=v.chat_name_id WHERE v.svr_id=?"
                parameters = [value]
                if chat is not None:
                    sql += " AND n.user_name=?"
                    parameters.append(chat)
                row = connection.execute(sql, parameters).fetchone()
                if row is not None and row[0]:
                    return bytes(row[0])
            except sqlite3.DatabaseError:
                raise SourceFormatError("Voice source schema is unsupported") from None
            finally:
                connection.close()
        return None
