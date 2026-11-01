"""SQLCipher 4 page authentication and committed WAL snapshots.

No SQLite connection is ever opened on a source path. Decryption consumes bytes;
the caller owns its private snapshot. The supported profile is fixed, fail closed.
"""
from __future__ import annotations

import hashlib
import hmac
import struct


PAGE = 4096
RESERVE = 80
MAGIC = b"SQLite format 3\0"


class SourceIntegrityError(ValueError):
    pass


def _wal_checksum(data: bytes, initial=(0, 0), endian="<"):
    if len(data) % 8:
        raise SourceIntegrityError("WAL checksum block is misaligned")
    first, second = initial
    for left, right in struct.iter_unpack(endian + "II", data):
        first = (first + left + second) & 0xffffffff
        second = (second + right + first) & 0xffffffff
    return first, second


def committed_wal(wal: bytes) -> tuple[list[tuple[int, bytes]], int | None]:
    """Verify the WAL chain and return pages only through its last commit.

    Partial final frames and old-salt recycled tails are not committed. Complete
    same-salt frames with an invalid checksum are corruption and reject the scan.
    """
    if not wal:
        return [], None
    if len(wal) < 32:
        raise SourceIntegrityError("WAL header is incomplete")
    magic, version, page_size = struct.unpack_from(">III", wal)
    if magic not in (0x377f0682, 0x377f0683) or version != 3007000 or page_size != PAGE:
        raise SourceIntegrityError("Unsupported WAL header or page size")
    endian = "<" if magic == 0x377f0682 else ">"
    rolling = _wal_checksum(wal[:24], endian=endian)
    if rolling != struct.unpack_from(">II", wal, 24):
        raise SourceIntegrityError("WAL header checksum mismatch")
    salt = wal[16:24]
    frames = []
    committed = 0
    database_pages = None
    for offset in range(32, len(wal) - (24 + PAGE) + 1, 24 + PAGE):
        header = wal[offset:offset + 24]
        if header[8:16] != salt:
            break
        page_no, size = struct.unpack_from(">II", header)
        if page_no < 1:
            raise SourceIntegrityError("WAL page number is invalid")
        page = wal[offset + 24:offset + 24 + PAGE]
        rolling = _wal_checksum(header[:8] + page, rolling, endian)
        if rolling != struct.unpack_from(">II", header, 16):
            raise SourceIntegrityError("WAL frame checksum mismatch")
        frames.append((page_no, page))
        if size:
            committed = len(frames)
            database_pages = size
    return frames[:committed], database_pages


class Cipher:
    def __init__(self, key_material: bytes, salt: bytes, *, raw_key=False):
        if len(key_material) != 32 or len(salt) != 16:
            raise SourceIntegrityError("Invalid key or database salt length")
        self.key = key_material if raw_key else hashlib.pbkdf2_hmac(
            "sha512", key_material, salt, 256000, 32)
        mac_salt = bytes(b ^ 0x3a for b in salt)
        self.mac_key = hashlib.pbkdf2_hmac("sha512", self.key, mac_salt, 2, 32)

    def decrypt_page(self, page: bytes, number: int) -> bytes:
        if len(page) != PAGE:
            raise SourceIntegrityError("Database page is incomplete")
        start = 16 if number == 1 else 0
        expected = hmac.new(self.mac_key,
            page[start:PAGE - 64] + struct.pack("<I", number), hashlib.sha512).digest()
        if not hmac.compare_digest(expected, page[-64:]):
            raise SourceIntegrityError("Page authentication failed: key, format, or source corruption")
        from cryptography.hazmat.primitives.ciphers import Cipher as AESCipher, algorithms, modes
        decoder = AESCipher(algorithms.AES(self.key), modes.CBC(page[-80:-64])).decryptor()
        body = decoder.update(page[start:-80]) + decoder.finalize()
        return (MAGIC if number == 1 else b"") + body + b"\0" * RESERVE


def decrypt_database(database: bytes, key: bytes | None, wal: bytes = b"") -> bytes:
    if len(database) < PAGE or len(database) % PAGE:
        raise SourceIntegrityError("Database length is not aligned to 4096-byte pages")
    plaintext = database.startswith(MAGIC)
    if plaintext:
        if struct.unpack_from(">H", database, 16)[0] != PAGE:
            raise SourceIntegrityError("Only 4096-byte source pages are supported")
        transform = lambda page, number: page
    else:
        if key is None:
            raise SourceIntegrityError("Encrypted source requires a locally supplied key")
        cipher = Cipher(key, database[:16])
        # WeChat 4.x uses a passphrase-derived key; authenticate before doing more work.
        cipher.decrypt_page(database[:PAGE], 1)
        transform = cipher.decrypt_page
    frames, committed_size = committed_wal(wal)
    latest = dict(frames)
    page_count = committed_size or len(database) // PAGE
    output = bytearray()
    for number in range(1, page_count + 1):
        page = latest.get(number)
        if page is None:
            page = database[(number - 1) * PAGE:number * PAGE]
        output.extend(transform(page, number))
    # A private rollback-mode copy avoids creating source-side WAL or SHM files.
    output[18:20] = b"\x01\x01"
    return bytes(output)
