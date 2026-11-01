"""后台常驻程序：盯着微信数据库，有变化就增量解密 + 入库。

  - 每 3 秒看一眼消息库文件的修改时间/大小（几乎不耗 CPU），变了就跑一轮；没变化也每 20 秒（配置 interval）跑一轮兜底
  - 一轮 = 镜像增量更新（mirror）→ 入库（ingest）；正常一轮 0.1～1 秒
  - 有待转写的语音就起一个最低优先级的子进程去转（转完自动退出）
  - 同一时间只允许一个实例（锁文件），状态写在 status.json，日志在 logs/daemon.log
  - 自己以“低于正常”优先级运行，不影响微信和其他程序
开机自启由计划任务负责（wx daemon install），任务每 5 分钟检查一次，挂了会被重新拉起。
"""
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import time

from . import archive, config, ingest, keys, mirror, voice

POLL = 3
STUCK = 900   # 连续这么久没成功同步就自杀重启
VOICE_BUDGET = 600


def _log():
    lg = logging.getLogger('wxkit')
    if not lg.handlers:
        h = logging.handlers.RotatingFileHandler(os.path.join(config.log_dir(), 'daemon.log'),
                                                 maxBytes=1 << 20, backupCount=3, encoding='utf-8')
        fm = logging.Formatter('%(asctime)s %(levelname)s %(message)s', '%m-%d %H:%M:%S')
        fm.converter = lambda t: time.gmtime(t + 8 * 3600)   # 日志也用北京时间
        h.setFormatter(fm)
        lg.addHandler(h); lg.setLevel(logging.INFO)
    return lg


class Lock:
    """单实例锁：进程退出（包括崩溃）时系统自动释放。"""

    def __init__(self, path):
        self.path = path; self.f = None

    def acquire(self):
        self.f = open(self.path, 'a+')
        try:
            if sys.platform == 'win32':
                import msvcrt
                self.f.seek(0); msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.f.close(); self.f = None
            return False


def lock_path():
    return os.path.join(config.DATA, 'daemon.lock')


def status_path():
    return os.path.join(config.DATA, 'status.json')


def read_status():
    try:
        return json.load(open(status_path(), encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def is_running():
    lk = Lock(lock_path())
    if lk.acquire():
        lk.f.close()
        return False
    return True


def _below_normal():
    if sys.platform == 'win32':
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
    else:
        os.nice(5)


def _sig(acct):
    """消息库及其 WAL 的 (修改时间, 大小)。变了说明有新消息。"""
    out = []
    for p in mirror.targets(acct):
        for f in (p, p + '-wal'):
            try:
                s = os.stat(f); out.append((f, s.st_mtime_ns, s.st_size))
            except OSError:
                pass
    return out


class Daemon:
    def __init__(self):
        self.log = _log()
        self.acct = None
        self.voice_proc = None
        self.stat = {'pid': os.getpid(), 'started': int(time.time()), 'cycles': 0, 'errors': 0}

    def _setup(self):
        acct = config.account()
        if not self.acct or acct.path != self.acct.path:
            self.acct = acct
            self.mirror = mirror.Mirror(acct, keys.db_key(acct))
            self.ingest = ingest.Ingest(acct)
            self.stat['account'] = acct.name
            self.log.info('账号 %s', acct.name)

    def cycle(self):
        t = time.time()
        changed = self.mirror.update_all()
        res = self.ingest.run(changed)
        new = sum(v[0] for k, v in res.items() if k.startswith('message_'))
        rec = sum(v[1] for k, v in res.items() if k.startswith('message_'))
        took = round(time.time() - t, 2)
        self.stat.update(cycles=self.stat['cycles'] + 1, last_cycle=int(time.time()), took=took)
        if new or rec:
            self.stat['last_new'] = int(time.time())
            self.log.info('新消息 %d 条，撤回 %d 条，用时 %.2fs', new, rec, took)
        return new

    def _voice(self):
        if self.voice_proc and self.voice_proc.poll() is None:
            return
        self.voice_proc = None
        if voice.pending(self.ingest.c) and voice.model_dir():
            exe = sys.executable
            if sys.platform == 'win32' and exe.lower().endswith('python.exe'):
                w = exe[:-10] + 'pythonw.exe'
                exe = w if os.path.exists(w) else exe
            with open(os.path.join(config.log_dir(), 'voice.log'), 'a') as log:   # 子进程继承句柄，这边用完就关
                self.voice_proc = subprocess.Popen(
                    [exe, '-m', 'wxkit.voice', str(VOICE_BUDGET)], cwd=os.path.dirname(os.path.dirname(__file__)),
                    stdout=subprocess.DEVNULL, stderr=log, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def _write_status(self):
        self.stat['heartbeat'] = int(time.time())
        tmp = status_path() + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.stat, f, ensure_ascii=False)
        os.replace(tmp, status_path())

    def run(self):
        lk = Lock(lock_path())
        if not lk.acquire():
            print('后台程序已经在运行'); return
        _below_normal()
        self.log.info('启动 pid=%d', os.getpid())
        last_sig, last_full, fails = None, 0, 0
        while True:
            try:
                self._setup()
                sig = _sig(self.acct)
                if sig != last_sig or time.time() - last_full > config.load()['interval']:
                    self.cycle(); last_sig = sig; last_full = time.time()
                    self._voice()
                self._prefetch()
                fails = 0
            except SystemExit as e:   # 没有密钥等：记下来，过一会再试
                self.stat['error'] = str(e); self.log.error('%s', e); fails += 1
            except Exception as e:
                self.stat['errors'] += 1; self.stat['error'] = repr(e)[:300]
                self.log.exception('一轮失败'); fails += 1
                if 'malformed' in str(e) and self.acct:   # 镜像库坏了：丢掉进度，下一轮整库重新解密
                    self._reset_mirror()
                self.acct = None   # 下一轮重新初始化
            try:
                self._write_status()
            except OSError:
                pass
            if fails and time.time() - self.stat.get('last_cycle', self.stat['started']) > STUCK:
                self.log.error('超过 %d 秒没有成功同步，退出让计划任务重新拉起', STUCK)
                os._exit(1)
            time.sleep(POLL if fails == 0 else min(60, POLL * 2 ** fails))

    def _prefetch(self):
        """后台把新朋友圈图片下载进缓存，防链接过期/删帖。单独线程、异常全吞、限速。"""
        import threading
        if not config.load().get('sns_prefetch'):
            return
        if getattr(self, '_prefetch_thread', None) and self._prefetch_thread.is_alive():
            return
        self._prefetch_thread = threading.Thread(target=self._prefetch_loop, daemon=True)
        self._prefetch_thread.start()

    def _prefetch_loop(self):
        import sqlite3
        from . import snsmedia
        while True:
            try:
                if config.load().get('sns_prefetch') and self.acct:
                    self._prefetch_batch(snsmedia, sqlite3)
            except Exception as e:
                self.log.warning('朋友圈预取出错：%r', e)
            time.sleep(60)

    def _prefetch_batch(self, snsmedia, sqlite3):
        acct = self.acct
        cur_file = os.path.join(acct.home, 'sns_prefetch.json')
        now = int(time.time())
        floor = now - 7 * 86400
        try:
            cur = int(json.load(open(cur_file, encoding='utf-8')).get('ts', floor))
        except (OSError, ValueError, AttributeError):
            cur = floor
        since = max(cur, floor)
        c = sqlite3.connect(f'file:{acct.archive}?mode=ro', uri=True, timeout=30)
        try:
            try:
                rows = c.execute('select tid, ts, media_json from sns where ts>=? and media>0 and gone=0 '
                                 'order by ts asc limit 50', (since,)).fetchall()
            except sqlite3.Error:
                return
        finally:
            c.close()
        done = 0
        advance = since
        for tid, ts, mj in rows:
            if done >= 10:
                break
            try:
                items = json.loads(mj) if mj else snsmedia.media_from_mirror(acct.mirror, tid)
            except (ValueError, TypeError):
                items = []
            cfg = config.load()
            ua = bool(cfg.get('sns_video_ua'))
            max_mb = cfg.get('sns_media_max_mb')
            d = snsmedia.cache_dir(acct, tid)
            cached = set(os.listdir(d)) if os.path.isdir(d) else set()
            for it in items:
                kind = 'video' if it.get('type') == 6 else 'image'
                markers = {str(it['i'])} if kind == 'image' else {f"{it['i']}_video", f"{it['i']}_thumb"}
                if any(f.rsplit('.', 1)[0] in markers for f in cached):
                    continue
                snsmedia.fetch_item(acct, tid, it, kind=kind, use_wx_ua=ua,
                                    max_mb=max_mb, log=self.log.info)
                done += 1
                time.sleep(0.5)
                if done >= 10:
                    break
            advance = max(advance, ts or 0)
        try:
            with open(cur_file, 'w', encoding='utf-8') as f:
                json.dump({'ts': advance}, f)
        except OSError:
            pass

    def _reset_mirror(self):
        for src in mirror.targets(self.acct):
            try:
                os.remove(os.path.join(self.acct.mirror, os.path.basename(src)) + '.state')
            except OSError:
                pass
        self.log.warning('镜像库损坏，已清掉进度，下一轮整库重建')


# ---------- 开机自启（Windows 计划任务） ----------
TASK = 'wechat-reader-bruce-daemon'
OLD_TASKS = ('wxkit-daemon',)


def _entry():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'wx.py')


def install_task():
    if sys.platform != 'win32':
        raise SystemExit('目前只做了 Windows 的开机自启；Mac 以后补（launchd）')
    exe = sys.executable
    w = os.path.join(os.path.dirname(exe), 'pythonw.exe')
    exe = w if os.path.exists(w) else exe
    ps = f'''
$a = New-ScheduledTaskAction -Execute '{exe}' -Argument '"{_entry()}" daemon run' -WorkingDirectory '{os.path.dirname(_entry())}'
$t1 = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$t2 = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -Priority 7
$k = New-ScheduledTask -Action $a -Trigger $t1,$t2 -Settings $s -Description 'WeChat for AI 后台同步：把微信新消息实时整理进本地档案库（只读）。维护者 Bruce，微信 bruce_jaychou'
$k.Author = 'Bruce'
Register-ScheduledTask -TaskName '{TASK}' -InputObject $k -Force | Out-Null
Start-ScheduledTask -TaskName '{TASK}'
'''
    for t in OLD_TASKS:   # 旧版本的任务名，换名后删掉
        ps += f"if (Get-ScheduledTask -TaskName '{t}' -ErrorAction SilentlyContinue) {{ Unregister-ScheduledTask -TaskName '{t}' -Confirm:$false }}\n"
    r = subprocess.run(['powershell', '-NoProfile', '-Command', ps], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit('!! 计划任务创建失败：' + (r.stderr or r.stdout)[-500:])


def uninstall_task():
    subprocess.run(['powershell', '-NoProfile', '-Command',
                    f"Unregister-ScheduledTask -TaskName '{TASK}' -Confirm:$false"], capture_output=True)


def stop():
    st = read_status()
    pid = st.get('pid')
    if pid and is_running():
        if sys.platform == 'win32':
            subprocess.run(['taskkill', '/PID', str(pid), '/F'], capture_output=True)
        else:
            os.kill(pid, 15)
        return pid
    return None


if __name__ == '__main__':
    Daemon().run()
