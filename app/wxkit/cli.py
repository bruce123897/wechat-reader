"""wx 命令。每个子命令一个函数（cmd_xxx），参数定义在文件末尾的 build()。
新增命令：写一个 cmd_xxx(db, a)，再在 build() 里加一段。
"""
import argparse
import csv
import html
import io
import json
import os
import re
import sqlite3
import sys
import time

from . import archive, config, keys, render, timeutil
from .query import DB, extra, is_noise

EVENT_KINDS = ('recall', 'friend_added', 'group_join', 'group_leave', 'group_rename', 'group_admin',
               'group_disband', 'payment', 'redpacket_open')


def _range(a, default):
    since = timeutil.when(a.since) if getattr(a, 'since', None) else (timeutil.when(default) if default else None)
    until = timeutil.until(a.until) if getattr(a, 'until', None) else None
    return since, until


def _out(a):
    return render.Out(0 if getattr(a, 'full', False) else a.max)


def _width(a, normal):
    return 0 if getattr(a, 'full', False) else normal


# ---------- 新消息 ----------
def _cursor_file(db):
    return os.path.join(db.acct.home, 'cursor.json')


def _freshness(db):
    """档案库落后多久了（后台没跑时提醒）。"""
    from . import daemon
    st = daemon.read_status()
    hb = st.get('heartbeat', 0)
    if time.time() - hb > 90:
        return f'⚠ 后台同步没在运行（上次 {timeutil.fmt(hb) if hb else "从未"}），结果可能不是最新：先 wx sync 或 wx daemon start'
    return ''


def _split_private_group(rows):
    priv, grp = [], []
    for r in rows:
        if is_noise(r['chat']):
            continue
        (grp if r['chat'].endswith('@chatroom') else priv).append(r)
    return priv, grp


def cmd_new(db, a):
    try:
        cur = json.load(open(_cursor_file(db)))['ts']
    except (OSError, ValueError, KeyError):
        cur = int(time.time()) - 7200
    since = timeutil.when(a.since) if a.since else cur
    now = int(time.time())
    rows = db.msgs(since=since, until=now + 1, not_kinds=['emoji'] if not a.full else None)
    priv, grp = _split_private_group(rows)
    out = _out(a)
    w = _freshness(db)
    if w:
        out(w)
    out(f'新消息 {timeutil.fmt(since)} → {timeutil.fmt(now)}：私聊 {len({r["chat"] for r in priv})} 人 {len(priv)} 条，'
        f'群 {len({r["chat"] for r in grp})} 个 {len(grp)} 条')
    render.by_chat(db, priv, out, _width(a, 300))
    if grp and not a.groups:
        stat = {}
        for r in grp:
            s = stat.setdefault(r['chat'], [0, []]); s[0] += 1
            if r['at'] == 'me' or (r['kind'] == 'quote' and extra(r).get('ref_who') and not r['is_me'] and _quotes_me(db, r)):
                s[1].append(r)
        out('■ 群（条数；@我 / 引用我的单列）')
        for chat, (n, hits) in sorted(stat.items(), key=lambda kv: -kv[1][0]):
            out(f'  {db.display(chat)} {n}条' + (f'，@我/引用我 {len(hits)} 条' if hits else ''))
            if hits:
                render.lines(db, hits, lambda s: out('    ' + s), _width(a, 300))
    elif grp:
        render.by_chat(db, grp, out, _width(a, 200))
    out.done('用 wx new --since 2h 缩小，或 wx read <名字> 看单个会话')
    if not a.peek:
        json.dump({'ts': now + 1}, open(_cursor_file(db), 'w'))


def _quotes_me(db, r):
    me = db.names.get(db.me)
    my_names = {me['nick'], me['remark']} - {''} if me else set()
    return extra(r).get('ref_who') in my_names


def cmd_since(db, a):
    """旧命令：某时间之后的私聊新消息（--groups 含群）。"""
    since = timeutil.when(a.when)
    rows = db.msgs(since=since, not_kinds=['emoji', 'system', 'channels'])
    priv, grp = _split_private_group(rows)
    out = _out(a)
    w = _freshness(db)
    if w:
        out(w)
    render.by_chat(db, priv + (grp if a.groups else []), out, _width(a, 300))
    out.done('时间往后挪一点，或 wx read <名字>')


def cmd_inbox(db, a):
    out = _out(a)
    since = timeutil.when(a.since) if a.since else 0
    n = a.n if a.n is not None else (10 ** 6 if a.since else 30)   # 给了 --since 就列出时间段内全部
    q = ('select * from sessions where hidden=0 and last_ts>=? ' + ('and unread>0 ' if a.unread else '') +
         'order by sort_ts desc limit ?')
    for s in db.c.execute(q, (since, n)):
        if is_noise(s['chat']):
            continue
        un = f'[未读{s["unread"]}] ' if s['unread'] else ''
        g = '群 ' if s['chat'].endswith('@chatroom') else ''
        summ = (s['summary'] or '').replace('\n', '⏎')[:60]
        if not out(f'{timeutil.fmt(s["last_ts"] or 0)} {g}{db.display(s["chat"])} {un}{summ}'):
            break
    out.done('加 -n 调小')


# ---------- 人与群 ----------
def cmd_find(db, a):
    out = _out(a)
    res = db.resolve(a.q)
    labels = dict(db.c.execute('select id, name from labels'))
    if not res:
        out(f'没找到 "{a.q}"'); return
    out('显示名 | 备注 | 昵称 | wxid | 微信号 | 类型 | 标签 | 消息数 | 最后聊天')
    for u in res[:a.n]:
        n = db.names.get(u)
        cnt, last = db.c.execute('select count(*), max(ts) from msgs where chat=?', (u,)).fetchone()
        lab = '、'.join(labels.get(int(x), x) for x in (n['labels'] or '').split(',') if x.strip().isdigit()) if n else ''
        out(' | '.join([db.display(u), n['remark'] if n else '', n['nick'] if n else '', u, n['alias'] if n else '',
                        db.kind_of(u), lab, str(cnt), timeutil.fmt(last) if last else '-']))
    if len(res) > a.n:
        out(f'…共 {len(res)} 个，加 -n 看更多')


def cmd_members(db, a):
    room = db.chat(a.group)
    out = _out(a)
    rows = list(db.c.execute('select user, gnick from members where room=?', (room,)))
    hits = [(u, g) for u, g in rows if not a.q or a.q.lower() in f'{u} {g} {db.display(u)}'.lower()]
    out(f'{db.display(room)}：{len(rows)} 人' + (f'，匹配 {len(hits)}' if a.q else ''))
    for u, g in hits:
        n = db.names.get(u)
        if not out(f'{db.display(u, room)} | 群昵称:{g or "-"} | {u}' + (' | 好友' if n and n['kind'] == 'friend' else '')):
            break
    out.done('加关键词过滤：wx members 群名 关键词')


def cmd_groups(db, a):
    """某人在哪些群（只算我也在的群）。"""
    out = _out(a)
    for who in [x.strip() for x in re.split('[,，]', a.who) if x.strip()]:
        users = db.resolve(who)
        if not users:
            out(f'!! 找不到 "{who}"'); continue
        for u in users[:3]:
            rows = list(db.c.execute('select room, gnick from members where user=?', (u,)))
            rows.sort(key=lambda r: -(db.last_ts(r[0]) or 0))
            out(f'■ {db.display(u)}（{u}）在 {len(rows)} 个群')
            for room, g in rows:
                lt = db.last_ts(room)
                if not out(f'  {db.display(room)}' + (f' | 群昵称:{g}' if g else '') + f" | 最后消息 {timeutil.fmt(lt) if lt else '-'}"):
                    break
    out.done('写得更具体，或一次只查一个人')


def cmd_labels(db, a):
    out = _out(a)
    labels = list(db.c.execute('select id, name from labels'))
    by = {}
    for u, ls in db.c.execute("select user, labels from names where labels != ''"):
        for x in ls.split(','):
            if x.strip().isdigit():
                by.setdefault(int(x), []).append(u)
    for i, name in labels:
        if a.name and a.name not in name:
            continue
        us = by.get(i, [])
        out(f'{name}（{len(us)}人）' + ('：' + '、'.join(db.display(u) for u in us) if a.name else ''))
    out.done('wx labels 标签名 看成员')


# ---------- 读与搜 ----------
def _chats(db, who):
    """'张三' 或 '张三,李四'（逗号分隔多个）→ 会话 wxid 列表。"""
    parts = [x.strip() for x in re.split('[,，]', who) if x.strip()]
    return [db.chat(x) for x in parts] if len(parts) > 1 else [db.chat(who)]


def cmd_read(db, a):
    chats = _chats(db, a.who)
    out = _out(a)
    w = _freshness(db)
    if w and not a.json:
        out(w)
    rows = [r for chat in chats for r in _read_one(db, a, chat, out)]
    if a.json:
        return render.as_json(db, rows)
    out.done('用 -n、--since/--until 缩小')


def _read_one(db, a, chat, out):
    since, until = _range(a, None)
    kinds = a.kind.split(',') if a.kind else None
    if a.around:
        t = timeutil.when(a.around)
        rows = db.msgs(chat=chat, until=t, limit=a.n // 2, newest=True, kinds=kinds) + \
            db.msgs(chat=chat, since=t, limit=a.n - a.n // 2, kinds=kinds)
    elif since:
        rows = db.msgs(chat=chat, since=since, until=until, kinds=kinds, limit=None if a.all else a.n,
                       newest=not a.all)
    else:
        rows = db.msgs(chat=chat, until=until, kinds=kinds, limit=a.n, newest=True)
    if not a.json:
        out(f'■ {db.display(chat)}（{len(rows)}条' + (f'，{timeutil.fmt(rows[0]["ts"])} 起' if rows else '') + '）')
        render.lines(db, rows, out, _width(a, 800))
    return rows


def _window(db, hit, n):
    """命中行在同一会话里前后各 n 条（按 ts, local_id 顺序），结果含命中行、按时间正序。"""
    chat, ts, lid = hit['chat'], hit['ts'], hit['local_id']
    before = db.c.execute('select * from msgs where chat=? and (ts<? or (ts=? and local_id<?)) '
                          'order by ts desc, local_id desc limit ?', (chat, ts, ts, lid, n)).fetchall()
    after = db.c.execute('select * from msgs where chat=? and (ts>? or (ts=? and local_id>?)) '
                         'order by ts, local_id limit ?', (chat, ts, ts, lid, n)).fetchall()
    return before[::-1] + [hit] + after


def _ctx_blocks(db, hits, n):
    """把每条命中的上下文窗口按会话聚合，重叠/相邻的合并成一段。返回 [(chat, rows)] 和命中 id 集合。"""
    hit_ids = {r['id'] for r in hits}
    per_chat = {}
    for h in hits:
        per_chat.setdefault(h['chat'], []).append(h)
    blocks = []
    for chat, hs in per_chat.items():
        rows, contig = {}, set()
        for h in hs:
            win = _window(db, h, n)
            for r in win:
                rows[r['id']] = r
            for a_, b_ in zip(win, win[1:]):
                contig.add((a_['id'], b_['id']))   # 同一窗口内相邻 → 之间没有缺口
        cur = []
        for r in sorted(rows.values(), key=lambda r: (r['ts'], r['local_id'])):
            if cur and (cur[-1]['id'], r['id']) not in contig:
                blocks.append((chat, cur)); cur = []
            cur.append(r)
        if cur:
            blocks.append((chat, cur))
    blocks.sort(key=lambda b: b[1][0]['ts'])
    return blocks, hit_ids


def cmd_search(db, a):
    since, until = _range(a, None if a.all_time else f'{a.days_opt or a.days}d')
    chats = _chats(db, a.within) if a.within else None
    chat = chats[0] if chats and len(chats) == 1 else None
    sender = None
    if a.sender:
        sender = db.me if a.sender in ('我', 'me') else (db.resolve(a.sender) or [a.sender])[0]
    rows = db.msgs(chat=chat, chats=chats if chats and not chat else None, since=since, until=until, q=a.q,
                   sender=sender, kinds=a.kind.split(',') if a.kind else None, limit=a.n, newest=True)
    if a.json:
        return render.as_json(db, rows)
    out = _out(a)
    if not rows:
        out(f'没搜到（范围：{"全部" if not since else timeutil.fmt(since) + " 起"}）。可加 --days 365 或 --all-time')
        return
    if len(rows) == a.n:
        out(f'（只显示最近 {a.n} 条，-n 调大）')
    if a.ctx:   # 带上下文：每条命中前后各 N 条，» 标命中行
        blocks, hit_ids = _ctx_blocks(db, rows, a.ctx)
        out(f'命中 {len(rows)} 条，每条前后各 {a.ctx} 行（» 是命中行；相邻的已合并）：')
        for c, brows in blocks:
            if not out(f'■ {db.display(c)}'):
                break
            if not render.lines(db, brows, out, _width(a, 200), mark=hit_ids):
                break
        out.done('缩短 --days、调小 --ctx，或 wx read <名字> 看整段')
        return
    render.flat(db, rows, out, _width(a, 200), show_chat=not chat)
    out.done('加 --in 某人、--ctx N 看上下文，或缩短 --days')


def cmd_msg(db, a):
    for mid in a.ids:
        r = db.get(int(mid))
        if not r:
            print(f'#{mid} 不存在'); continue
        print(f'#{mid} {timeutil.fmt(r["ts"], "%Y-%m-%d %H:%M:%S")} {db.display(r["chat"])}｜{render.who(db, r)} [{r["kind"]}]')
        print(render.body(db, r, 0))
        ex = extra(r)
        if ex and a.extra:
            print(json.dumps(ex, ensure_ascii=False))


# ---------- 钱 ----------
def transfers(db, since, until=None, chat=None):
    """转账按单号去重。返回 [{ts, chat, dir(+1 收 / -1 付), amount, state, tid, memo}]。
    方向：发起转账的人是付款方，点“收款”的人是收款方（全部历史上两者 100% 一致）。
    “已收款”那条消息里的 receiver 字段不是收款人，不能用。"""
    by = {}
    for r in db.msgs(chat=chat, since=since, until=until, kinds=['transfer']):
        ex = extra(r)
        tid = ex.get('tid') or f'id{r["id"]}'
        t = by.setdefault(tid, {'ts': r['ts'], 'chat': r['chat'], 'amount': ex.get('amount'), 'tid': tid,
                                'subs': set(), 'memo': ex.get('memo') or '', 'pay': 0, 'acc': 0})
        sub = ex.get('paysub'); group = r['chat'].endswith('@chatroom')
        t['subs'].add(sub)
        if sub == '1':
            t['pay'] = -1 if r['is_me'] else 1 if ex.get('receiver') == db.me or not group else 0
        elif sub in ('3', '8'):
            t['acc'] = 1 if r['is_me'] else -1 if not group else 0
    out = []
    for t in by.values():
        pay, acc = t.pop('pay'), t.pop('acc')
        t['dir'] = pay or acc
        if not t['dir']:
            continue   # 群里别人之间的转账，跟我无关
        s = t.pop('subs')
        t['state'] = '已退还' if '4' in s else '已收款' if s & {'3', '8'} else '待收款'
        out.append(t)
    return sorted(out, key=lambda t: t['ts'])


def cmd_pay(db, a):
    since, until = _range(a, f'{a.days}d')
    chat = db.chat(a.who) if a.who else None
    ts = transfers(db, since, until, chat)
    if a.amount is not None:
        ts = [t for t in ts if t['amount'] is not None and abs(t['amount'] - a.amount) < 0.005]
    if a.json:
        print(json.dumps([dict(t, chat_name=db.display(t['chat']), time=timeutil.fmt(t['ts'], '%Y-%m-%d %H:%M:%S'))
                          for t in ts], ensure_ascii=False)); return
    out = _out(a)
    out(f'转账 {timeutil.fmt(since)} 起：{len(ts)} 笔（按单号去重；← 收 → 付；没写状态的都已收款）')

    def item(t, day):
        s = ('←' if t['dir'] > 0 else '→') + f"{t['amount'] or 0:g} {timeutil.fmt(t['ts'], '%H:%M' if day else '%m-%d %H:%M')}"
        s += '' if t['state'] == '已收款' else ' ' + t['state']
        s += f" 尾{t['tid'][-8:]}" if a.ids else ''
        return s + (f" 备注:{t['memo']}" if t['memo'] else '')

    if a.list:   # 逐笔时间线
        for t in ts:
            out(f"{db.display(t['chat'])} {item(t, False)}")
    by = {}
    for t in ts:
        by.setdefault(t['chat'], []).append(t)
    net = {c: sum((t['amount'] or 0) * t['dir'] for t in g if t['state'] != '已退还') for c, g in by.items()}
    if not a.list:   # 默认：每个会话一行，按净额排
        for c in sorted(by, key=lambda c: -net[c]):
            g = by[c]; prev = None; parts = []
            for t in g:
                d = timeutil.fmt(t['ts'], '%m-%d')
                parts.append(item(t, d == prev)); prev = d
            out(f"{db.display(c)} {net[c]:+g}：" + ' '.join(parts))
    if net:
        inc = sum(v for v in net.values() if v > 0); exp = sum(v for v in net.values() if v < 0)
        out(f'合计 收{inc:g} 付{-exp:g} 净{inc + exp:+g}')
    rp = [] if a.amount is not None else db.msgs(chat=chat, since=since, until=until, kinds=['redpacket'])
    if rp:
        mine = sum(1 for r in rp if r['is_me'])
        out(f'— 红包 {len(rp)} 个（我发 {mine}，收到 {len(rp) - mine}；微信不在本地存红包金额）')
    out.done('缩短 --days 或加 --who')


# ---------- 事件 / 撤回 ----------
def cmd_events(db, a):
    since, until = _range(a, f'{a.days}d')
    kinds = a.kind.split(',') if a.kind else list(EVENT_KINDS)
    rows = db.msgs(since=since, until=until, kinds=kinds)
    rec = []
    if 'recall' in kinds:   # 撤回了、但原文已保存的
        rec = db.c.execute("select * from msgs where recalled_at>=? and kind!='recall' order by ts", (since,)).fetchall()
    out = _out(a)
    if rec:
        out(f'■ 撤回的消息（原文已保存）{len(rec)} 条')
        render.flat(db, rec, out, _width(a, 300))
    # 刷屏的两类（领红包、没捕获原文的撤回提示）只按会话计数，除非用 --kind 点名要看
    noisy = set() if a.kind else {'redpacket_open', 'recall'}
    cnt = {}
    for r in rows:
        if r['kind'] in noisy:
            cnt.setdefault(r['kind'], {}).setdefault(r['chat'], 0)
            cnt[r['kind']][r['chat']] += 1
    rest = [r for r in rows if r['kind'] not in noisy]
    if rest:
        out(f'■ 事件 {len(rest)} 条')
        render.flat(db, rest, out, _width(a, 150))
    for k, label in (('redpacket_open', '领红包'), ('recall', '撤回提示（原文未捕获）')):
        if cnt.get(k):
            top = sorted(cnt[k].items(), key=lambda kv: -kv[1])
            out(f'■ {label} {sum(cnt[k].values())} 条：' + '，'.join(f'{db.display(c)} {n}' for c, n in top[:8])
                + ('…' if len(top) > 8 else '') + f'（明细：--kind {k}）')
    if not rows and not rec:
        out('这段时间没有事件')
    out.done('缩短 --days，或 --kind friend_added,group_join')


def cmd_recalls(db, a):
    since, until = _range(a, f'{a.days}d')
    rows = list(db.c.execute('select * from msgs where (recalled_at>=? or (kind=? and ts>=?)) order by ts',
                             (since, 'recall', since)))
    if a.who:
        chat = db.chat(a.who); rows = [r for r in rows if r['chat'] == chat]
    out = _out(a)
    got = [r for r in rows if r['kind'] != 'recall']
    out(f'撤回 {len(rows)} 条，其中原文已保存 {len(got)} 条（其余是本工具开始运行前撤回的）')
    render.flat(db, got if not a.all else rows, out, _width(a, 300))
    out.done('缩短 --days')


# ---------- 统计 ----------
def cmd_stats(db, a):
    since, until = _range(a, f'{a.days}d')
    out = _out(a)
    if a.who:
        chat = db.chat(a.who)
        w, args = 'chat=? and ts>=?' + (' and ts<?' if until else ''), [chat, since] + ([until] if until else [])
        n, me, first, last = db.c.execute(f'select count(*), sum(is_me), min(ts), max(ts) from msgs where {w}', args).fetchone()
        out(f'{db.display(chat)} {timeutil.fmt(since)} 起：{n} 条（我 {me or 0}，对方 {n - (me or 0)}）'
            + (f'，{timeutil.fmt(first)} ~ {timeutil.fmt(last)}' if n else ''))
        kinds = db.c.execute(f'select kind, count(*) from msgs where {w} group by kind order by 2 desc', args).fetchall()
        out('类型：' + '，'.join(f'{k} {c}' for k, c in kinds))
        days = db.c.execute(f"select date(ts+28800,'unixepoch') d, count(*) from msgs where {w} group by d order by d desc limit 14", args).fetchall()
        out('每天：' + '，'.join(f'{d[5:]} {c}' for d, c in days))
        if chat.endswith('@chatroom'):
            top = db.c.execute(f'select sender, count(*) from msgs where {w} group by sender order by 2 desc limit 10', args).fetchall()
            out('发言最多：' + '，'.join(f'{db.display(s, chat)} {c}' for s, c in top))
        return
    rows = db.c.execute('select chat, count(*), sum(is_me), max(ts) from msgs where ts>=? group by chat order by 2 desc limit ?',
                        (since, a.n * 3)).fetchall()
    out(f'{timeutil.fmt(since)} 起最活跃的会话（总数 | 我发 | 最后）：')
    k = 0
    for chat, n, me, last in rows:
        if is_noise(chat) or (a.private and chat.endswith('@chatroom')):
            continue
        out(f'  {db.display(chat)} | {n} | {me} | {timeutil.fmt(last)}'); k += 1
        if k >= a.n:
            break


# ---------- 导出 ----------
def cmd_export(db, a):
    chat = db.chat(a.who)
    since, until = _range(a, None)
    rows = db.msgs(chat=chat, since=since, until=until)
    name = db.display(chat)
    path = a.out or os.path.join(config.DATA, 'exports', f'{name}_{time.strftime("%Y%m%d-%H%M")}.{a.format}')
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    recs = [{'time': timeutil.fmt(r['ts'], '%Y-%m-%d %H:%M:%S'), 'sender': render.who(db, r, name),
             'kind': r['kind'], 'text': render.body(db, r, 0), 'recalled': bool(r['recalled_at'])} for r in rows]
    if a.format == 'json':
        json.dump(recs, open(path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    elif a.format == 'csv':
        with open(path, 'w', encoding='utf-8-sig', newline='') as f:
            wr = csv.DictWriter(f, fieldnames=list(recs[0]) if recs else ['time']); wr.writeheader(); wr.writerows(recs)
    elif a.format == 'html':
        body = '\n'.join(f'<p><small>{r["time"]}</small> <b>{html.escape(r["sender"])}</b>：{html.escape(r["text"])}</p>' for r in recs)
        open(path, 'w', encoding='utf-8').write(f'<!doctype html><meta charset="utf-8"><title>{html.escape(name)}</title>'
                                                f'<body style="font-family:sans-serif;max-width:800px;margin:auto">{body}</body>')
    else:
        buf = io.StringIO()
        render.lines(db, rows, lambda s: buf.write(s + '\n') or True, 0, name)
        txt = buf.getvalue()
        if a.format == 'md':
            txt = f'# {name}\n\n' + txt.replace('\n', '  \n')
        open(path, 'w', encoding='utf-8').write(txt)
    print(f'已导出 {len(rows)} 条 → {path}')


# ---------- 媒体 ----------
def cmd_media(db, a):
    from . import media
    out_dir = a.out or os.path.join(os.environ.get('TEMP', config.DATA), 'wxkit_media')
    if a.id:
        rows = [db.get(int(i)) for i in a.id.split(',')]
    else:
        chats = _chats(db, a.who) if a.who else None   # 不写人 = 所有会话
        since, until = _range(a, None)
        rows = db.msgs(chats=chats, since=since, until=until, kinds=a.kind.split(','), limit=a.n, newest=True)
    ok = 0
    for r in rows:
        if not r:
            continue
        try:
            p, note = media.export(db.acct, dict(r), out_dir)
        except Exception as e:
            p, note = None, f'失败：{e}'
        ok += p is not None
        name = db.display(r['chat']); w = render.who(db, r, name)
        label = w if a.who and ',' not in a.who or w == name else f'{name}｜{w}'   # 跨会话时带上会话名
        print(f'{timeutil.fmt(r["ts"])} {label} [{r["kind"]}] #{r["id"]} → {p or note}'
              + (f'（{note}）' if p and note else ''))
    print(f'共 {len(rows)} 个，取到 {ok} 个。图片可以直接用 Read 工具看')


# ---------- 朋友圈 / 收藏 ----------
def cmd_sns(db, a):
    since, until = _range(a, None)
    w, args = ['1=1'], []
    if a.who:
        u = db.me if a.who in ('我', 'me') else (db.resolve(a.who) or [a.who])[0]
        w.append('user=?'); args.append(u)
    if since:
        w.append('ts>=?'); args.append(since)
    if a.q:
        w.append('text like ?'); args.append(f'%{a.q}%')
    try:
        rows = db.c.execute(f'select * from sns where {" and ".join(w)} order by ts desc limit ?', args + [a.n]).fetchall()
    except sqlite3.Error:
        print('还没有朋友圈数据（后台同步一次后就有）'); return
    out = _out(a)
    for r in rows:
        bits = [f'{timeutil.fmt(r["ts"])} {db.display(r["user"])}：{(r["text"] or "").replace(chr(10), "⏎")}']
        if r['media']:
            from . import snsmedia
            cd = snsmedia.cache_dir(db.acct, r['tid'])
            fs = sorted(os.path.join(cd, f) for f in os.listdir(cd)) if os.path.isdir(cd) else []
            if fs:
                bits.append(f'[图/视频{r["media"]}张已存]')
                bits.extend(fs)
            else:
                bits.append(f'[图/视频{r["media"]}张 → wx sns-media tid:{r["tid"]}]')
        if r['link']:
            bits.append(f'[链接] {r["link"][:80]}')
        if r['loc']:
            bits.append(f'📍{r["loc"]}')
        if r['likes']:
            bits.append(f'👍{r["likes"]}')
        if r['comments']:
            bits.append(f'💬{r["comments"]}')
        if r['gone']:
            bits.append('[已删除]')
        bits.append(f'(tid:{r["tid"]})')
        if not out(' '.join(bits)):
            break
    out.done('加 -n 或 --since 缩小')


def cmd_sns_media(db, a):
    """下载朋友圈图片/视频到本地并解密。密钥全自动（都在动态 XML 里）。"""
    from . import snsmedia
    acct = db.acct
    cfg = config.load()
    use_wx_ua = a.wx_ua or bool(cfg.get('sns_video_ua'))
    max_mb = cfg.get('sns_media_max_mb')
    if getattr(a, 'backfill', False):
        import time
        since, until = _range(a, None)
        w, args = ['media>0'], []
        if since:
            w.append('ts>=?'); args.append(since)
        if until:
            w.append('ts<?'); args.append(until)
        try:
            rows = db.c.execute(f'select * from sns where {" and ".join(w)} order by ts asc', args).fetchall()
        except sqlite3.Error:
            print('还没有朋友圈数据（后台同步一次后就有）'); return
        import collections
        ok = exp = fail = 0
        why = collections.Counter()
        print(f'回填 {len(rows)} 条动态（图片+视频，限速），跑完打印结果…', flush=True)
        for r in rows:
            tid = r['tid']
            for it in snsmedia.media_for_tid(db.c, acct.mirror, tid):
                jobs = ['video' if it.get('type') == 6 else 'image']
                if it.get('live_url'):
                    jobs.append('video')   # LivePhoto 附带的小视频
                for k in jobs:
                    p, note = snsmedia.fetch_item(acct, tid, it, kind=k, thumb=False,
                                                  force=a.force, use_wx_ua=use_wx_ua, max_mb=max_mb)
                    if p:
                        ok += 1
                    elif note == '[已过期 404]':
                        exp += 1
                    else:
                        fail += 1; why[note] += 1
                    time.sleep(0.5)
        print(f'回填完成：{len(rows)} 条动态；成功 {ok}，过期 404 {exp}，失败 {fail}。')
        for k, v in why.most_common():
            print(f'  失败原因 {k}：{v}')
        return
    kinds = [k.strip() for k in a.kind.split(',') if k.strip()] or ['image']
    tid = a.tid
    who = a.who
    if tid is None and who and who.startswith('tid:'):
        tid = int(who[4:]); who = None
    if tid is not None:
        rows = db.c.execute('select * from sns where tid=?', (tid,)).fetchall()
        if not rows:
            rows = [{'tid': tid, 'user': '', 'ts': 0, 'text': '', 'media': None}]
    else:
        w, args = ['media>0'], []
        if who:
            u = db.me if who in ('我', 'me') else (db.resolve(who) or [who])[0]
            w.append('user=?'); args.append(u)
        since, until = _range(a, None)
        if since:
            w.append('ts>=?'); args.append(since)
        if until:
            w.append('ts<?'); args.append(until)
        try:
            rows = db.c.execute(f'select * from sns where {" and ".join(w)} order by ts desc limit ?',
                                args + [a.n]).fetchall()
        except sqlite3.Error:
            print('还没有朋友圈数据（后台同步一次后就有）'); return
    result = []
    for r in rows:
        tid = r['tid']
        items = snsmedia.media_for_tid(db.c, acct.mirror, tid)
        who_name = db.display(r['user']) if r['user'] else ''
        head = f'{timeutil.fmt(r["ts"]) if r["ts"] else ""} {who_name}：{(r["text"] or "")[:30].replace(chr(10), " ")}'.strip()
        post = {'tid': tid, 'head': head, 'files': []}
        for k in kinds:
            for it in items:
                p, note = snsmedia.fetch_item(acct, tid, it, kind=k, thumb=a.thumb,
                                              force=a.force, use_wx_ua=use_wx_ua,
                                              max_mb=cfg.get('sns_media_max_mb'))
                if p is None and note in ('[不是视频]', '[这是视频，用 --kind video]'):
                    continue
                post['files'].append({'i': it['i'], 'kind': k, 'path': p, 'note': note})
        result.append(post)
    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=1)); return
    for post in result:
        print(post['head'] + f'  (tid:{post["tid"]})')
        if not post['files']:
            print('  （没有可取的媒体）')
        for f in post['files']:
            print(f'  [{f["kind"]} {f["i"]}] ' + (f['path'] or f['note']))
    print('图片可直接用 Read 工具看；视频是 mp4。过期链接显示 [已过期 404]。')


def cmd_fav(db, a):
    out = _out(a)
    w, args = '1=1', []
    if a.q:
        w, args = 'text like ?', [f'%{a.q}%']
    try:
        rows = db.c.execute(f'select * from favs where {w} order by ts desc limit ?', args + [a.n]).fetchall()
    except sqlite3.Error:
        print('还没有收藏数据'); return
    for r in rows:
        if not out(f'{timeutil.fmt(r["ts"])} {db.display(r["src"]) if r["src"] else ""}：{r["text"][:_width(a, 300) or None]}'):
            break
    out.done('加关键词或 -n')


# ---------- 高级 ----------
def cmd_sql(db, a):
    q = a.query.strip()
    if not q.lower().startswith(('select', 'with', 'pragma table_info')):
        raise SystemExit('!! 只允许查询（select）')
    cur = db.c.execute(q)
    out = _out(a)
    out('\t'.join(d[0] for d in cur.description))
    for row in cur:
        if not out('\t'.join('' if v is None else str(v).replace('\n', '⏎') for v in row)):
            break
    out.done('加 limit')


# ---------- 运维 ----------
def cmd_status(_db, a):
    from . import daemon, voice
    acct = config.account()
    st = daemon.read_status()
    run = daemon.is_running()
    hb = st.get('heartbeat', 0)
    print(f'账号：{acct.name}')
    print(f'后台同步：{"运行中" if run else "没运行"}' + (f'（pid {st.get("pid")}，心跳 {int(time.time() - hb)} 秒前，'
          f'上轮 {st.get("took")}s，累计 {st.get("cycles")} 轮）' if run and hb else '') +
          (f'；最近错误：{st["error"]}' if st.get('error') else ''))
    if os.path.exists(acct.archive):
        c = archive.connect(acct)
        n, last = c.execute('select count(*), max(ts) from msgs').fetchone()
        rec = c.execute('select count(*) from msgs where recalled_at is not null and kind != "recall"').fetchone()[0]
        print(f'档案库：{n} 条消息，最新 {timeutil.fmt(last) if last else "-"}；已保存撤回原文 {rec} 条；'
              f'大小 {os.path.getsize(acct.archive) >> 20}MB')
        print(f'语音：待转 {voice.pending(c)} 条' + ('' if voice.model_dir() else '（没有语音模型）'))
    else:
        print('档案库：还没建（wx sync）')
    print('密钥：' + ('已保存' if keys.load(acct).get('db') else '没有（wx key）') +
          '；图片密钥：' + ('有' if keys.load(acct).get('img_aes') else '没有（V2 图片解不开，wx key --image）'))


def cmd_doctor(_db, a):
    from . import daemon, media, voice
    ok = True

    def chk(name, good, hint=''):
        nonlocal ok
        ok &= bool(good)
        print(f'{"✓" if good else "✗"} {name}' + ('' if good else f' —— {hint}'))
    for mod in ('Crypto', 'zstandard', 'pilk', 'sherpa_onnx'):
        try:
            __import__(mod); chk(f'依赖 {mod}', True)
        except ImportError:
            chk(f'依赖 {mod}', False, '重新运行安装脚本')
    try:
        acct = config.account(); chk(f'微信账号 {acct.name}', True)
    except SystemExit as e:
        chk('微信账号', False, str(e)); return
    try:
        keys.db_key(acct); chk('数据库密钥', True)
    except SystemExit as e:
        chk('数据库密钥', False, str(e))
    chk('图片密钥', keys.load(acct).get('img_aes'), 'wx key --image')
    chk('ffmpeg（部分图片需要）', media.ffmpeg(), '装 ffmpeg 或 wx config ffmpeg <路径>')
    chk('语音模型', voice.model_dir(), '运行安装脚本下载')
    chk('后台同步在运行', daemon.is_running(), 'wx daemon start；开机自启：wx daemon install')
    if sys.platform == 'win32':
        import subprocess
        r = subprocess.run(['schtasks', '/query', '/tn', daemon.TASK], capture_output=True)
        chk('开机自启计划任务', r.returncode == 0, 'wx daemon install')
    from . import SIGN, CONTACT
    print('全部正常' if ok else f'有问题，按提示处理；搞不定就把这段截图发给开发者（{CONTACT}）')
    print(SIGN)


def cmd_sync(_db, a):
    from . import daemon, ingest, mirror
    acct = config.account()
    if daemon.is_running() and not a.reparse:
        hb = daemon.read_status().get('last_cycle', 0)
        t0 = time.time()
        while time.time() - t0 < 25:
            if daemon.read_status().get('last_cycle', 0) > max(hb, t0 - 1):
                break
            time.sleep(0.5)
        print('后台同步在运行，已是最新（上一轮 ' + timeutil.fmt(daemon.read_status().get('last_cycle', 0), '%H:%M:%S') + '）')
        return
    lk = daemon.Lock(daemon.lock_path())
    if not lk.acquire():
        raise SystemExit('!! 后台程序正在运行，--reparse 前先 wx daemon stop')
    t = time.time()
    changed = mirror.Mirror(acct, keys.db_key(acct)).update_all()
    ing = ingest.Ingest(acct)
    res = ing.reparse() if a.reparse else ing.run(changed)
    new = sum(v[0] for k, v in res.items() if k.startswith('message_'))
    print(f'同步完成：新消息 {new} 条，用时 {time.time() - t:.1f}s' + ('（已按新规则重新整理全部消息）' if a.reparse else ''))


def cmd_daemon(_db, a):
    from . import daemon
    if a.action == 'run':
        daemon.Daemon().run()
    elif a.action == 'start':
        if daemon.is_running():
            print('已经在运行'); return
        import subprocess
        exe = sys.executable
        w = os.path.join(os.path.dirname(exe), 'pythonw.exe')
        subprocess.Popen([w if os.path.exists(w) else exe, daemon._entry(), 'daemon', 'run'],
                         cwd=os.path.dirname(daemon._entry()), creationflags=0x00000008 | 0x00000200 if sys.platform == 'win32' else 0,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        for _ in range(20):
            time.sleep(0.5)
            if daemon.is_running():
                print('已启动'); return
        print('!! 没启动起来，看日志 ' + os.path.join(config.log_dir(), 'daemon.log'))
    elif a.action == 'stop':
        pid = daemon.stop(); print(f'已停止 pid {pid}' if pid else '本来就没运行')
    elif a.action == 'install':
        daemon.install_task(); print(f'已设置开机自启（计划任务 {daemon.TASK}，每 5 分钟检查一次，挂了自动拉起），并已启动')
    elif a.action == 'uninstall':
        daemon.uninstall_task(); print('已取消开机自启（后台程序本身用 wx daemon stop 停）')
    else:
        cmd_status(None, a)


def cmd_key(_db, a):
    from . import keygrab
    if a.image:
        keygrab.image_key_interactive(config.account()); return
    try:
        acct = config.account()
    except SystemExit:
        acct = None   # 还没登录过微信：capture 会把微信拉起来，等你扫码登录
    ok = keygrab.db_key_interactive(
        acct, supplied=a.set,
        timeout=getattr(a, 'timeout', None) or 180,
        force=getattr(a, 'force', False),
        relaunch=getattr(a, 'relaunch', False),
        launch=not getattr(a, 'no_launch', False))
    if not ok:
        raise SystemExit(1)


def cmd_config(_db, a):
    cfg = config.load()
    if a.k is None:
        for k, v in cfg.items():
            print(f'{k} = {v}')
        return
    if a.k not in config.DEFAULTS:
        raise SystemExit(f'!! 没有配置项 {a.k}，可选：' + '、'.join(config.DEFAULTS))
    v = a.v
    if isinstance(config.DEFAULTS[a.k], bool) and v is not None:
        v = str(v).strip().lower() in ('1', 'true', 'yes', 'on', '是', '开')
    elif isinstance(config.DEFAULTS[a.k], int) and v is not None:
        v = int(v)
    cfg[a.k] = v if v not in ('none', 'None', '') else None
    config.save(cfg); print(f'{a.k} = {cfg[a.k]}')


def cmd_voice(db, a):
    from . import voice
    c = db.c
    st = dict(c.execute('select state, count(*) from voices group by state').fetchall())
    print(f'语音：已转 {st.get(1, 0)}，待转 {st.get(0, 0)}，本地没有数据 {st.get(2, 0)}' +
          ('' if voice.model_dir() else '；没有语音模型'))
    if a.run:
        acct = config.account()
        print(f'转写了 {voice.work(acct, budget=a.run)} 条')


# ---------- 参数 ----------
NO_DB = {'status', 'doctor', 'sync', 'daemon', 'key', 'config'}


def build():
    from . import SIGN
    p = argparse.ArgumentParser(prog='wx', description=SIGN + '\n让 AI Agent 直接读电脑上的微信聊天记录（只读）。wx <命令> -h 看各命令用法。',
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('-V', '--version', action='version', version=SIGN)
    p.add_argument('--account', help='多账号时指定（wxid 或目录名开头）')
    sub = p.add_subparsers(dest='cmd', metavar='命令')

    def add(name, fn, help_, aliases=(), maxc=True):
        s = sub.add_parser(name, help=help_, aliases=list(aliases))
        s.set_defaults(fn=fn)
        if maxc:
            s.add_argument('--max', type=int, default=config.load()['max_chars'], help='输出上限（字）')
            s.add_argument('--full', action='store_true', help='不截断')
        return s

    def rng(s, since_help='起始时间：2h / 3d / 14:00 / 10-17 / "10-17 14:00" / 今天'):
        s.add_argument('--since', help=since_help); s.add_argument('--until', help='结束时间（同上）')

    s = add('new', cmd_new, '上次看过之后的新消息（私聊全文，群只给条数和 @我）')
    s.add_argument('--since', help='从这个时间算，不用上次的位置'); s.add_argument('--groups', action='store_true', help='群消息也全文')
    s.add_argument('--peek', action='store_true', help='只看不记位置')
    s = add('since', cmd_since, '某时间之后的私聊新消息（旧命令）')
    s.add_argument('when'); s.add_argument('--groups', action='store_true')
    s = add('inbox', cmd_inbox, '会话列表（像微信首页）'); s.add_argument('-n', type=int, help='最多几个，默认 30')
    s.add_argument('--unread', action='store_true', help='只看有未读的'); s.add_argument('--since', help='只看这之后有消息的会话，如 24h')
    s = add('find', cmd_find, '找人/群：备注、昵称、微信号、wxid'); s.add_argument('q'); s.add_argument('-n', type=int, default=15)
    s = add('read', cmd_read, '看一个会话', aliases=['msgs'])
    s.add_argument('who', help='名字或 wxid；逗号分隔可一次看多个'); s.add_argument('n', nargs='?', type=int, default=50, help='条数，默认 50'); rng(s)
    s.add_argument('--around', help='某时间点前后各 n/2 条'); s.add_argument('--kind', help='只看某类：image,file,transfer…')
    s.add_argument('--all', action='store_true', help='配合 --since：时间段内全部'); s.add_argument('--json', action='store_true')
    s = add('search', cmd_search, '全文搜索（多个词空格分开都要包含；re:开头是正则）', aliases=['grep'])
    s.add_argument('q'); s.add_argument('days', nargs='?', type=int, default=30, help='最近多少天，默认 30'); rng(s)
    s.add_argument('--days', dest='days_opt', type=int, help='同上')
    s.add_argument('--in', dest='within', help='只在某会话里搜（逗号分隔可多个）'); s.add_argument('--from', dest='sender', help='只看某人发的（我 = 自己）')
    s.add_argument('--ctx', type=int, default=2, metavar='N', help='每条命中前后各带 N 条上下文，默认 2（--ctx 0 只看命中行；相邻自动合并，» 标命中行）')
    s.add_argument('--kind'); s.add_argument('-n', type=int, default=60); s.add_argument('--all-time', action='store_true')
    s.add_argument('--json', action='store_true')
    s = add('msg', cmd_msg, '按编号看消息全文（输出里的 #编号）'); s.add_argument('ids', nargs='+')
    s.add_argument('--extra', action='store_true', help='附带原始字段（转账单号、文件 md5 等）')
    s = add('pay', cmd_pay, '转账（按单号去重，分收支，每会话净额）+ 红包')
    s.add_argument('days', nargs='?', type=int, default=3); rng(s); s.add_argument('--who'); s.add_argument('--json', action='store_true')
    s.add_argument('--list', action='store_true', help='逐笔按时间列出（默认按会话归并）')
    s.add_argument('--ids', action='store_true', help='显示单号尾号（和微信账单对单号时用）')
    s.add_argument('--amount', type=float, help='只看这个金额的转账')
    s = add('events', cmd_events, '撤回、加好友、进退群、改群名等事件')
    s.add_argument('days', nargs='?', type=int, default=1); rng(s); s.add_argument('--kind')
    s = add('recalls', cmd_recalls, '被撤回的消息和原文'); s.add_argument('days', nargs='?', type=int, default=7); rng(s)
    s.add_argument('--who'); s.add_argument('--all', action='store_true', help='也列出没捕获到原文的')
    s = add('members', cmd_members, '群成员'); s.add_argument('group'); s.add_argument('q', nargs='?')
    s = add('groups', cmd_groups, '某人在哪些群（逗号分隔可查多人）'); s.add_argument('who')
    s = add('labels', cmd_labels, '标签及成员'); s.add_argument('name', nargs='?')
    s = add('stats', cmd_stats, '统计：某会话的量和分布，或最活跃的会话'); s.add_argument('who', nargs='?')
    s.add_argument('--days', type=int, default=30); rng(s); s.add_argument('-n', type=int, default=20)
    s.add_argument('--private', action='store_true', help='排行只看私聊')
    s = add('export', cmd_export, '导出会话到文件（md/txt/json/csv/html）', maxc=False); s.add_argument('who'); rng(s)
    s.add_argument('--format', default='md', choices=['md', 'txt', 'json', 'csv', 'html']); s.add_argument('--out')
    s = add('media', cmd_media, '取图片/视频/文件到本地（图片自动解密）', aliases=['img'], maxc=False)
    s.add_argument('who', nargs='?'); s.add_argument('-n', type=int, default=5); rng(s)
    s.add_argument('--kind', default='image', help='image / video / file，可逗号组合'); s.add_argument('--id', help='按消息编号取')
    s.add_argument('--out', help='输出目录')
    s = add('sns', cmd_sns, '朋友圈文字（含点赞评论；别人删了也留着）。图/视频用 sns-media'); s.add_argument('who', nargs='?'); rng(s)
    s.add_argument('-n', type=int, default=10); s.add_argument('-q', help='关键词')
    s = add('sns-media', cmd_sns_media, '下载朋友圈图片/视频到本地并自动解密（图直接 Read 看）', aliases=['snsimg'], maxc=False)
    s.add_argument('who', nargs='?', help='名字，或 tid:<数字>（负号 tid 用 tid: 前缀，避免被当成参数）')
    s.add_argument('--tid', type=int, help='指定一条动态的 tid'); rng(s)
    s.add_argument('-n', type=int, default=10, help='最多几条动态，默认 10')
    s.add_argument('--kind', default='image', help='image / video，可逗号组合，默认 image')
    s.add_argument('--thumb', action='store_true', help='只取缩略图（快、小）')
    s.add_argument('--force', action='store_true', help='忽略缓存重新下载')
    s.add_argument('--wx-ua', dest='wx_ua', action='store_true', help='下载视频时用微信 UA（视频 CDN 需要；也可 wx config sns_video_ua true）')
    s.add_argument('--backfill', action='store_true', help='批量回填：扫描时间范围内所有动态，图片+视频都下载解密（限速）')
    s.add_argument('--json', action='store_true')
    s = add('fav', cmd_fav, '收藏'); s.add_argument('q', nargs='?'); s.add_argument('-n', type=int, default=20)
    s = add('sql', cmd_sql, '直接查档案库（只读 SQL；表结构见 references/internals.md）'); s.add_argument('query')
    add('status', cmd_status, '运行状态', maxc=False)
    add('doctor', cmd_doctor, '全面自检', maxc=False)
    s = add('sync', cmd_sync, '立即同步一次（后台在跑时只等它跑完一轮）', maxc=False)
    s.add_argument('--reparse', action='store_true', help='整理规则更新后重新整理全部消息')
    s = add('daemon', cmd_daemon, '后台同步：start / stop / install（开机自启）/ uninstall / run', maxc=False)
    s.add_argument('action', nargs='?', default='status', choices=['run', 'start', 'stop', 'install', 'uninstall', 'status'])
    s = add('key', cmd_key, '密钥：检查 / 自动获取（挂钩微信登录捕获）/ 手动给（--set）/ 图片密钥（--image）', maxc=False)
    s.add_argument('--grab', action='store_true', help='（兼容旧用法）重新获取')
    s.add_argument('--force', action='store_true', help='即使已有有效密钥也重新捕获一次')
    s.add_argument('--relaunch', action='store_true', help='先关微信再启动，强制走一次干净的登录捕获')
    s.add_argument('--no-launch', dest='no_launch', action='store_true', help='不自动启动微信（只挂钩已在运行的）')
    s.add_argument('--timeout', type=int, help='自动捕获最多等多少秒（默认 180）')
    s.add_argument('--image', action='store_true', help='图片密钥')
    s.add_argument('--set', help='直接给出 64 位十六进制数据库密钥（会先验证）')
    s = add('config', cmd_config, '看/改配置：wx config [项] [值]', maxc=False); s.add_argument('k', nargs='?'); s.add_argument('v', nargs='?')
    s = add('voice', cmd_voice, '语音转写进度；--run 秒数 立即转一批', maxc=False); s.add_argument('--run', type=float)
    return p


def main(argv=None):
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding='utf-8', errors='replace')
        except AttributeError:
            pass
    p = build()
    a = p.parse_args(argv)
    if not getattr(a, 'fn', None):
        p.print_help(); return
    if a.account:
        os.environ['WXKIT_ACCOUNT'] = a.account
    db = None if a.cmd in NO_DB else DB(a.account)
    try:
        a.fn(db, a)
    except BrokenPipeError:
        pass
