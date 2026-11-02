"""查询接口：命令和脚本都用这里，不直接拼 SQL。

  db = DB()                    # 默认账号；DB('wxid_xxx') 指定
  db.resolve('AL')             # 按备注/昵称/微信号/wxid 找人或群 → [user, ...]（最像的在前）
  db.chat('AL')                # 唯一确定一个会话，不唯一就报错并列出候选
  db.msgs(chat=..., since=..., until=..., kinds=[...], q=..., limit=...)   # → [Row]
  db.display(user, room)       # 显示名：备注 > 群昵称 > 昵称 > wxid
每行消息 Row 的字段：id chat local_id svrid ts sender is_me kind text extra at recalled_at
"""
import json
import re
import sqlite3

from . import archive, config

# 微信内置账号，不算真正的会话
SYSTEM_USERS = {'weixin', 'fmessage', 'floatbottle', 'medianote', 'notifymessage', 'qmessage', 'qqmail', 'tmessage',
                'newsapp', 'brandsessionholder', 'brandservicesessionholder', '@placeholder_foldgroup', 'notification_messages',
                'opencustomerservicemsg', 'qqsafe', 'mphelper', 'exmail_tool', 'helper_folders'}


def is_noise(chat):
    return chat in SYSTEM_USERS or chat.startswith('gh_') or chat.startswith('@')


class DB:
    def __init__(self, account=None):
        self.acct = config.account(account)
        self.c = archive.connect(self.acct)
        self.c.row_factory = sqlite3.Row
        self.me = self.acct.wxid
        self._names = None
        self._gnick = {}

    # ---------- 名字 ----------
    @property
    def names(self):
        if self._names is None:
            self._names = {r['user']: r for r in self.c.execute('select * from names')}
        return self._names

    def display(self, user, room=None):
        """显示名：备注 > 群昵称 > 昵称 > wxid（没名字的群用前几个成员名代替，和微信一样）。"""
        if user == self.me:
            return '我'
        n = self.names.get(user)
        name = n['remark'] if n else ''
        if room and not name:
            if room not in self._gnick:
                self._gnick[room] = {u: g for u, g in self.c.execute('select user, gnick from members where room=?', (room,)) if g}
            name = self._gnick[room].get(user, '')
        if not name and n:
            name = n['nick']
        if not name and user.endswith('@chatroom'):
            ms = [u for (u,) in self.c.execute('select user from members where room=? and user!=? limit 3', (user, self.me))]
            name = '群:' + '、'.join(self.display(u) for u in ms) if ms else ''
        return ' '.join((name or user).split())

    def kind_of(self, user):
        n = self.names.get(user)
        return n['kind'] if n else ('group' if user.endswith('@chatroom') else 'other')

    def last_ts(self, chat):
        r = self.c.execute('select max(ts) from msgs where chat=?', (chat,)).fetchone()
        return r[0] or 0

    def resolve(self, q):
        """找人/群。完全一致优先，其次包含；同级按最近聊天时间排。"""
        q = q.strip()
        if q in self.names or self.c.execute('select 1 from msgs where chat=? limit 1', (q,)).fetchone():
            return [q]
        exact, part = [], []
        ql = q.lower()
        for u, n in self.names.items():
            fields = [n['remark'], n['nick'], n['alias'], u]
            if any(f and f.lower() == ql for f in fields):
                exact.append(u)
            elif any(f and ql in f.lower() for f in fields):
                part.append(u)
        res = exact or part
        res.sort(key=lambda u: -self.last_ts(u))
        return res

    def chat(self, q):
        cands = self.resolve(q)
        talked = [u for u in cands if self.last_ts(u)]
        if len(talked) == 1 or (talked and len(cands) == 1):
            return talked[0]
        if not cands:
            raise SystemExit(f'!! 找不到 "{q}"。试试 wx find {q[:2]}（备注、昵称、微信号都能搜）')
        if not talked:
            raise SystemExit(f'!! "{q}" 找到了联系人但没有聊天记录：' + '；'.join(self.brief(u) for u in cands[:5]))
        raise SystemExit(f'!! "{q}" 对应多个会话，请写得更具体（或直接用 wxid）：\n' +
                         '\n'.join('  ' + self.brief(u) for u in talked[:10]))

    def brief(self, u):
        n = self.names.get(u)
        bits = [self.display(u)]
        if n and n['remark'] and n['nick'] and n['nick'] != n['remark']:
            bits.append(f'昵称:{n["nick"]}')
        bits.append(u)
        return ' | '.join(bits)

    # ---------- 消息 ----------
    def msgs(self, chat=None, since=None, until=None, kinds=None, not_kinds=None, q=None, sender=None,
             limit=None, newest=False, chats=None):
        """newest=True：取最新的 limit 条（结果仍按时间正序）。q 支持多个词（空格分开，都要包含）和 re:正则。"""
        w, a = [], []
        if chat:
            w.append('chat=?'); a.append(chat)
        if chats is not None:
            w.append(f'chat in ({",".join("?" * len(chats))})'); a += list(chats)
        if since:
            w.append('ts>=?'); a.append(since)
        if until:
            w.append('ts<?'); a.append(until)
        if kinds:
            w.append(f'kind in ({",".join("?" * len(kinds))})'); a += list(kinds)
        if not_kinds:
            w.append(f'kind not in ({",".join("?" * len(not_kinds))})'); a += list(not_kinds)
        if sender:
            w.append('sender=?'); a.append(sender)
        rx = None
        if q:
            if q.startswith('re:'):
                rx = re.compile(q[3:], re.I)
            else:
                for t in q.split():
                    w.append('text like ?'); a.append(f'%{t}%')
        sql = 'select * from msgs' + (' where ' + ' and '.join(w) if w else '')
        sql += ' order by ts desc, local_id desc' if newest else ' order by ts, local_id'
        if limit and not rx:
            sql += f' limit {int(limit)}'
        rows = []
        for r in self.c.execute(sql, a):
            if rx and not rx.search(r['text'] or ''):
                continue
            rows.append(r)
            if rx and limit and len(rows) >= limit:
                break
        return rows[::-1] if newest else rows

    def get(self, mid):
        return self.c.execute('select * from msgs where id=?', (mid,)).fetchone()


def extra(r):
    return json.loads(r['extra']) if r['extra'] else {}
