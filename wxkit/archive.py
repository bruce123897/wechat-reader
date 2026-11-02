"""档案库 archive.db：整理好的明文消息。后台程序是唯一写入者，wx 命令只读它。

表：
  msgs     一行一条消息。(shard, chat, local_id) 唯一；撤回后原文保留，recalled_at 记撤回时间
  names    联系人/群/公众号：备注、昵称、微信号、类型、标签 id
  members  群成员与群昵称
  labels   标签 id → 名字
  sessions 会话列表（未读数、最后一条摘要）
  voices   语音转写结果（svrid → 文字），state: 0 待转 1 已转 2 无数据
  cursors  每张原始消息表处理到哪
  kv       杂项状态（new 命令的游标、心跳等）
"""
import os
import sqlite3

SCHEMA = '''
CREATE TABLE IF NOT EXISTS msgs(
  id INTEGER PRIMARY KEY, shard INT, chat TEXT, local_id INT, svrid INT, ts INT,
  sender TEXT, is_me INT, kind TEXT, text TEXT, extra TEXT, at TEXT, recalled_at INT, chash INT,
  UNIQUE(shard, chat, local_id));
CREATE INDEX IF NOT EXISTS msgs_chat_ts ON msgs(chat, ts);
CREATE INDEX IF NOT EXISTS msgs_ts ON msgs(ts);
CREATE INDEX IF NOT EXISTS msgs_kind_ts ON msgs(kind, ts);
CREATE INDEX IF NOT EXISTS msgs_svrid ON msgs(svrid);
CREATE TABLE IF NOT EXISTS names(user TEXT PRIMARY KEY, remark TEXT, nick TEXT, alias TEXT, kind TEXT, labels TEXT);
CREATE TABLE IF NOT EXISTS members(room TEXT, user TEXT, gnick TEXT, PRIMARY KEY(room, user));
CREATE TABLE IF NOT EXISTS labels(id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS sessions(chat TEXT PRIMARY KEY, unread INT, summary TEXT, last_ts INT,
  sort_ts INT, hidden INT, draft TEXT, last_sender TEXT);
CREATE TABLE IF NOT EXISTS voices(svrid INTEGER PRIMARY KEY, chat TEXT, ts INT, txt TEXT, state INT DEFAULT 0);
CREATE INDEX IF NOT EXISTS voices_state ON voices(state, ts);
CREATE TABLE IF NOT EXISTS cursors(shard INT, tbl TEXT, max_local INT, PRIMARY KEY(shard, tbl));
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
'''


def connect(acct, write=False):
    os.makedirs(acct.home, exist_ok=True)
    if not write and not os.path.exists(acct.archive):
        raise SystemExit('!! 档案库还没建好：运行 wx sync（第一次约几分钟），或确认后台程序在跑（wx status）')
    uri = f'file:{acct.archive}' + ('' if write else '?mode=ro')
    c = sqlite3.connect(uri, uri=True, timeout=30)
    c.execute('PRAGMA busy_timeout=30000')
    if write:
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA synchronous=NORMAL')
        c.executescript(SCHEMA)
    return c


def kv_get(c, k, default=None):
    r = c.execute('select v from kv where k=?', (k,)).fetchone()
    return r[0] if r else default


def kv_set(c, k, v):
    c.execute('insert or replace into kv values(?,?)', (k, str(v)))
