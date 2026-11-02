"""紧凑输出（省 token）+ 输出上限。

格式：
  ■ 会话名（N条）                     多个会话时的分组标题
  10-17 14:05 张三：第一句              日期变了才写日期
     第二句                           同一人连续发言，后面每条只缩进
     +3m 第三句                        隔了几分钟才标 +Nm
  [已撤回] 原文                        撤回的消息原文照样显示
  回复内容 ↪张三:「被引用的话」          引用回复
  (@我) …                             群里 @ 了我
  …(+1234字 #56789)                   太长被截断；wx msg 56789 看全文
总输出超过上限（默认 6000 字，--max 改）时截断并提示怎么缩小范围。
"""
import json
import sys

from . import timeutil
from .query import extra


SYSTEMISH = {'system', 'friend_added', 'recall', 'redpacket_open', 'payment', 'group_join', 'group_leave',
             'group_rename', 'group_admin', 'group_disband'}


class Out:
    """攒输出，超上限就停。"""

    def __init__(self, max_chars=6000):
        self.max = max_chars or 10 ** 9
        self.n = 0
        self.cut = False

    def __call__(self, s=''):
        if self.cut:
            return False
        if self.n + len(s) > self.max:
            self.cut = True
            return False
        sys.stdout.write(s + '\n')
        self.n += len(s) + 1
        return True

    def done(self, hint):
        if self.cut:
            sys.stdout.write(f'…（输出到上限 {self.max} 字截断。{hint}）\n')


def body(db, r, width=300, room=None):
    """一条消息的正文（含撤回、引用、@ 标记）。"""
    t = (r['text'] or '').replace('\r', '').replace('\n', '⏎')
    if width and len(t) > width:
        t = t[:width] + f'…(+{len(t) - width}字 #{r["id"]})'
    ex = extra(r) if r['kind'] == 'quote' and r['extra'] else None
    if ex and ex.get('ref'):
        ref = ex['ref'].replace('\n', '⏎')
        rw = ex.get('ref_who') or ''
        t += f' ↪{rw + ":" if rw else ""}「{ref[:60] + "…" if len(ref) > 60 else ref}」'
    if r['recalled_at'] and r['kind'] != 'recall':
        t = '[已撤回] ' + t
    if r['at'] == 'me':
        t = '(@我) ' + t
    elif r['at'] == 'all':
        t = '(@所有人) ' + t
    return t


def who(db, r, private_name='对方'):
    if r['kind'] in SYSTEMISH and not (r['kind'] == 'recall' and r['is_me']):
        return '系统'
    if r['is_me']:
        return '我'
    if r['chat'].endswith('@chatroom'):
        return db.display(r['sender'], r['chat']) if r['sender'] and r['sender'] != r['chat'] else '系统'
    return private_name


def lines(db, rows, out, width=300, private_name='对方', mark=None):
    """一个会话内的消息，按发言人合并。mark 是一组消息 id，这些消息正文前加 » 标出。"""
    last = d0 = None; t0 = 0
    for r in rows:
        d = timeutil.fmt(r['ts'], '%m-%d'); hm = timeutil.fmt(r['ts'], '%H:%M')
        sd = who(db, r, private_name)
        b = body(db, r, width)
        if mark and r['id'] in mark:
            b = '» ' + b
        if sd != last or d != d0:
            ok = out(f'{d + " " if d != d0 else ""}{hm} {sd}：{b}')
            last, d0, t0 = sd, d, r['ts']
        else:
            m = (r['ts'] - t0) // 60
            ok = out(f'   {f"+{m}m " if m else ""}{b}')
        if not ok:
            return False
    return True


def by_chat(db, rows, out, width=300, order='first'):
    """多个会话：按会话分组，组按第一条（或最后一条）消息时间排。"""
    groups = {}
    for r in rows:
        groups.setdefault(r['chat'], []).append(r)
    key = (lambda kv: kv[1][0]['ts']) if order == 'first' else (lambda kv: -kv[1][-1]['ts'])
    for chat, rs in sorted(groups.items(), key=key):
        if not out(f'■ {db.display(chat)}（{len(rs)}条）'):
            return False
        if not lines(db, rs, out, width):
            return False
    return True


def flat(db, rows, out, width=200, show_chat=True):
    """一行一条，带会话名（搜索结果用）。"""
    for r in rows:
        head = timeutil.fmt(r['ts'])
        ch = db.display(r['chat']) + '｜' if show_chat else ''
        if not out(f'{head} {ch}{who(db, r, db.display(r["chat"]))}：{body(db, r, width)}'):
            return False
    return True


def as_json(db, rows):
    out = []
    for r in rows:
        d = {k: r[k] for k in r.keys()}
        d['time'] = timeutil.fmt(r['ts'], '%Y-%m-%d %H:%M:%S')
        d['chat_name'] = db.display(r['chat'])
        d['sender_name'] = db.display(r['sender'], r['chat'] if r['chat'].endswith('@chatroom') else None)
        d['extra'] = extra(r)
        out.append(d)
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + '\n')
