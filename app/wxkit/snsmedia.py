"""朋友圈图片/视频：解析 media、下载、ISAAC64 解密、本地缓存。

朋友圈每条动态的原始 XML 在镜像库 sns.db 的 SnsTimeLine.content；入库时 social.py 把每个
<media> 解析成结构化 dict 存进 archive 的 sns.media_json。拿图时从 media_json（没有就现查
mirror）取 url+key+token，直连 qpic.cn 下载，再用 ISAAC64 解密还原成普通 jpg/mp4。

密钥全自动：图片/视频的 key、token 都在这段 XML 里，不需要人工找 key。
  图片（<media><type>2</type>）：<url key= token=> 指向原图，key 是十进制 uint64，整文件 ISAAC64 异或。
  视频（<media><type>6</type>）：<url> 指向视频 CDN，<enc key=> 是解密 seed，只加密前 131072 字节。
  LivePhoto（图片里嵌 <LivePhoto><liveMedia type=6>）：静态图是主 url，附带一段小视频。
没有 key 的 url = 未加密，原样保存。链接会过期（CDN 404），这也是要做后台预取的原因。
"""
import html
import json
import os
import re
import sqlite3
import time
import urllib.request

MASK = (1 << 64) - 1
VIDEO_LIMIT = 131072          # 视频惯例只加密前 128KB
VIDEO_HOST_HINT = 'video.qq.com'


# ---------- ISAAC64（朋友圈媒体解密用的伪随机流） ----------
class _Isaac64:
    def __init__(self, seed):
        self.rsl = [0] * 256
        self.rsl[0] = seed & MASK
        self.mem = [0] * 256
        self.a = self.b = self.c = 0
        self._init()
        self.cnt = 256

    def _mix(self, x):
        a, b, c, d, e, f, g, h = x
        a = (a - e) & MASK; f ^= (h >> 9); h = (h + a) & MASK
        b = (b - f) & MASK; g ^= (a << 9) & MASK; a = (a + b) & MASK
        c = (c - g) & MASK; h ^= (b >> 23); b = (b + c) & MASK
        d = (d - h) & MASK; a ^= (c << 15) & MASK; c = (c + d) & MASK
        e = (e - a) & MASK; b ^= (d >> 14); d = (d + e) & MASK
        f = (f - b) & MASK; c ^= (e << 20) & MASK; e = (e + f) & MASK
        g = (g - c) & MASK; d ^= (f >> 17); f = (f + g) & MASK
        h = (h - d) & MASK; e ^= (g << 14) & MASK; g = (g + h) & MASK
        return [a, b, c, d, e, f, g, h]

    def _init(self):
        x = [0x9e3779b97f4a7c13] * 8
        for _ in range(4):
            x = self._mix(x)
        for i in range(0, 256, 8):
            x = [(x[j] + self.rsl[i + j]) & MASK for j in range(8)]; x = self._mix(x); self.mem[i:i + 8] = x
        for i in range(0, 256, 8):
            x = [(x[j] + self.mem[i + j]) & MASK for j in range(8)]; x = self._mix(x); self.mem[i:i + 8] = x
        self._isaac()

    def _isaac(self):
        self.c = (self.c + 1) & MASK; self.b = (self.b + self.c) & MASK
        m = self.mem
        for i in range(256):
            x = m[i]; r = i % 4
            if r == 0:
                self.a = (~(self.a ^ ((self.a << 21) & MASK))) & MASK
            elif r == 1:
                self.a ^= self.a >> 5
            elif r == 2:
                self.a ^= (self.a << 12) & MASK
            else:
                self.a ^= self.a >> 33
            self.a = (m[(i + 128) % 256] + self.a) & MASK
            y = (m[(x >> 3) & 255] + self.a + self.b) & MASK; m[i] = y
            self.b = (m[(y >> 11) & 255] + x) & MASK; self.rsl[i] = self.b

    def next(self):
        if self.cnt == 0:
            self._isaac(); self.cnt = 256
        self.cnt -= 1
        return self.rsl[self.cnt]


def keystream(seed, n):
    """大端 64 位块拼成的 n 字节密钥流。"""
    g = _Isaac64(seed)
    out = bytearray()
    while len(out) < n:
        out += g.next().to_bytes(8, 'big')
    return bytes(out[:n])


def decrypt(data, seed, limit=None):
    """ISAAC64 异或解密。limit=None 整文件；视频传 131072 只解前段。"""
    n = len(data) if limit is None else min(limit, len(data))
    if n <= 0:
        return data
    ks = keystream(seed, n)
    head = (int.from_bytes(data[:n], 'big') ^ int.from_bytes(ks, 'big')).to_bytes(n, 'big')
    return head + data[n:]


# ---------- XML 解析 ----------
def _one(tagname, block):
    """取 <tagname ...>text</tagname>，返回 (属性 dict, text)；没有返回 (None, None)。"""
    m = re.search(rf'<{tagname}\b([^>]*)>([^<]*)</{tagname}>', block)
    if not m:
        return None, None
    attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
    return attrs, html.unescape(m.group(2))


def _int_or_none(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def parse_media(content_xml):
    """一条动态的 content XML → [media dict]。属性顺序无关，按属性名取，不用位置正则。

    每个 dict：i, type(2图片/6视频), url, key, token, md5, thumb_url, thumb_key, thumb_token,
    enc_key(视频/LivePhoto 的解密 seed), live_url, live_enc_key（LivePhoto 附带小视频）。
    """
    out = []
    for i, block in enumerate(re.findall(r'<media\b.*?</media>', content_xml or '', re.S)):
        live = re.search(r'<LivePhoto>.*?</LivePhoto>', block, re.S)
        live_xml = live.group(0) if live else ''
        main = block.replace(live_xml, '') if live_xml else block

        mtype = _int_or_none((_one('type', main)[1] or '').strip())
        ua, utext = _one('url', main)
        ta, ttext = _one('thumb', main)
        enc_attrs, _ = _one('enc', main)   # <enc key="...">1</enc>
        enc_key = enc_attrs.get('key') if enc_attrs else None

        item = {
            'i': i,
            'type': mtype,
            'url': utext,
            'key': (ua or {}).get('key'),
            'token': (ua or {}).get('token'),
            'md5': (ua or {}).get('md5'),
            'thumb_url': ttext,
            'thumb_key': (ta or {}).get('key'),
            'thumb_token': (ta or {}).get('token'),
            'enc_key': enc_key,
            'live_url': None,
            'live_enc_key': None,
        }
        if live_xml:
            lm = re.search(r'<liveMedia>.*?</liveMedia>', live_xml, re.S)
            lb = lm.group(0) if lm else live_xml
            la, ltext = _one('url', lb)
            le, _ = _one('enc', lb)
            item['live_url'] = ltext
            item['live_enc_key'] = le.get('key') if le else None
        out.append(item)
    return out


# ---------- 下载 ----------
# 朋友圈视频 CDN（video.qq.com）会挡掉非微信客户端（返回 400，X-retmsg: sns illegal ua），
# 必须用微信自己的 User-Agent 才放行。是否开启由开关控制（默认关，交 Jayden 决定）。
_WX_UA = 'MicroMessenger Client'


class TooBig(Exception):
    """媒体超过大小上限。"""


def _download(url, use_wx_ua=False, retries=3, timeout=30, max_bytes=None):
    """直连优先（qpic.cn 是国内 CDN），失败再走环境代理。返回 bytes 或抛异常。

    max_bytes：边下边数，超过就抛 TooBig（避免把超大视频整个拉下来撑满硬盘）。
    """
    headers = {'User-Agent': _WX_UA} if use_wx_ua else {}
    openers = [urllib.request.build_opener(urllib.request.ProxyHandler({})),   # 先直连
               urllib.request.build_opener()]                                   # 再走环境代理
    last = None
    for attempt in range(retries):
        for op in openers:
            try:
                req = urllib.request.Request(url, headers=headers)
                resp = op.open(req, timeout=timeout)
                clen = resp.headers.get('Content-Length')
                if max_bytes and clen and clen.isdigit() and int(clen) > max_bytes:
                    raise TooBig(int(clen))
                if not max_bytes:
                    return resp.read()
                buf = bytearray()
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    buf += chunk
                    if len(buf) > max_bytes:
                        raise TooBig(len(buf))
                return bytes(buf)
            except TooBig:
                raise
            except urllib.error.HTTPError as e:
                last = e
                if e.code in (403, 404):   # 过期/没权限，换代理也没用，直接抛
                    raise
            except Exception as e:
                last = e
        time.sleep(0.5 * (attempt + 1))
    raise last if last else RuntimeError('下载失败')


def _dl_url(url, token):
    url = html.unescape(url or '')
    if not url:
        return ''
    if '?' in url:          # 视频 url 自带 encfilekey/token/idx
        return url
    if token:
        return f'{url}?token={token}&idx=1'
    return url


def _ext(data):
    if data[:3] == b'\xff\xd8\xff':
        return 'jpg'
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'webp'
    if data[4:8] == b'ftyp':
        return 'mp4'
    return 'bin'


def cache_dir(acct, tid):
    return os.path.join(acct.home, 'sns_media', str(tid))


def _decrypt_payload(data, key, limit=None):
    seed = _int_or_none(key)
    if seed is None:
        return data              # 没 key = 未加密，原样
    return decrypt(data, seed, limit)


def fetch_item(acct, tid, item, kind='image', thumb=False, force=False, use_wx_ua=False,
               max_mb=None, log=None):
    """下载+解密一个 media。返回 (本地路径 或 None, 说明)。不抛异常。

    max_mb：单文件上限（MB）。超过的视频只存缩略图并记日志。
    log：可选日志回调（daemon 用）。
    """
    d = cache_dir(acct, tid)
    max_bytes = int(max_mb * 1024 * 1024) if max_mb else None
    is_video = item.get('type') == 6
    if kind == 'video' and not is_video and not item.get('live_url'):
        return None, '[不是视频]'
    if kind == 'image' and is_video:
        return None, '[这是视频，用 --kind video]'

    if thumb:
        # 视频的 url/thumb key 都是占位符 "0"，真正的解密 seed 在 <enc key>；图片缩略图用自己的 thumb key。
        tkey = item.get('enc_key') if is_video else item.get('thumb_key')
        url, key, token, limit, idx = item.get('thumb_url'), tkey, item.get('thumb_token'), None, f"{item['i']}_thumb"
    elif is_video:
        url, key, token, limit, idx = item.get('url'), item.get('enc_key'), None, VIDEO_LIMIT, f"{item['i']}_video"
    elif kind == 'video' and item.get('live_url'):   # 图片附带的 LivePhoto 小视频
        url, key, token, limit, idx = item.get('live_url'), item.get('live_enc_key'), None, VIDEO_LIMIT, f"{item['i']}_live"
    else:
        url, key, token, limit, idx = item.get('url'), item.get('key'), item.get('token'), None, str(item['i'])

    if not url:
        return None, '[没有地址]'

    # 已缓存就直接给路径
    if not force and os.path.isdir(d):
        for f in os.listdir(d):
            if f.rsplit('.', 1)[0] == idx:
                return os.path.join(d, f), ''

    dl = _dl_url(url, token)
    # 是否用微信 UA 由开关决定（默认开，视频 CDN 需要）。关着时视频会 400，走下面的提示分支。
    cap = max_bytes if (is_video and not thumb) else None   # 只给视频设上限
    try:
        raw = _download(dl, use_wx_ua=use_wx_ua, max_bytes=cap)
    except TooBig as e:
        if log:
            log(f'朋友圈视频超过上限（{int(e.args[0]) >> 20}MB>{max_mb}MB），只存缩略图 tid={tid} i={item["i"]}')
        tp, tn = fetch_item(acct, tid, item, kind='video', thumb=True, force=force, use_wx_ua=use_wx_ua)
        return tp, (f'[视频>{max_mb}MB，只存缩略图]' if tp else '[视频超上限且缩略图取不到]')
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, '[已过期 404]'
        if e.code == 400 and VIDEO_HOST_HINT in dl and not use_wx_ua:
            return None, '[视频需开启微信 UA 开关 wx config sns_video_ua true]'
        return None, f'[下载失败 HTTP {e.code}]'
    except Exception as e:
        return None, f'[下载失败 {type(e).__name__}]'

    out = _decrypt_payload(raw, key, limit)
    ext = _ext(out)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f'{idx}.{ext}')
    with open(path, 'wb') as f:
        f.write(out)
    return path, ('' if ext != 'bin' else '[解密结果未知格式]')


def media_for_tid(c_archive, mirror_dir, tid):
    """取一条动态的 media 列表：优先用 archive.sns.media_json，没有就现查 mirror。"""
    try:
        row = c_archive.execute('select media_json from sns where tid=?', (tid,)).fetchone()
    except sqlite3.Error:
        row = None
    if row and row[0]:
        try:
            return json.loads(row[0])
        except (ValueError, TypeError):
            pass
    return media_from_mirror(mirror_dir, tid)


def media_from_mirror(mirror_dir, tid):
    p = os.path.join(mirror_dir, 'sns.db')
    if not os.path.exists(p):
        return []
    s = sqlite3.connect(f'file:{p}?mode=ro', uri=True)
    try:
        r = s.execute('select content from SnsTimeLine where tid=?', (tid,)).fetchone()
    finally:
        s.close()
    return parse_media(r[0] if r else '')
