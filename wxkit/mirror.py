"""把微信的加密库增量解成明文镜像。只读原库，不加锁、不写原库。

增量原理：
  - 主库文件：记下每页的“指纹”（HMAC 前 8 字节）。文件变了就逐页比指纹，只解变化的页。
  - WAL 文件（新消息先写这里）：记下处理到哪一帧，只解新增的帧，按提交点应用。
  - 主库被合并（checkpoint）后，重放一遍当前 WAL，保证镜像 = 主库 + WAL 的最新状态。
读到正在写一半的页（HMAC 不对）就跳过，下一轮再补。
"""
import json
import os
import re
import struct

from . import crypto

PAGE = crypto.PAGE
CHUNK = 256  # 每次读 256 页（1MB），内存占用小
WANT = [('message', re.compile(r'^(message|media)_\d+\.db$')),
        ('contact', re.compile(r'^contact\.db$')),
        ('session', re.compile(r'^session\.db$')),
        ('sns', re.compile(r'^sns\.db$')),
        ('favorite', re.compile(r'^favorite\.db$'))]


def targets(acct):
    out = []
    for sub, pat in WANT:
        d = os.path.join(acct.db, sub)
        if not os.path.isdir(d):
            continue
        for n in sorted(os.listdir(d)):
            p = os.path.join(d, n)
            if pat.match(n) and os.path.getsize(p) >= PAGE:
                out.append(p)
    return out


def _write(path, data):
    with open(path, 'wb') as f:
        f.write(data)


class Mirror:
    def __init__(self, acct, raw_key):
        self.acct = acct
        self.raw = raw_key
        os.makedirs(acct.mirror, exist_ok=True)

    def path(self, src):
        return os.path.join(self.acct.mirror, os.path.basename(src))

    # ---------- 状态 ----------
    def _state_file(self, src):
        return self.path(src) + '.state'

    def _load_state(self, src):
        try:
            with open(self._state_file(src)) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_state(self, src, st):
        tmp = self._state_file(src) + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(st, f)
        os.replace(tmp, self._state_file(src))

    # ---------- 主流程 ----------
    def update_all(self):
        """返回本轮有变化的库的文件名集合。"""
        changed = set()
        for src in targets(self.acct):
            try:
                if self.update(src):
                    changed.add(os.path.basename(src))
            except OSError:
                continue   # 文件被占用等，下一轮再来
        return changed

    def update(self, src):
        st = self._load_state(src)
        dst = self.path(src)
        s = os.stat(src)
        sig = [s.st_mtime_ns, s.st_size]
        with open(src, 'rb') as f:
            salt = f.read(16)
        keys = crypto.derive(self.raw, salt)
        wrote = False
        if not os.path.exists(dst) or not os.path.exists(dst + '.tags') or st.get('salt') != salt.hex():
            self._full(src, dst, keys); st = {'salt': salt.hex(), 'main': sig, 'wal_salt': None, 'wal_off': 0}
            wrote = True
        elif st.get('main') != sig:
            wrote |= self._main_diff(src, dst, keys) > 0
            st['main'] = sig if not getattr(self, '_torn', False) else [0, 0]
            st['wal_off'] = 0   # 主库合并过，WAL 要从头重放
        n, st = self._wal(src, dst, keys, st)
        wrote |= n > 0
        self._save_state(src, st)
        return wrote

    def _full(self, src, dst, keys):
        tmp = dst + '.building'
        tags = bytearray()
        with open(src, 'rb') as fi, open(tmp, 'wb') as fo:
            pgno = 0
            while True:
                buf = fi.read(PAGE * CHUNK)
                if not buf:
                    break
                for i in range(0, len(buf) - PAGE + 1, PAGE):
                    pgno += 1
                    pg = buf[i:i + PAGE]
                    plain = crypto.decrypt_page(pg, pgno, keys)
                    fo.write(plain if plain else b'\x00' * PAGE)
                    tags += crypto.tag(pg) if plain else b'\x00' * 8
        os.replace(tmp, dst)
        _write(dst + '.tags', bytes(tags))

    def _main_diff(self, src, dst, keys):
        self._torn = False
        with open(dst + '.tags', 'rb') as f:
            tags = bytearray(f.read())
        n = 0
        with open(src, 'rb') as fi, open(dst, 'r+b') as fo:
            pgno = 0
            while True:
                buf = fi.read(PAGE * CHUNK)
                if not buf:
                    break
                for i in range(0, len(buf) - PAGE + 1, PAGE):
                    pgno += 1
                    pg = buf[i:i + PAGE]
                    t = crypto.tag(pg); o = (pgno - 1) * 8
                    if tags[o:o + 8] == t:
                        continue
                    plain = crypto.decrypt_page(pg, pgno, keys)
                    if plain is None:
                        self._torn = True; continue
                    fo.seek((pgno - 1) * PAGE); fo.write(plain)
                    if len(tags) < o + 8:
                        tags += b'\x00' * (o + 8 - len(tags))
                    tags[o:o + 8] = t; n += 1
            if pgno * PAGE < os.path.getsize(dst):
                fo.truncate(pgno * PAGE); del tags[pgno * 8:]
        _write(dst + '.tags', bytes(tags))
        return n

    def _wal(self, src, dst, keys, st):
        wal = src + '-wal'
        if not os.path.exists(wal) or os.path.getsize(wal) <= 32:
            return 0, st
        with open(wal, 'rb') as f:
            head = f.read(32)
            wsalt = head[16:24].hex()
            if st.get('wal_salt') != wsalt or st.get('wal_off', 0) < 32 or st['wal_off'] > os.path.getsize(wal):
                st['wal_salt'] = wsalt; st['wal_off'] = 32
            f.seek(st['wal_off'])
            data = f.read()
        frames, o, last_commit = [], 0, -1
        while o + 24 + PAGE <= len(data):
            h = data[o:o + 24]
            if h[8:16].hex() != wsalt:
                break
            pgno, commit = struct.unpack('>II', h[:8])
            frames.append((pgno, commit, data[o + 24:o + 24 + PAGE], o + 24 + PAGE))
            if commit:
                last_commit = len(frames) - 1
            o += 24 + PAGE
        if last_commit < 0:
            return 0, st
        n = 0
        with open(dst, 'r+b') as fo:
            size_pages = None
            for pgno, commit, pg, _ in frames[:last_commit + 1]:
                plain = crypto.decrypt_page(pg, pgno, keys)
                if plain is None:
                    continue
                fo.seek((pgno - 1) * PAGE); fo.write(plain); n += 1
                if commit:
                    size_pages = commit
            if size_pages and size_pages * PAGE < os.path.getsize(dst):
                fo.truncate(size_pages * PAGE)
        st['wal_off'] += frames[last_commit][3]
        return n, st
