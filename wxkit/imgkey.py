"""V2 图片密钥：读取本机微信主进程的 seed，按账号派生并用图片验证。

派生方式与现有版本相同：md5(f'{seed}{wxid}').hexdigest()[:16]。
只读取进程内存和账号图片，不启动、重启微信，不把候选值写入日志。
"""
import ctypes
import ctypes.wintypes as W
import glob
import hashlib
import os
import re
import struct
import sys
import time

from Crypto.Cipher import AES

RE_SEED = re.compile(rb'(?<![0-9])([0-9]{8,12})(?![0-9])')
SEED_MIN, SEED_MAX = 100_000_000, 4_000_000_000
CHUNK = 8 * 1024 * 1024


def _memory_api():
    # 延迟加载 Windows API，让其它平台仍能使用已有密钥和图片解密。
    from . import memscan
    api = memscan.kernel32
    api.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    api.OpenProcess.restype = W.HANDLE
    api.CloseHandle.argtypes = [W.HANDLE]
    api.CloseHandle.restype = W.BOOL
    api.VirtualQueryEx.argtypes = [W.HANDLE, ctypes.c_void_p,
                                  ctypes.POINTER(memscan.MEMORY_BASIC_INFORMATION), ctypes.c_size_t]
    api.VirtualQueryEx.restype = ctypes.c_size_t
    api.ReadProcessMemory.argtypes = [W.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    api.ReadProcessMemory.restype = W.BOOL
    return memscan


def _wxids(acct):
    base = os.path.basename(acct.path.rstrip('\\/'))
    out = [base]
    if '_' in base:
        out.append(base.rsplit('_', 1)[0])
    return list(dict.fromkeys(out))


def _samples(acct, limit=3):
    """取同一账号的完整 V2 AES 段，原图优先，缩略图兜底。"""
    orig, thumb = [], []
    pattern = os.path.join(acct.path, 'msg', 'attach', '*', '*', 'Img', '*.dat')
    for path in glob.iglob(pattern):
        try:
            with open(path, 'rb') as stream:
                data = stream.read(4096)
        except OSError:
            continue
        if data[:6] != b'\x07\x08V2\x08\x07' or len(data) < 15:
            continue
        length = struct.unpack('<I', data[6:10])[0]
        if not 32 <= length <= 2048:
            continue
        padded = length + 16 - length % 16
        if len(data) < 15 + padded:
            continue
        sample = (data[15:15 + padded], length, path)
        if path.endswith('_t.dat'):
            if len(thumb) < limit:
                thumb.append(sample)
        else:
            orig.append(sample)
        if len(orig) >= limit:
            break
    return (orig + thumb)[:limit]


def _head_ok(head):
    return (head.startswith(b'\xff\xd8\xff') or head.startswith(b'\x89PNG\r\n\x1a\n')
            or head.startswith((b'GIF87a', b'GIF89a', b'wxgf'))
            or (head[:4] == b'RIFF' and head[8:12] == b'WEBP'))


def _verify(key, samples):
    if not samples:
        return False
    for block, length, _ in samples:
        if len(block) < 16 or len(block) % 16 or len(block) < length:
            return False
        try:
            head = AES.new(key, AES.MODE_ECB).decrypt(block[:32])
        except ValueError:
            return False
        if not _head_ok(head):
            return False
    return True


def _derive(seed, wxid):
    return hashlib.md5(f'{seed}{wxid}'.encode()).hexdigest()[:16].encode()


def scan_seeds(pid, on_status=None, timeout=60):
    """有时限地扫描可读写内存，始终关闭进程句柄。"""
    memory = _memory_api()
    handle = memory.kernel32.OpenProcess(
        memory.PROCESS_VM_READ | memory.PROCESS_QUERY_INFORMATION, False, pid)
    if not handle:
        if on_status:
            on_status('无法读取微信进程，请确认微信仍在运行且当前用户有读取权限')
        return set()
    deadline = time.monotonic() + timeout
    seeds, total = set(), 0
    timed_out = False
    try:
        for base, size in memory._rw_regions(handle):
            off, tail = 0, b''
            while off < size:
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                count = min(CHUNK, size - off)
                chunk = memory._read_mem(handle, base + off, count)
                if chunk:
                    data = tail + chunk
                    total += len(chunk)
                    for match in RE_SEED.finditer(data):
                        # 分块末尾的数字可能尚未结束，留到下一块再判定。
                        if match.end() == len(data) and off + len(chunk) < size:
                            continue
                        value = int(match.group(0))
                        if SEED_MIN < value < SEED_MAX:
                            seeds.add(value)
                    tail = data[-14:] if len(chunk) == count else b''
                else:
                    tail = b''
                off += count
            if timed_out:
                break
    finally:
        memory.kernel32.CloseHandle(handle)
    if on_status:
        suffix = '（已到扫描时限）' if timed_out else ''
        on_status(f'扫描 {total / 1e6:.0f}MB，候选 {len(seeds)} 个{suffix}')
    return seeds


def solve(acct, xor_hint=None, on_status=None, timeout=60):
    """返回验证过的 (AES bytes, seed, xor)，失败返回 None，不保存候选。"""
    say = on_status or (lambda _message: None)
    if timeout <= 0:
        raise ValueError('扫描时限必须大于 0 秒')
    if sys.platform != 'win32':
        say('自动获取图片密钥目前仅支持 Windows；其他平台可导入已有密钥')
        return None
    deadline = time.monotonic() + timeout
    samples = _samples(acct)
    if not samples:
        say('这个账号本机还没有完整的 V2 图片可验证，请在电脑版微信打开一张图片后重试')
        return None
    memory = _memory_api()
    pid = memory.find_wechat_pid()
    if not pid:
        say('微信没在运行，请先打开并登录目标账号')
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        say('准备图片样本超过时限，请增加 --timeout 后重试')
        return None
    # 给图片验证保留时间，避免扫描耗尽总时限后丢弃已经读到的候选。
    seeds = sorted(scan_seeds(pid, on_status, timeout=remaining * 0.8))
    if not seeds:
        say('没有读到可验证的候选，请确认目标账号已登录后重试')
        return None
    try:
        xor_hint = int(xor_hint) if xor_hint is not None else None
    except (TypeError, ValueError):
        xor_hint = None
    fast = [seed for seed in seeds if seed & 0xFF == xor_hint] if xor_hint is not None else []
    fast_set = set(fast)
    groups = [fast, [seed for seed in seeds if seed not in fast_set]] if fast else [seeds]
    wxids = _wxids(acct)
    for group in groups:
        for wxid in wxids:
            for seed in group:
                if time.monotonic() >= deadline:
                    say('已到扫描时限，尚未验证出图片密钥；可增加 --timeout 后重试')
                    return None
                key = _derive(seed, wxid)
                if _verify(key, samples):
                    return key, seed, seed & 0xFF
    say('候选未通过图片验证，请确认微信登录账号与本地图片属于同一账号')
    return None
