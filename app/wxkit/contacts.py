"""联系人、群、群昵称、标签。从镜像里的 contact.db 读，结果写进档案库的 names / members / labels 表。

contact.local_type：1 = 好友，2 = 群，3 = 非好友（群成员、陌生人等）。
contact.extra_buffer 是 protobuf，字段 30 = 标签 id（逗号分隔）。
chat_room.ext_buffer 是 protobuf，重复字段 1 = 成员 {1: wxid, 2: 群昵称}。
"""
import sqlite3


def _varint(b, i):
    r = s = 0
    while True:
        x = b[i]; i += 1; r |= (x & 0x7f) << s; s += 7
        if x < 0x80:
            return r, i


def pb_fields(b):
    """最小 protobuf 解析：只拆一层，返回 [(字段号, 值)]，值是 int 或 bytes。"""
    out, i = [], 0
    try:
        while i < len(b):
            k, i = _varint(b, i); f, t = k >> 3, k & 7
            if t == 0:
                v, i = _varint(b, i)
            elif t == 2:
                n, i = _varint(b, i); v = b[i:i + n]; i += n
            elif t == 5:
                v = b[i:i + 4]; i += 4
            elif t == 1:
                v = b[i:i + 8]; i += 8
            else:
                break
            out.append((f, v))
    except IndexError:
        pass
    return out


def _s(v):
    try:
        return v.decode('utf-8') if isinstance(v, bytes) else ''
    except UnicodeDecodeError:
        return ''


def load(contact_db):
    """返回 (names, members, labels)
    names:   [(username, remark, nick, alias, kind, label_ids)]  kind: friend/group/official/other
    members: [(room, username, group_nick)]
    labels:  [(id, name)]
    """
    c = sqlite3.connect(f'file:{contact_db}?mode=ro', uri=True)
    names = []
    for u, rk, nk, al, lt, eb in c.execute('select username,remark,nick_name,alias,local_type,extra_buffer from contact'):
        kind = ('group' if u.endswith('@chatroom') else 'official' if u.startswith('gh_')
                else 'friend' if lt == 1 else 'other')
        lab = next((_s(v) for f, v in pb_fields(eb or b'') if f == 30), '')
        names.append((u, rk or '', nk or '', al or '', kind, lab))
    known = {n[0] for n in names}
    try:
        for u, rk, nk, al in c.execute('select username,remark,nick_name,alias from stranger'):
            if u not in known:
                names.append((u, rk or '', nk or '', al or '', 'other', ''))
    except sqlite3.Error:
        pass
    members = []
    for room, eb in c.execute('select username, ext_buffer from chat_room'):
        for f, v in pb_fields(eb or b''):
            if f != 1 or not isinstance(v, bytes):
                continue
            m = dict((ff, vv) for ff, vv in pb_fields(v))
            u = _s(m.get(1, b''))
            if u:
                members.append((room, u, _s(m.get(2, b''))))
    try:
        labels = list(c.execute('select label_id_, label_name_ from contact_label'))
    except sqlite3.Error:
        labels = []
    c.close()
    return names, members, labels
