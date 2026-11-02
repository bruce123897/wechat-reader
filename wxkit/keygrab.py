"""获取数据库密钥。

正常路径（Windows）：挂钩运行中的微信，在它登录、打开数据库的那一刻把密钥读出来——
这就是"通过正常登录拿到密钥"。没登录就自动把微信拉起来，等你扫码登录，期间一直轮询，
拿到并用数据库第 1 页的 HMAC 验证通过才保存，绝不会存错。全程不需要你按回车、不改微信、不删数据。

获取顺序：
  1. 已经保存并验证通过的 keys.bin（重装脚本时直接复用，不折腾你）
  2. 本机其他工具留下的密钥文件 / 加密配置
  3. 自动：确保微信在运行（没运行就启动它），挂钩 + 轮询，直到登录完成捕获到密钥
  4. 内存扫描（备用，WeChat 4.x 上通常找不到）
  5. 手动：wx key --set <64位十六进制>
图片 AES 密钥优先复用/导入，再从微信内存 seed 派生并验证；图片异或值从本机图片文件尾反推。
"""
import sys
import time

from . import crypto, keys


def _verify_and_save(acct, raw_hex):
    try:
        raw = bytes.fromhex(raw_hex.strip())
    except (ValueError, AttributeError):
        return False
    if len(raw) == 32 and crypto.check_key(raw, keys.probe_db(acct)):
        keys.save(acct, db=raw.hex())
        return True
    return False


def _account_safe(acct=None):
    """拿当前账号；新机器上还没登录过微信时目录还不存在，返回 None 而不是崩。"""
    if acct is not None:
        return acct
    try:
        from . import config
        return config.account()
    except SystemExit:
        return None
    except Exception:
        return None


def _capture_windows(acct, timeout, launch, relaunch, on_status):
    """Windows：挂钩微信进程轮询数据库密钥。返回 True=拿到并验证通过。

    - 没检测到微信且 launch=True：自动启动微信（新机器会弹扫码，老机器自动登录）。
    - relaunch=True：即使微信在运行也先关掉再启动，强制走一次干净的登录捕获。
    - 全程轮询，不需要用户按回车；拿到候选密钥后等账号目录出现再验证保存。
    """
    from . import wxkey_dll

    if wxkey_dll.find_dll() is None:
        on_status('!! 缺少 wx_key.dll（应随包在 wxkit/bin/ 里），无法自动获取密钥')
        return False

    running = wxkey_dll.find_wechat_pid()
    # 关键：已经登录、数据库已打开的会话，挂钩读不到密钥——密钥只在"登录/打开数据库"那一刻能捕获。
    # 所以微信已经在运行时，必须重启它触发一次干净的登录（已登录的会自动重登，无需重新扫码）。
    if running and launch:
        on_status('正在重启微信以捕获密钥（已登录的会自动重新登录，无需重新扫码）…')
        wxkey_dll.close_wechat(on_status=on_status)
        time.sleep(2)
        running = None
    if not running:
        if launch and wxkey_dll.launch_wechat(on_status=on_status):
            on_status('已启动微信。新设备请在微信窗口扫码登录；已登录过的会自动登录，稍等即可。')
        elif not launch:
            on_status('（--no-launch）只挂钩已在运行的微信；若它已登录可能读不到密钥，必要时重启微信或去掉 --no-launch。')
        else:
            on_status('微信没在运行。请打开微信（新设备扫码登录），脚本会自动捕获密钥。')

    dll = None
    hooked_pid = None
    cand = None
    deadline = time.time() + timeout
    last_tick = 0
    try:
        while time.time() < deadline:
            pid = wxkey_dll.find_wechat_pid()
            if pid:
                if dll is None or hooked_pid != pid:
                    if dll:
                        dll.cleanup()
                    dll = wxkey_dll.WxKeyDll()
                    if dll.init_hook(pid):
                        hooked_pid = pid
                    else:
                        err = dll.get_last_error()
                        dll.cleanup()
                        dll = None
                        hooked_pid = None
                        if err:
                            on_status(f'  挂钩暂时失败（{err}），重试中…')
                if dll is not None:
                    k = dll.poll_key()
                    if k:
                        cand = k
            if cand:
                a = _account_safe(acct)
                if a and _verify_and_save(a, cand):
                    on_status('数据库密钥：已通过微信登录捕获并验证')
                    return True
            now = time.time()
            if now - last_tick >= 5:
                last_tick = now
                left = int(deadline - now)
                if cand and _account_safe(acct) is None:
                    on_status(f'  已拿到密钥，等微信完成首次登录生成数据目录…剩余 {left}s')
                else:
                    on_status(f'  等待微信登录并捕获密钥…剩余 {left}s（在微信窗口扫码登录即可）')
            time.sleep(1)
    finally:
        if dll:
            dll.cleanup()

    # 备用：内存扫描
    on_status('自动捕获超时，尝试内存扫描（备用）…')
    a = _account_safe(acct)
    if a:
        try:
            from . import memscan
            key_hex = memscan.auto_scan(on_status=lambda s: on_status(f'  {s}'))
            if key_hex and _verify_and_save(a, key_hex):
                on_status('数据库密钥：已从内存扫描提取并验证')
                return True
        except Exception as e:
            on_status(f'  内存扫描出错: {e}')
    return False


def db_key_interactive(acct, supplied=None, timeout=180, launch=True, relaunch=False, force=False, on_status=None):
    """拿到并验证数据库密钥。返回 True=成功。

    supplied: 用户用 wx key --set 直接给的 hex。
    force:    即使已有验证通过的密钥也重新捕获一次（强制走登录捕获）。
    relaunch: 先关微信再启动，强制一次干净登录（配合 force 用）。
    on_status: 进度回调；默认 print。AI/脚本调用时不需要任何交互。
    """
    say = on_status or print

    # 1. 手动给的密钥
    if supplied:
        a = _account_safe(acct)
        if a is None:
            say('!! 还没登录过微信（没有数据目录），先登录微信再用 wx key --set'); return False
        if _verify_and_save(a, supplied):
            say('数据库密钥：已验证并保存'); return True
        say('!! 你给的密钥验证不通过（不是这个账号的，或长度不对；要 32 字节 = 64 位十六进制）'); return False

    # 2. 已有且验证通过（force 时跳过，强制重抓）
    if not force:
        a = _account_safe(acct)
        if a is not None:
            saved = keys.load(a).get('db')
            if saved and crypto.check_key(bytes.fromhex(saved), keys.probe_db(a)):
                say('数据库密钥：已保存且验证通过'); return True

        # 3. 从本机其他工具导入
        if a is not None and keys.import_any(a):
            say('数据库密钥：已从本机已有的密钥文件导入并验证'); return True

    # 4. 自动捕获
    if sys.platform == 'win32':
        if _capture_windows(acct, timeout=timeout, launch=launch, relaunch=relaunch, on_status=say):
            return True
        say('')
        say('!! 自动获取没成功。可以这样：')
        say('   1) 确认微信已登录，然后重新运行 wx key')
        say('   2) 已有该账号的 32 字节数据库密钥时：wx key --set <64位十六进制>')
        say('   （密钥只在本机离线解你自己的聊天记录，以加密形式存在本机，不上传。）')
        return False

    # macOS / Linux：不自动挂钩（需要关 SIP / 调试权限），走导入或手动
    a = _account_safe(acct)
    if a is not None and keys.import_any(a):
        say('数据库密钥：已从本机已有配置导入并验证'); return True
    say('')
    say('!! macOS 上最省事的方式：wx key --set <64位十六进制>（不改微信、不关 SIP）。')
    say('   需要自动提取时：临时关 SIP，或清楚风险后 wx key --allow-resign。')
    return False


def image_key_interactive(acct, timeout=60, force=False):
    """图片密钥：优先复用，再导入或自动获取。只保存通过验证的扫描结果。"""
    from . import media
    if timeout <= 0:
        print('!! --timeout 必须大于 0 秒')
        return False, False
    try:
        x = media.guess_xor(acct)
    except OSError:
        x = None
        print('图片异或值：部分图片暂时不可读，将继续尝试获取密钥')
    if x is not None:
        keys.save(acct, img_xor=str(x))
        print(f'图片异或值：已从本机图片反推并保存（{x}）')
    else:
        print('图片异或值：本机暂时没有可用的图片来反推，等电脑版微信收到图片后再 wx key --image')
    ready = False
    if keys.load(acct).get('img_aes') and not force:
        print('图片 AES 密钥：已有')
        ready = True
    elif not force:
        try:
            keys.import_any(acct)
        except (OSError, ValueError, SystemExit):
            # 没有数据库样本也允许独立获取图片密钥。
            pass
        ready = bool(keys.load(acct).get('img_aes'))
        if ready:
            print('图片 AES 密钥：已从本机已有配置导入')
    if not ready:
        print('图片 AES 密钥：正在从微信内存获取并验证…')
        try:
            from . import imgkey
            hint = x if x is not None else keys.load(acct).get('img_xor')
            hit = imgkey.solve(acct, xor_hint=hint, on_status=lambda message: print('  ' + message), timeout=timeout)
            if hit:
                key, _seed, xor = hit
                keys.save(acct, img_aes=key.decode('ascii'), img_xor=str(xor))
                ready = True
                print('图片 AES 密钥：已自动获取、通过图片验证并加密保存')
            else:
                print('图片 AES 密钥：本次获取未成功；老格式图片仍可使用')
        except Exception as exc:
            print(f'图片 AES 密钥：获取未完成（{type(exc).__name__}），请检查微信状态后重试')
    d = keys.load(acct)
    return ready, d.get('img_xor') is not None
