"""图片 / 视频 / 文件：从消息找到本地文件，图片解密成普通 jpg/png。

本地文件位置（<root> = 账号目录/msg）：
  图片  attach/<md5(会话)>/<年-月>/Img/<文件名>.dat   _h 高清  _t 缩略图（电脑没点开原图时只有它）
  视频  video/<年-月>/<文件名>.mp4（没下载时只有 _thumb.jpg 封面）
  文件  file/<年-月>/<原文件名>（重名时微信会加 (1)(2)）
<文件名> 由入库时从 packed_info_data 里取出，存在 msgs.extra.file。

图片 .dat 格式：
  老格式：整个文件异或一个字节
  V1/V2：文件头 07 08 56 3x 08 07 + AES长度 + 异或长度；前段 AES-ECB，中段明文，尾段异或
  V1 用固定 AES 密钥，V2 的 AES 密钥每个账号不同（wx key 获取）
  解出来如果是 wxgf（微信自家 HEVC 封装），用 ffmpeg 转成 jpg
"""
import collections
import glob
import hashlib
import json
import os
import shutil
import struct
import subprocess

from . import config, keys, timeutil

V1_KEY = b'cfcd208495d565ef'


def _root(acct):
    return os.path.join(acct.path, 'msg')


def files_for(acct, row):
    """row: dict(chat, ts, kind, extra)。返回候选本地文件列表（最好的在前）。"""
    ex = json.loads(row['extra']) if isinstance(row.get('extra'), str) else (row.get('extra') or {})
    root = _root(acct)
    if row['kind'] == 'image' and ex.get('file'):
        base = os.path.join(root, 'attach', hashlib.md5(row['chat'].encode()).hexdigest(), '*', 'Img', ex['file'])
        return [p for suf in ('.dat', '_h.dat', '_t.dat') for p in glob.glob(base + suf)]
    if row['kind'] == 'video' and ex.get('file'):
        base = os.path.join(root, 'video', '*', ex['file'])
        return glob.glob(base + '.mp4') + glob.glob(base + '_thumb.jpg')
    if row['kind'] == 'file' and ex.get('name'):
        month = timeutil.fmt(row['ts'], '%Y-%m')
        stem, ext = os.path.splitext(ex['name'])
        cands = glob.glob(os.path.join(root, 'file', month, glob.escape(ex['name']))) + \
            glob.glob(os.path.join(root, 'file', month, glob.escape(stem) + '(*)' + glob.escape(ext))) + \
            glob.glob(os.path.join(root, 'file', '*', glob.escape(ex['name'])))
        size = str(ex.get('size') or '')
        seen, out = set(), []
        for p in cands:
            if p not in seen:
                seen.add(p); out.append(p)
        # 同名文件按大小挑
        out.sort(key=lambda p: (str(os.path.getsize(p)) != size,))
        return out
    return []


# ---------- 图片解密 ----------
def _is_v(b):
    return b[:4] in (b'\x07\x08V1', b'\x07\x08V2') and b[4:6] == b'\x08\x07'


def guess_xor(acct, n=40):
    """异或值可以从图片本身推出来：JPG 结尾固定是 FF D9。取多张图片投票。"""
    votes = collections.Counter()
    for p in glob.glob(os.path.join(_root(acct), 'attach', '*', '*', 'Img', '*_t.dat'))[:n * 5]:
        b = open(p, 'rb').read()
        if _is_v(b) and len(b) > 20:
            votes[b[-1] ^ 0xD9] += 1
        if sum(votes.values()) >= n:
            break
    return votes.most_common(1)[0][0] if votes else None


def decrypt_dat(acct, data):
    from Crypto.Cipher import AES
    if not _is_v(data):
        k = data[0] ^ 0xFF   # 老格式：JPG 开头固定 FF
        return bytes(x ^ k for x in data)
    aes, xor = keys.image_keys(acct)
    if data[3:4] == b'1':
        aes = V1_KEY
    if xor is None:
        xor = guess_xor(acct)
        if xor is not None:
            keys.save(acct, img_xor=str(xor))
    if aes is None:
        raise RuntimeError('没有图片密钥（V2 格式需要）：运行 wx key --image')
    al, xl = struct.unpack('<II', data[6:14])
    d = data[15:]
    alp = al + (16 - al % 16) if al % 16 else al + 16
    head = AES.new(aes, AES.MODE_ECB).decrypt(d[:alp])[:al]
    tail = bytes(x ^ xor for x in d[len(d) - xl:]) if xl else b''
    return head + d[alp:len(d) - xl] + tail


def _ext(b):
    return ('jpg' if b[:2] == b'\xff\xd8' else 'png' if b[:4] == b'\x89PNG' else 'gif' if b[:3] == b'GIF'
            else 'webp' if b[8:12] == b'WEBP' else 'wxgf' if b[:4] == b'wxgf' else 'bin')


def ffmpeg():
    return config.load().get('ffmpeg') or shutil.which('ffmpeg')


def save_image(acct, src, out_base):
    """解密一张 .dat 到 out_base.<扩展名>，返回路径。"""
    raw = decrypt_dat(acct, open(src, 'rb').read())
    ext = _ext(raw)
    if ext == 'wxgf':
        ff = ffmpeg()
        if not ff:
            raise RuntimeError('这张图是微信 HEVC 格式，需要 ffmpeg 才能转 jpg')
        h = out_base + '.hevc'
        i = raw.find(b'\x00\x00\x00\x01')
        open(h, 'wb').write(raw[i if i >= 0 else 0:])
        subprocess.run([ff, '-loglevel', 'quiet', '-y', '-f', 'hevc', '-i', h, '-frames:v', '1', out_base + '.jpg'],
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        os.remove(h)
        return out_base + '.jpg'
    open(out_base + '.' + ext, 'wb').write(raw)
    return out_base + '.' + ext


def export(acct, row, out_dir):
    """把一条图片/视频/文件消息对应的本地文件放到 out_dir。返回 (路径, 说明)。"""
    fs = files_for(acct, row)
    if not fs:
        return None, '本地没有（电脑版没下载过）'
    os.makedirs(out_dir, exist_ok=True)
    name = f"{timeutil.fmt(row['ts'], '%m%d-%H%M%S')}_{row.get('local_id', '')}"
    src = fs[0]
    note = '缩略图' if src.endswith(('_t.dat', '_thumb.jpg')) else ''
    if row['kind'] == 'image':
        return save_image(acct, src, os.path.join(out_dir, name)), note
    dst = os.path.join(out_dir, name + '_' + os.path.basename(src))
    shutil.copy2(src, dst)
    return dst, note
