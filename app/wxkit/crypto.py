"""微信 4.x 数据库是 SQLCipher 4：每页 4096 字节，末尾 80 字节 = 16 字节 IV + 64 字节 HMAC-SHA512。

  enc_key = PBKDF2-SHA512(原始密钥, 盐, 256000 次, 32 字节)     盐 = 文件前 16 字节
  mac_key = PBKDF2-SHA512(enc_key, 盐 ^ 0x3a, 2 次, 32 字节)
  第 1 页前 16 字节是盐；解密后换回 'SQLite format 3\\0'。
"""
import hashlib
import hmac
import struct

from Crypto.Cipher import AES

PAGE = 4096
RESERVE = 80
SQLITE_HEADER = b'SQLite format 3\x00'

_derived = {}   # 盐 -> (enc_key, mac_key)，PBKDF2 很慢（约 0.2 秒），每个库只算一次


def derive(raw_key, salt):
    k = (raw_key, salt)
    if k not in _derived:
        enc = hashlib.pbkdf2_hmac('sha512', raw_key, salt, 256000, 32)
        mac = hashlib.pbkdf2_hmac('sha512', enc, bytes(b ^ 0x3a for b in salt), 2, 32)
        _derived[k] = (enc, mac)
    return _derived[k]


def page_ok(page, pgno, mac_key):
    off = 16 if pgno == 1 else 0
    want = page[PAGE - RESERVE + 16:PAGE]
    got = hmac.new(mac_key, page[off:PAGE - RESERVE + 16] + struct.pack('<I', pgno), hashlib.sha512).digest()
    return hmac.compare_digest(want, got)


def decrypt_page(page, pgno, keys):
    """返回明文页；HMAC 不对（读到写了一半的页，或密钥不对）返回 None。"""
    enc, mac = keys
    if len(page) != PAGE or not page_ok(page, pgno, mac):
        return None
    off = 16 if pgno == 1 else 0
    iv = page[PAGE - RESERVE:PAGE - RESERVE + 16]
    body = AES.new(enc, AES.MODE_CBC, iv).decrypt(page[off:PAGE - RESERVE])
    return (SQLITE_HEADER if pgno == 1 else b'') + body + page[PAGE - RESERVE:]


def check_key(raw_key, db_path):
    """用某个库的第 1 页验证密钥对不对。"""
    with open(db_path, 'rb') as f:
        p1 = f.read(PAGE)
    if len(p1) < PAGE:
        return False
    return page_ok(p1, 1, derive(raw_key, p1[:16])[1])


def tag(page):
    """页的“指纹”：HMAC 的前 8 字节。页内容变了，HMAC 一定变（IV 也会换）。"""
    return page[PAGE - 64:PAGE - 56]


def encrypt_page(plain, pgno, raw_key, salt, iv):
    """仅供测试：造一页加密数据，验证解密和增量逻辑。"""
    enc, mac = derive(raw_key, salt)
    off = 16 if pgno == 1 else 0
    body = AES.new(enc, AES.MODE_CBC, iv).encrypt(plain[off:PAGE - RESERVE])
    head = salt if pgno == 1 else b''
    pre = head + body + iv
    m = hmac.new(mac, pre[off:] + struct.pack('<I', pgno), hashlib.sha512).digest()
    return pre + m + b'\x00' * (RESERVE - 16 - 64)
