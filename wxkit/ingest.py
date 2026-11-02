"""镜像 → 档案库。每轮只处理有变化的库。

每张消息表取两类行：
  - local_id 比上次处理到的大：新消息
  - 最近 2 小时内的消息：用来发现撤回、状态变化（微信撤回是原地把那一行改写成系统消息）
撤回时：保留原来的 kind/text，只填 recalled_at，并把撤回提示存进 extra.recall。
没见过原文就被撤回的，存成 kind=recall。
"""
import hashlib
import json
import os
import re
import sqlite3
import time
import zlib

import zstandard

from . import archive, contacts, parse

RESCAN = 7200
_Z = zstandard.ZstdDecompressor()
_HEX32 = re.compile(rb'[0-9a-f]{32}')


def _txt(b):
    if isinstance(b, bytes):
        if b[:4] == b'\x28\xb5\x2f\xfd':
            try:
                b = _Z.decompress(b, max_output_size=50 * 1024 * 1024)
            except zstandard.ZstdError:
                return ''
        return b.decode('utf-8', 'replace')
    return b or ''


def _shard(fname):
    return int(re.search(r'_(\d+)\.db$', fname).group(1))


class Ingest:
    def __init__(self, acct):
        self.acct = acct
        self.me = acct.wxid
        self.c = archive.connect(acct, write=True)
        self._names = None

    # ---------- 联系人、会话 ----------
    def refresh_contacts(self):
        names, members, labels = contacts.load(os.path.join(self.acct.mirror, 'contact.db'))
        with self.c:
            self.c.execute('delete from names'); self.c.executemany('insert or replace into names values(?,?,?,?,?,?)', names)
            self.c.execute('delete from members'); self.c.executemany('insert or replace into members values(?,?,?)', members)
            self.c.execute('delete from labels'); self.c.executemany('insert or replace into labels values(?,?)', labels)
        self._names = None

    def refresh_sessions(self):
        s = sqlite3.connect(f'file:{os.path.join(self.acct.mirror, "session.db")}?mode=ro', uri=True)
        rows = list(s.execute('select username, unread_count, summary, last_timestamp, sort_timestamp, is_hidden,'
                              ' draft, last_sender_display_name from SessionTable'))
        s.close()
        with self.c:
            self.c.execute('delete from sessions')
            self.c.executemany('insert into sessions values(?,?,?,?,?,?,?,?)',
                               [(r[0], r[1], _txt(r[2]), r[3], r[4], r[5], _txt(r[6]), r[7]) for r in rows])

    def name_of(self, u):
        if self._names is None:
            self._names = {u: (rk or nk or u) for u, rk, nk in self.c.execute('select user, remark, nick from names')}
        return self._names.get(u, u)

    # ---------- 消息 ----------
    def messages(self, fname, full=False):
        """处理一个 message_N.db。返回 (新增, 撤回, 改动) 条数。
        full=True：整理规则改了以后重新整理全部旧消息（已捕获的撤回原文不会被覆盖）。"""
        shard = _shard(fname)
        m = sqlite3.connect(f'file:{os.path.join(self.acct.mirror, fname)}?mode=ro', uri=True)
        n2i = dict(m.execute('select rowid, user_name from Name2Id'))
        by_md5 = {hashlib.md5(u.encode()).hexdigest(): u for u in n2i.values()}
        cur = dict(((t, v) for t, v in self.c.execute('select tbl, max_local from cursors where shard=?', (shard,))))
        since_seq = (int(time.time()) - RESCAN) * 1000
        stats = [0, 0, 0]
        tables = [t for (t,) in m.execute("select name from sqlite_master where type='table' and name like 'Msg\\_%' escape '\\'")]
        for tbl in tables:
            chat = by_md5.get(tbl[4:])
            if not chat:
                continue
            last = cur.get(tbl, 0)
            rows = m.execute(f'select local_id, server_id, local_type, real_sender_id, create_time, source,'
                             f' message_content, packed_info_data from {tbl} where local_id > ? or sort_seq > ?',
                             (0 if full else last, since_seq)).fetchall()
            if not rows:
                continue
            with self.c:
                self._table(shard, chat, rows, last, n2i, stats, full)
                self.c.execute('insert or replace into cursors values(?,?,?)', (shard, tbl, max(last, max(r[0] for r in rows))))
        m.close()
        return stats

    def _table(self, shard, chat, rows, last, n2i, stats, full=False):
        is_group = chat.endswith('@chatroom')
        old = {}
        recent = [r[0] for r in rows if r[0] <= last]
        for i in range(0, len(recent), 500):
            part = recent[i:i + 500]
            q = f'select local_id, chash, kind, extra, recalled_at from msgs where shard=? and chat=? and local_id in ({",".join("?" * len(part))})'
            for lid, ch, kind, ex, rec in self.c.execute(q, (shard, chat, *part)):
                old[lid] = (ch, kind, ex, rec)
        ins = []
        for lid, svrid, lt, rsid, ts, src, content, packed in rows:
            raw = _txt(content)
            chash = zlib.crc32(raw.encode('utf-8', 'replace')) ^ lt
            prev = old.get(lid)
            if prev and (prev[0] == chash and not full or prev[3] and prev[1] != 'recall'):
                continue   # 没变；或者是已捕获原文的撤回，原文不能被覆盖
            base, sub = lt & 0xffffffff, lt >> 32
            sender = n2i.get(rsid, '')
            body = raw
            if is_group:
                pre, body = parse.split_group_prefix(raw)
                sender = pre or sender
            if not is_group and not sender:
                sender = chat
            src = _txt(src)
            try:
                kind, text, extra = parse.parse(base, sub, body, src, self.name_of)
            except Exception as e:   # 单条解析失败不影响整体
                kind, text, extra = 'other', f'[解析失败 类型{base}:{sub}] {parse.short(body, 80)}', {'err': str(e)[:80]}
            if kind in ('image', 'video') and packed:   # 本地文件名藏在 packed_info_data 里
                h = _HEX32.search(packed)
                if h:
                    extra = dict(extra or {}, file=h.group(0).decode())
            at = parse.at_info(src, self.me) if is_group else None
            if kind == 'recall' and text.startswith('你撤回'):
                sender = self.me
            if prev is None:
                rec = ts if kind == 'recall' else None
                ins.append((shard, chat, lid, svrid, ts, sender, int(sender == self.me), kind, text,
                            json.dumps(extra, ensure_ascii=False) if extra else None, at, rec, chash))
                if kind == 'voice' and svrid:
                    self.c.execute('insert or ignore into voices(svrid, chat, ts) values(?,?,?)', (svrid, chat, ts))
                stats[0] += 1
            elif kind == 'recall' and prev[1] != 'recall':
                ex = json.loads(prev[2]) if prev[2] else {}
                ex['recall'] = text
                self.c.execute('update msgs set recalled_at=?, extra=?, chash=? where shard=? and chat=? and local_id=?',
                               (int(time.time()), json.dumps(ex, ensure_ascii=False), chash, shard, chat, lid))
                stats[1] += 1
            else:
                self.c.execute('update msgs set kind=?, text=?, extra=?, at=?, chash=? where shard=? and chat=? and local_id=?',
                               (kind, text, json.dumps(extra, ensure_ascii=False) if extra else None, at, chash, shard, chat, lid))
                stats[2] += 1
        if ins:
            self.c.executemany('insert or ignore into msgs(shard, chat, local_id, svrid, ts, sender, is_me, kind, text,'
                               ' extra, at, recalled_at, chash) values(?,?,?,?,?,?,?,?,?,?,?,?,?)', ins)

    def reparse(self):
        out = {}
        for f in sorted(os.listdir(self.acct.mirror)):
            if re.match(r'^message_\d+\.db$', f):
                out[f] = self.messages(f, full=True)
        from . import voice
        voice.apply_text(self.c)   # 重新整理会把语音正文冲掉，补回转写
        return out

    # ---------- 一轮 ----------
    def run(self, changed):
        """changed：本轮镜像有变化的库文件名。返回统计字典。"""
        out = {}
        if 'contact.db' in changed or not self.c.execute('select 1 from names limit 1').fetchone():
            self.refresh_contacts(); out['contacts'] = 1
        if 'session.db' in changed or not self.c.execute('select 1 from sessions limit 1').fetchone():
            self.refresh_sessions(); out['sessions'] = 1
        from . import social
        if 'sns.db' in changed or not archive.kv_get(self.c, 'sns') or social.needs_refresh(self.c):
            out['sns'] = social.refresh_sns(self.c, self.acct.mirror); archive.kv_set(self.c, 'sns', 1)
        if 'favorite.db' in changed or not archive.kv_get(self.c, 'favs'):
            out['favs'] = social.refresh_favs(self.c, self.acct.mirror); archive.kv_set(self.c, 'favs', 1)
        for f in sorted(changed):
            if f.startswith('message_'):
                out[f] = self.messages(f)
        if not archive.kv_get(self.c, 'built'):
            for f in sorted(os.listdir(self.acct.mirror)):
                if re.match(r'^message_\d+\.db$', f) and f not in changed:
                    out[f] = self.messages(f)
            with self.c:
                archive.kv_set(self.c, 'built', int(time.time()))
        with self.c:
            archive.kv_set(self.c, 'ingested_at', int(time.time()))
        return out
