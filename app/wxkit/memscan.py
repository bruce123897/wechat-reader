"""纯 Python 内存扫描：从运行中的微信进程内存里找数据库密钥。

策略（快到慢，找到即停）：
  1. 模块扫描：只读微信主 DLL 的可写节区（几十 MB），逐 8 字节对齐找 32 字节候选
  2. 盐邻近扫描：在全部 RW 区域里找数据库盐（16 字节），在盐附近 ±512 字节找候选
  3. 不做全内存暴力扫描（2 GB+ 太慢）

每个候选用 HMAC 验证（PBKDF2 ~0.2s），限制最多验证 MAX_CHECKS 次。
"""
import ctypes
import ctypes.wintypes as W
import os
import struct
import subprocess
import sys
import time

from . import crypto

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
MEM_IMAGE = 0x1000000
PAGE_READWRITE = 0x04
PAGE_WRITECOPY = 0x08
PAGE_EXECUTE_READWRITE = 0x40
PAGE_EXECUTE_WRITECOPY = 0x80

RW_PROTECT = {PAGE_READWRITE, PAGE_WRITECOPY, PAGE_EXECUTE_READWRITE, PAGE_EXECUTE_WRITECOPY}
MAX_CHECKS = 200

kernel32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('BaseAddress', ctypes.c_uint64),
        ('AllocationBase', ctypes.c_uint64),
        ('AllocationProtect', W.DWORD),
        ('_pad1', W.DWORD),
        ('RegionSize', ctypes.c_uint64),
        ('State', W.DWORD),
        ('Protect', W.DWORD),
        ('Type', W.DWORD),
        ('_pad2', W.DWORD),
    ]


class MODULEINFO(ctypes.Structure):
    _fields_ = [
        ('lpBaseOfDll', ctypes.c_void_p),
        ('SizeOfImage', W.DWORD),
        ('EntryPoint', ctypes.c_void_p),
    ]


# 设置 64 位兼容的函数签名
psapi.EnumProcessModulesEx.argtypes = [W.HANDLE, ctypes.POINTER(ctypes.c_void_p), W.DWORD, ctypes.POINTER(W.DWORD), W.DWORD]
psapi.EnumProcessModulesEx.restype = W.BOOL
psapi.GetModuleFileNameExW.argtypes = [W.HANDLE, ctypes.c_void_p, ctypes.c_wchar_p, W.DWORD]
psapi.GetModuleFileNameExW.restype = W.DWORD
psapi.GetModuleInformation.argtypes = [W.HANDLE, ctypes.c_void_p, ctypes.POINTER(MODULEINFO), W.DWORD]
psapi.GetModuleInformation.restype = W.BOOL


def _rw_regions(handle, type_filter=None):
    """枚举进程的可读写内存区域。type_filter 可以是 MEM_PRIVATE / MEM_IMAGE 等。"""
    mbi = MEMORY_BASIC_INFORMATION()
    addr = 0
    limit = 0x7FFFFFFFFFFF
    while addr < limit:
        ret = kernel32.VirtualQueryEx(handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
        if not ret:
            break
        base = mbi.BaseAddress
        size = mbi.RegionSize
        if (mbi.State == MEM_COMMIT
                and mbi.Protect in RW_PROTECT
                and 4096 <= size <= 50 * 1024 * 1024):
            if type_filter is None or mbi.Type == type_filter:
                yield base, size
        next_addr = base + size
        if next_addr <= addr:
            break
        addr = next_addr


def _read_mem(handle, addr, size):
    buf = ctypes.create_string_buffer(size)
    nread = ctypes.c_size_t(0)
    if kernel32.ReadProcessMemory(handle, ctypes.c_void_p(addr), buf, size, ctypes.byref(nread)):
        return buf.raw[:nread.value]
    return None


def _get_wechat_modules(handle):
    """找到微信进程里的主 DLL 模块（WeChatWin.dll / xwechat.dll / Weixin.exe 等）。
    返回 [(name, base, size), ...]。"""
    modules = (ctypes.c_void_p * 1024)()
    needed = W.DWORD()
    if not psapi.EnumProcessModulesEx(handle, modules, ctypes.sizeof(modules), ctypes.byref(needed), 0x03):
        return []
    count = needed.value // ctypes.sizeof(ctypes.c_void_p)
    result = []
    name_buf = ctypes.create_unicode_buffer(512)
    info = MODULEINFO()
    targets = ('wechatwin', 'xwechat', 'weixin')
    for i in range(count):
        if not modules[i]:
            continue
        if psapi.GetModuleFileNameExW(handle, modules[i], name_buf, 512):
            fname = os.path.basename(name_buf.value).lower()
            base_name = os.path.splitext(fname)[0].lower()
            if any(t in base_name for t in targets):
                psapi.GetModuleInformation(handle, modules[i], ctypes.byref(info), ctypes.sizeof(info))
                base = modules[i] if isinstance(modules[i], int) else (modules[i] or 0)
                result.append((fname, base, info.SizeOfImage))
    return result


def _is_good_candidate(cand):
    """快速排除明显不是密钥的候选（在做昂贵的 PBKDF2 之前）。"""
    if len(set(cand)) <= 10:
        return False
    if all(b == 0 for b in cand):
        return False
    if cand == b'\xff' * 32:
        return False
    return True


def scan_db_key(pid, db_path, on_status=None, timeout=120):
    """扫描进程内存找能解开 db_path 的密钥。返回 hex 字符串或 None。"""
    with open(db_path, 'rb') as f:
        page1 = f.read(crypto.PAGE)
    if len(page1) < crypto.PAGE:
        return None
    salt = page1[:16]

    handle = kernel32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not handle:
        if on_status:
            on_status(f'无法打开进程 {pid}（需要管理员权限？）')
        return None

    checked = 0
    deadline = time.time() + timeout

    try:
        # === Pass 1: 模块扫描 ===
        mods = _get_wechat_modules(handle)
        if mods and on_status:
            on_status(f'找到 {len(mods)} 个微信模块: {", ".join(n for n,_,_ in mods)}')

        for mod_name, mod_base, mod_size in mods:
            if time.time() > deadline:
                break
            if on_status:
                on_status(f'扫描模块 {mod_name} ({mod_size/1024/1024:.1f} MB)...')
            # 枚举模块内的 RW 区域
            mbi = MEMORY_BASIC_INFORMATION()
            addr = mod_base
            end = mod_base + mod_size
            while addr < end and time.time() < deadline:
                ret = kernel32.VirtualQueryEx(handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
                if not ret:
                    break
                base = mbi.BaseAddress
                size = mbi.RegionSize
                if (mbi.State == MEM_COMMIT and mbi.Protect in RW_PROTECT and size > 0):
                    data = _read_mem(handle, base, min(size, 50 * 1024 * 1024))
                    if data:
                        for pos in range(0, len(data) - 31, 8):
                            cand = data[pos:pos + 32]
                            if _is_good_candidate(cand):
                                checked += 1
                                if crypto.check_key(cand, db_path):
                                    if on_status:
                                        on_status(f'在模块 {mod_name} 中找到密钥！(验证了 {checked} 个候选)')
                                    return cand.hex()
                                if checked >= MAX_CHECKS // 2:
                                    break
                        if checked >= MAX_CHECKS // 2:
                            break
                next_addr = base + size
                if next_addr <= addr:
                    break
                addr = next_addr

        if checked > 0 and on_status:
            on_status(f'模块扫描: 验证了 {checked} 个候选，未命中')

        # === Pass 2: 盐邻近扫描 ===
        if time.time() < deadline:
            regions = list(_rw_regions(handle, MEM_PRIVATE))
            total_mb = sum(s for _, s in regions) / 1024 / 1024
            if on_status:
                on_status(f'盐邻近扫描: {len(regions)} 个堆区域 ({total_mb:.0f} MB)...')

            salt_hits = 0
            for i, (base, size) in enumerate(regions):
                if time.time() > deadline or checked >= MAX_CHECKS:
                    break
                data = _read_mem(handle, base, size)
                if not data:
                    continue
                idx = data.find(salt)
                while idx >= 0 and checked < MAX_CHECKS and time.time() < deadline:
                    salt_hits += 1
                    for offset in range(-512, 512, 8):
                        pos = idx + offset
                        if 0 <= pos <= len(data) - 32:
                            cand = data[pos:pos + 32]
                            if _is_good_candidate(cand):
                                checked += 1
                                if crypto.check_key(cand, db_path):
                                    if on_status:
                                        on_status(f'在盐附近找到密钥！(验证了 {checked} 个候选)')
                                    return cand.hex()
                                if checked >= MAX_CHECKS:
                                    break
                    idx = data.find(salt, idx + 1)

            if on_status:
                on_status(f'盐邻近扫描完成: 发现 {salt_hits} 处盐，验证了 {checked} 个候选')

        if on_status:
            on_status(f'扫描完成，共验证 {checked} 个候选，未找到密钥')
        return None
    finally:
        kernel32.CloseHandle(handle)


def find_wechat_pid():
    """查找微信主进程 PID。"""
    for name in ('Weixin', 'WeChat'):
        try:
            r = subprocess.run(
                ['powershell', '-NoProfile', '-Command',
                 f'Get-Process {name} -ErrorAction SilentlyContinue '
                 f'| Where-Object {{ $_.MainWindowHandle -ne 0 }} '
                 f'| Select-Object -ExpandProperty Id -First 1'],
                capture_output=True, text=True, timeout=5)
            pid = r.stdout.strip()
            if pid.isdigit():
                return int(pid)
        except Exception:
            pass
    for name in ('Weixin', 'WeChat'):
        try:
            r = subprocess.run(
                ['powershell', '-NoProfile', '-Command',
                 f'Get-Process {name} -ErrorAction SilentlyContinue '
                 f'| Sort-Object -Property Threads -Descending '
                 f'| Select-Object -ExpandProperty Id -First 1'],
                capture_output=True, text=True, timeout=5)
            pid = r.stdout.strip()
            if pid.isdigit():
                return int(pid)
        except Exception:
            pass
    return None


def auto_scan(on_status=None):
    """自动扫描微信进程内存提取数据库密钥。返回 hex 字符串或 None。"""
    if sys.platform != 'win32':
        return None
    pid = find_wechat_pid()
    if not pid:
        if on_status:
            on_status('微信未运行')
        return None
    from . import config, keys
    acct = config.account()
    db_path = keys.probe_db(acct)
    if on_status:
        on_status(f'微信 PID={pid}，开始内存扫描...')
    return scan_db_key(pid, db_path, on_status)
