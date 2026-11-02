"""朋友圈与收藏 → 档案库。

朋友圈：sns.db SnsTimeLine 每行一条动态（content 是 XML）。只增不删：别人删掉的动态档案库里还留着。
  点赞 <like_user_list>、评论 <comment_user_list> 也在这段 XML 里（只有电脑版刷到过的才有）。
朋友圈消息：SnsMessage_tmp3 = 别人给我点赞/评论的通知。
收藏：favorite.db fav_db_item，content 是 <favitem> XML。
"""
import json
import os
import re
import sqlite3

from . import parse, snsmedia

SCHEMA = '''
CREATE TABLE IF NOT EXISTS sns(tid INTEGER PRIMARY KEY, user TEXT, ts INT, text TEXT, media INT, loc TEXT,
  link TEXT, likes TEXT, comments TEXT, gone INT DEFAULT 0, media_json TEXT);
CREATE INDEX IF NOT EXISTS sns_user_ts ON sns(user, ts);
CREATE TABLE IF NOT EXISTS sns_notes(id INTEGER PRIMARY KEY, ts INT, type INT, feed INT, user TEXT, nick TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS favs(id INTEGER PRIMARY KEY, type INT, ts INT, src TEXT, text TEXT);
'''
_S = re.S


def _sns_row(tid, user, x):
    t = re.search(r'<createTime>(\d+)</createTime>', x)
    desc = parse.tag(x, 'contentDesc')
    co = re.search(r'<ContentObject>(.*?)</ContentObject>', x, _S)
    co = co.group(1) if co else ''
    media = len(re.findall(r'<media>', co))
    title = parse.tag(co, 'title'); url = parse.tag(co, 'contentUrl')
    link = (title + ' ' + url).strip() if (title or url) and not media else ''
    loc = re.search(r'<location[^>]*\bpoiName="([^"]*)"', x)
    def people(block):
        inner = re.search(rf'<{block}>(.*?)</{block}>', x, _S)
        return re.findall(r'<user_comment>(.*?)</user_comment>', inner.group(1), _S) if inner else []
    likes = '、'.join(parse.tag(m, 'nickname') for m in people('like_user_list'))
    comments = ' ／ '.join(f'{parse.tag(m, "nickname")}: {parse.tag(m, "content")}' for m in people('comment_user_list'))
    items = snsmedia.parse_media(co)
    media_json = json.dumps(items, ensure_ascii=False) if items else None
    return (tid, user, int(t.group(1)) if t else 0, desc, media, loc.group(1) if loc else '', link, likes, comments, media_json)


def _migrate(c):
    """老库没有 media_json 列就补上（无痛迁移）；已有则跳过。"""
    cols = [r[1] for r in c.execute('pragma table_info(sns)')]
    if cols and 'media_json' not in cols:
        c.execute('ALTER TABLE sns ADD COLUMN media_json TEXT')


def needs_refresh(c):
    """升级后 sns 表还没有 media_json 列 → 要全量刷新一次（迁移+回填），不必等 sns.db 变化。"""
    cols = [r[1] for r in c.execute('pragma table_info(sns)')]
    return bool(cols) and 'media_json' not in cols


def refresh_sns(c, mirror_dir):
    p = os.path.join(mirror_dir, 'sns.db')
    if not os.path.exists(p):
        return 0
    c.executescript(SCHEMA)
    _migrate(c)
    s = sqlite3.connect(f'file:{p}?mode=ro', uri=True)
    rows = [_sns_row(tid, u, x or '') for tid, u, x in s.execute('select tid, user_name, content from SnsTimeLine')]
    notes = list(s.execute('select local_id, create_time, type, feed_id, from_username, from_nickname, content from SnsMessage_tmp3'))
    s.close()
    with c:
        # 点赞评论会更新，所以用 upsert；原库里删掉的动态不删，标记 gone
        c.executemany('insert into sns(tid,user,ts,text,media,loc,link,likes,comments,gone,media_json) '
                      'values(?,?,?,?,?,?,?,?,?,0,?) '
                      'on conflict(tid) do update set likes=excluded.likes, comments=excluded.comments, '
                      'gone=0, media_json=excluded.media_json', rows)
        live = {r[0] for r in rows}
        if live:
            lo = min(r[2] for r in rows)
            for (tid,) in c.execute('select tid from sns where ts>=? and gone=0', (lo,)).fetchall():
                if tid not in live:
                    c.execute('update sns set gone=1 where tid=?', (tid,))
        c.executemany('insert or replace into sns_notes values(?,?,?,?,?,?,?)', notes)
    return len(rows)


def refresh_favs(c, mirror_dir):
    p = os.path.join(mirror_dir, 'favorite.db')
    if not os.path.exists(p):
        return 0
    c.executescript(SCHEMA)
    f = sqlite3.connect(f'file:{p}?mode=ro', uri=True)
    rows = []
    for lid, typ, ts, x, frm in f.execute('select local_id, type, update_time, content, fromusr from fav_db_item'):
        x = x or ''
        title = parse.tag(x, 'title') or parse.tag(x, 'desc')
        items = [parse.tag(d, 'datadesc') or parse.tag(d, 'datatitle')
                 for d in re.findall(r'<dataitem\b[^>]*>(.*?)</dataitem>', x, _S)]
        text = ' ／ '.join(t for t in [title] + items if t)
        rows.append((lid, typ, ts, frm or '', parse.short(text, 2000)))
    f.close()
    with c:
        c.executemany('insert or replace into favs values(?,?,?,?,?)', rows)
    return len(rows)
