"""通过 wx_key.dll 从运行中的微信进程提取数据库密钥和图片密钥。

DLL 随包自带（wxkit/bin/）。
API：
  InitializeHook(pid) → bool       挂钩微信进程
  PollKeyData(buf, size) → bool    轮询数据库密钥（64 字符十六进制）
  GetImageKey(buf, size) → bool    获取图片密钥（JSON）
  GetStatusMessage(buf, size, &level) → bool
  CleanupHook() → bool
  GetLastErrorMsg() → char*
"""
import ctypes
import ctypes.wintypes as W
import json
import os
import subprocess
import sys
import time


def _dll_candidates():
    here = os.path.dirname(os.path.abspath(__file__))
    yield os.path.join(here, 'bin', 'wx_key.dll')


def find_dll():
    for p in _dll_candidates():
        if os.path.exists(p):
            return p
    return None


class WxKeyDll:
    def __init__(self, dll_path=None):
        self.path = dll_path or find_dll()
        if not self.path or not os.path.exists(self.path):
            raise FileNotFoundError('wx_key.dll 未找到')
        bin_dir = os.path.dirname(self.path)
        if hasattr(os, 'add_dll_directory'):
            os.add_dll_directory(bin_dir)
        self.lib = ctypes.CDLL(self.path)
        self.lib.InitializeHook.argtypes = [W.DWORD]
        self.lib.InitializeHook.restype = ctypes.c_bool
        self.lib.PollKeyData.argtypes = [ctypes.c_char_p, ctypes.c_int]
        self.lib.PollKeyData.restype = ctypes.c_bool
        self.lib.GetStatusMessage.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        self.lib.GetStatusMessage.restype = ctypes.c_bool
        self.lib.CleanupHook.argtypes = []
        self.lib.CleanupHook.restype = ctypes.c_bool
        self.lib.GetLastErrorMsg.argtypes = []
        self.lib.GetLastErrorMsg.restype = ctypes.c_char_p
        self.lib.GetImageKey.argtypes = [ctypes.c_char_p, ctypes.c_int]
        self.lib.GetImageKey.restype = ctypes.c_bool

    def init_hook(self, pid):
        return self.lib.InitializeHook(W.DWORD(pid))

    def poll_key(self):
        buf = ctypes.create_string_buffer(256)
        if self.lib.PollKeyData(buf, 256):
            s = buf.value.decode('utf-8', errors='ignore').strip()
            if len(s) == 64:
                return s
        return None

    def get_status(self):
        buf = ctypes.create_string_buffer(512)
        level = ctypes.c_int(0)
        if self.lib.GetStatusMessage(buf, 512, ctypes.byref(level)):
            return buf.value.decode('utf-8', errors='ignore').strip(), level.value
        return '', 0

    def get_last_error(self):
        p = self.lib.GetLastErrorMsg()
        return p.decode('utf-8', errors='ignore') if p else ''

    def get_image_key(self):
        buf = ctypes.create_string_buffer(8192)
        if self.lib.GetImageKey(buf, 8192):
            s = buf.value.decode('utf-8', errors='ignore').strip()
            try:
                return json.loads(s)
            except (ValueError, TypeError):
                pass
        return None

    def cleanup(self):
        try:
            self.lib.CleanupHook()
        except Exception:
            pass


def find_wechat_pid():
    """查找微信主进程 PID（有主窗口的那个）。微信 4.x 进程名是 Weixin，旧版是 WeChat。"""
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


def auto_db_key(timeout=30, on_status=None):
    """自动从运行中的微信提取数据库密钥。返回 64 字符十六进制字符串或 None。"""
    dll_path = find_dll()
    if not dll_path:
        if on_status:
            on_status('wx_key.dll 未找到，跳过自动提取')
        return None

    pid = find_wechat_pid()
    if not pid:
        if on_status:
            on_status('微信未运行，跳过自动提取（启动微信后再试）')
        return None

    if on_status:
        on_status(f'检测到微信进程 PID={pid}，正在提取密钥...')

    dll = WxKeyDll(dll_path)
    try:
        if not dll.init_hook(pid):
            err = dll.get_last_error()
            if on_status:
                on_status(f'挂钩失败: {err}')
            return None

        deadline = time.time() + timeout
        while time.time() < deadline:
            key = dll.poll_key()
            if key:
                if on_status:
                    on_status('数据库密钥提取成功')
                return key
            time.sleep(0.5)

        if on_status:
            on_status(f'{timeout} 秒内未拿到密钥')
        return None
    finally:
        dll.cleanup()


def auto_image_key(on_status=None):
    """自动获取图片密钥。返回 {"accounts": [{"wxid": ..., "keys": [...]}]} 或 None。"""
    dll_path = find_dll()
    if not dll_path:
        return None
    try:
        dll = WxKeyDll(dll_path)
        return dll.get_image_key()
    except Exception:
        return None


def find_wechat_exe():
    """找电脑版微信的可执行文件。微信 4.x 是 Weixin.exe，旧版是 WeChat.exe。找不到返回 None。"""
    # 1. 正在运行的进程
    for name in ('Weixin', 'WeChat'):
        try:
            r = subprocess.run(
                ['powershell', '-NoProfile', '-Command',
                 f"Get-Process {name} -ErrorAction SilentlyContinue "
                 f"| Where-Object {{ $_.Path }} | Select-Object -ExpandProperty Path -First 1"],
                capture_output=True, text=True, timeout=5)
            p = r.stdout.strip()
            if p and os.path.exists(p):
                return p
        except Exception:
            pass
    # 2. 注册表
    try:
        import winreg
        for root, sub, exe in (
            (winreg.HKEY_CURRENT_USER, r'Software\Tencent\Weixin', 'Weixin.exe'),
            (winreg.HKEY_CURRENT_USER, r'Software\Tencent\WeChat', 'WeChat.exe'),
            (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Tencent\Weixin', 'Weixin.exe'),
            (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Tencent\WeChat', 'WeChat.exe'),
        ):
            try:
                with winreg.OpenKey(root, sub) as k:
                    for val in ('InstallPath', 'InstallDir'):
                        try:
                            d = winreg.QueryValueEx(k, val)[0]
                        except OSError:
                            continue
                        cand = os.path.join(d, exe)
                        if d and os.path.exists(cand):
                            return cand
            except OSError:
                continue
    except Exception:
        pass
    # 3. 卸载信息里的 DisplayIcon / InstallLocation
    try:
        r = subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             r"Get-ChildItem 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',"
             r"'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall' "
             r"-ErrorAction SilentlyContinue | ForEach-Object { $p=Get-ItemProperty $_.PSPath; "
             r"if ($p.DisplayName -match '微信|WeChat|Weixin') { $p.DisplayIcon; $p.InstallLocation } }"],
            capture_output=True, text=True, timeout=8)
        for line in r.stdout.splitlines():
            s = line.strip().strip('"').split(',')[0].strip('"')
            if not s:
                continue
            for cand in (s, os.path.join(s, 'Weixin.exe'), os.path.join(s, 'WeChat.exe')):
                if cand.lower().endswith('.exe') and os.path.exists(cand):
                    return cand
    except Exception:
        pass
    # 4. 常见安装目录
    bases = []
    for env in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA'):
        v = os.environ.get(env)
        if v:
            bases += [os.path.join(v, 'Tencent', 'Weixin'), os.path.join(v, 'Tencent', 'WeChat')]
    for d in 'CDEFG':
        bases += [rf'{d}:\Weixin', rf'{d}:\WeChat', rf'{d}:\Program Files\Tencent\Weixin']
    for b in bases:
        for exe in ('Weixin.exe', 'WeChat.exe'):
            cand = os.path.join(b, exe)
            if os.path.exists(cand):
                return cand
    return None


def launch_wechat(on_status=None):
    """启动电脑版微信。成功返回 True。"""
    exe = find_wechat_exe()
    if not exe:
        if on_status:
            on_status('没找到微信程序。请确认电脑版微信（4.x）已安装。')
        return False
    try:
        subprocess.Popen([exe], cwd=os.path.dirname(exe),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        if on_status:
            on_status(f'启动微信失败：{e}')
        return False


def close_wechat(on_status=None):
    """关闭电脑版微信（温和退出；不退登录）。"""
    for name in ('Weixin', 'WeChat'):
        try:
            subprocess.run(['taskkill', '/IM', f'{name}.exe', '/T', '/F'],
                           capture_output=True, text=True, timeout=10)
        except Exception:
            pass
