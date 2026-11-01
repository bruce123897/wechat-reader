"""Source adapter tests use synthetic SQLite databases, never a user's chats."""
from __future__ import annotations

import hashlib
import hmac
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest

from wechat_reader.crypto import Cipher, MAGIC, PAGE, SourceIntegrityError, committed_wal, decrypt_database
from wechat_reader.source import Source, SourceFormatError, capture, discover


DATA = Path(__file__).resolve().parents[1] / "data" / "test-source"


@contextmanager
def opened(path):
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def encode_page(plain: bytes, number: int, cipher: Cipher, salt: bytes):
    from cryptography.hazmat.primitives.ciphers import Cipher as AES, algorithms, modes
    start = 16 if number == 1 else 0
    iv = bytes(range(16))
    encoder = AES(algorithms.AES(cipher.key), modes.CBC(iv)).encryptor()
    content = encoder.update(plain[start:-80]) + encoder.finalize()
    digest = hmac.new(cipher.mac_key, content + iv + struct.pack("<I", number), hashlib.sha512).digest()
    return (salt if number == 1 else b"") + content + iv + digest


class CipherTests(unittest.TestCase):
    def test_page_authentication_and_wrong_key(self):
        salt = bytes(range(16))
        cipher = Cipher(b"k" * 32, salt)
        plain = MAGIC + b"\0" * (PAGE - len(MAGIC))
        encrypted = encode_page(plain, 1, cipher, salt)
        self.assertEqual(cipher.decrypt_page(encrypted, 1), plain)
        with self.assertRaises(SourceIntegrityError):
            Cipher(b"z" * 32, salt).decrypt_page(encrypted, 1)
        damaged = bytearray(encrypted)
        damaged[200] ^= 1
        with self.assertRaises(SourceIntegrityError):
            cipher.decrypt_page(damaged, 1)

    def test_incomplete_database_and_wal_header(self):
        with self.assertRaises(SourceIntegrityError):
            decrypt_database(b"incomplete", None)
        with self.assertRaises(SourceIntegrityError):
            committed_wal(b"short")
        with self.assertRaises(SourceIntegrityError):
            committed_wal(b"x" * 32)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        DATA.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=DATA)
        self.base = Path(self.temporary.name)
        self.root = self.base / "xwechat_files" / "synthetic_1234"
        self.storage = self.root / "db_storage"
        for name in ("contact", "session", "message"):
            (self.storage / name).mkdir(parents=True)
        with opened(self.storage / "contact/contact.db") as connection:
            connection.executescript("""
                CREATE TABLE contact(username,remark,nick_name,alias);
                INSERT INTO contact VALUES('peer','remark','nick','alias');
                CREATE TABLE name2id(username);
                INSERT INTO name2id VALUES('room@chatroom');
                INSERT INTO name2id VALUES('peer');
                CREATE TABLE chat_room(username,ext_buffer);
                CREATE TABLE chatroom_member(room_id,member_id);
                INSERT INTO chatroom_member VALUES(1,2);
            """)
            member = b"\x0a\x04peer\x12\x04room"
            connection.execute("INSERT INTO chat_room VALUES(?,?)", ("room@chatroom", b"\x0a" + bytes([len(member)]) + member))
        with opened(self.storage / "session/session.db") as connection:
            connection.executescript("""CREATE TABLE SessionTable(username,unread_count,last_timestamp,summary);
                INSERT INTO SessionTable VALUES('peer',2,100,'summary');""")
        self.message_path = self.storage / "message/message_0.db"
        self.table = "Msg_" + hashlib.md5(b"peer").hexdigest()
        with opened(self.message_path) as connection:
            connection.executescript(f"""CREATE TABLE Name2Id(user_name);
                INSERT INTO Name2Id VALUES('peer');
                INSERT INTO Name2Id VALUES('synthetic');
                CREATE TABLE {self.table}(local_id,server_id,local_type,real_sender_id,create_time,
                    message_content,compress_content,packed_info_data);
                INSERT INTO {self.table} VALUES(1,99,1,1,100,'synthetic text',NULL,NULL);
            """)

    def tearDown(self):
        self.temporary.cleanup()

    def test_discover_scan_cursor_update_and_readonly(self):
        self.assertEqual(discover([self.root.parent]), [self.root.resolve()])
        paths = list(self.storage.rglob("*"))
        before = {str(p): p.read_bytes() for p in paths if p.is_file()}
        source = Source(self.root)
        batches = list(source.scan())
        self.assertEqual(sum(len(b["messages"]) for b in batches), 1)
        self.assertEqual(batches[0]["members"][0]["nick"], "room")
        self.assertEqual(batches[1]["sessions"][0]["unread"], 2)
        self.assertFalse(batches[2]["messages"][0]["is_me"])
        cursors = {batch["source"]: batch["cursor"] for batch in batches}
        self.assertEqual(list(source.scan(cursors)), [])
        after = {str(p): p.read_bytes() for p in self.storage.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        with opened(self.message_path) as connection:
            connection.execute(f"UPDATE {self.table} SET message_content='old message edited' WHERE local_id=1")
        changed = list(source.scan(cursors))
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["messages"][0]["text"], "old message edited")

    def test_live_wal_commits_only_and_corruption(self):
        connection = sqlite3.connect(self.message_path)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(f"INSERT INTO {self.table} VALUES(2,100,1,2,100,'sent',NULL,NULL)")
            connection.commit()
            raw, wal, _ = capture(self.message_path)
            frames, size = committed_wal(wal)
            self.assertTrue(frames)
            self.assertGreater(size, 0)
            private = sqlite3.connect(":memory:")
            try:
                private.deserialize(decrypt_database(raw, None, wal))
                self.assertEqual(private.execute(f"SELECT count(*) FROM {self.table}").fetchone()[0], 2)
            finally:
                private.close()
            # A complete authentic frame with its commit marker cleared is not
            # committed: SQLite's checksum must also be updated for this fixture.
            damaged = bytearray(wal)
            damaged[56 + 100] ^= 1
            with self.assertRaises(SourceIntegrityError):
                committed_wal(damaged)
            frames, size = committed_wal(wal[:32])
            self.assertEqual((frames, size), ([], None))
            # Rebuild an authentic uncommitted final frame. Its pages must not
            # become visible merely because they are physically in the WAL.
            uncommitted = bytearray(wal[:32])
            rolling = struct.unpack_from(">II", wal, 24)
            endian = "<" if struct.unpack_from(">I", wal)[0] == 0x377f0682 else ">"
            for offset in range(32, len(wal) - (24 + PAGE) + 1, 24 + PAGE):
                frame = bytearray(wal[offset:offset + 24 + PAGE])
                frame[4:8] = b"\0" * 4
                first, second = rolling
                for left, right in struct.iter_unpack(endian + "II", frame[:8] + frame[24:]):
                    first = (first + left + second) & 0xffffffff
                    second = (second + right + first) & 0xffffffff
                rolling = first, second
                struct.pack_into(">II", frame, 16, *rolling)
                uncommitted.extend(frame)
            self.assertEqual(committed_wal(uncommitted), ([], None))
            before = self.message_path.read_bytes(), self.message_path.with_name(self.message_path.name + "-wal").read_bytes()
            list(Source(self.root).scan())
            after = self.message_path.read_bytes(), self.message_path.with_name(self.message_path.name + "-wal").read_bytes()
            self.assertEqual(before, after)
        finally:
            connection.close()

    def test_source_schema_failure_is_explicit(self):
        with opened(self.message_path) as connection:
            connection.execute("DROP TABLE Name2Id")
        with self.assertRaises(SourceFormatError):
            list(Source(self.root).scan())

    def test_empty_new_shard_is_valid(self):
        with opened(self.message_path) as connection:
            connection.execute(f"DROP TABLE {self.table}")
        self.assertEqual(list(Source(self.root).scan())[-1]["messages"], [])

    def test_voice_missing_and_invalid_id(self):
        source = Source(self.root)
        self.assertIsNone(source.read_voice("99", "peer"))
        with self.assertRaises(SourceFormatError):
            source.read_voice("not-a-server-id")


if __name__ == "__main__":
    unittest.main()
