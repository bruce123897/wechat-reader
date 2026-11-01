"""时间：一律按北京时间显示和理解。

when() 能看懂的写法：
  30m / 2h / 3d / 1w          多久以前
  14:00                       今天 14:00（如果还没到，就是昨天）
  10-17 / 10-17 14:00         今年某天
  2026-10-17 [14:00]          完整日期
  今天 / 昨天 / 前天 / 本周 / 本月   中文也行（today / yesterday 同理）
"""
import datetime
import re
import time

TZ = datetime.timezone(datetime.timedelta(hours=8))


def now():
    return datetime.datetime.now(TZ)


def fmt(ts, f='%m-%d %H:%M'):
    return datetime.datetime.fromtimestamp(ts, TZ).strftime(f)


def _day(d):
    return int(datetime.datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp())


def when(s):
    """返回 unix 秒。看不懂就报错并说明格式。"""
    s = (s or '').strip()
    n = now()
    m = re.fullmatch(r'(\d+(?:\.\d+)?)\s*(m|min|h|d|w)', s, re.I)
    if m:
        v = float(m.group(1)); unit = m.group(2).lower()[0]
        return int(time.time() - v * {'m': 60, 'h': 3600, 'd': 86400, 'w': 604800}[unit])
    words = {'今天': 0, 'today': 0, '昨天': 1, 'yesterday': 1, '前天': 2}
    if s.lower() in words:
        return _day(n - datetime.timedelta(days=words[s.lower()]))
    if s in ('本周', 'week'):
        return _day(n - datetime.timedelta(days=n.weekday()))
    if s in ('本月', 'month'):
        return _day(n.replace(day=1))
    m = re.fullmatch(r'(\d{1,2}):(\d{2})', s)
    if m:
        t = n.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if t > n:
            t -= datetime.timedelta(days=1)
        return int(t.timestamp())
    m = re.fullmatch(r'(?:(\d{4})[-/.])?(\d{1,2})[-/.](\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?', s)
    if m:
        y = int(m.group(1) or n.year)
        t = datetime.datetime(y, int(m.group(2)), int(m.group(3)), int(m.group(4) or 0), int(m.group(5) or 0), tzinfo=TZ)
        if not m.group(1) and t > n + datetime.timedelta(days=1):
            t = t.replace(year=y - 1)   # 说 12-30 而现在是 1 月，指去年
        return int(t.timestamp())
    raise SystemExit(f'!! 看不懂时间 "{s}"。可以写：30m / 2h / 3d / 14:00 / 10-17 / "10-17 14:00" / 2026-10-17 / 今天 / 昨天')


def until(s):
    """区间结束：只写了日期的，算到那天 24 点。"""
    t = when(s)
    if re.fullmatch(r'(?:\d{4}[-/.])?\d{1,2}[-/.]\d{1,2}|今天|昨天|前天|today|yesterday', s.strip()):
        t += 86400
    return t
