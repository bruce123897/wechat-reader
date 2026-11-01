"""路径与配置。所有路径都从这里取，别的模块不写死任何路径或账号。

数据目录默认 ~/.wxdb，可用环境变量 WXKIT_HOME 改。结构：
  ~/.wxdb/config.json           全局配置（选用的账号、同步间隔等）
  ~/.wxdb/<账号>/mirror/        明文镜像（可随时删，会重建）
  ~/.wxdb/<账号>/archive.db     档案库（只增不删，删了撤回原文就没了）
  ~/.wxdb/<账号>/keys.bin       密钥（Windows 下用 DPAPI 加密保存）
  ~/.wxdb/logs/                 后台日志
"""
import glob
import json
import os
import re
import sys

HOME = os.path.expanduser('~')
DATA = os.environ.get('WXKIT_HOME') or os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'customized'))
CFG_FILE = os.path.join(DATA, 'config.json')
TZ_HOURS = 8  # 时间一律按北京时间显示

DEFAULTS = {
    'account': None,        # 账号目录名，如 wxid_xxx_1a2b；None = 自动选唯一/最近活跃的
    'wechat_root': None,    # xwechat_files 的位置；None = 自动探测
    'interval': 20,         # 后台兜底同步间隔（秒）；有新消息时 3 秒内就会同步
    'max_chars': 6000,      # 单条命令默认输出上限（字）
    'ffmpeg': None,         # ffmpeg 路径；None = 从 PATH 找
    'sns_prefetch': True,    # 后台自动把新朋友圈图片+视频下载进缓存，防链接过期/删帖；关：wx config sns_prefetch false
    'sns_video_ua': True,    # 下载朋友圈视频时用微信客户端 UA（视频 CDN 需要，否则 400）
    'sns_media_max_mb': 200, # 单个媒体大小上限（MB）；超过的视频只存缩略图并记日志，避免撑满硬盘
}


def load():
    cfg = dict(DEFAULTS)
    try:
        with open(CFG_FILE, encoding='utf-8') as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save(cfg):
    os.makedirs(DATA, exist_ok=True)
    tmp = CFG_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({k: v for k, v in cfg.items() if k in DEFAULTS}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CFG_FILE)


def _roots_from_wechat_config():
    """微信 4.x 把“文件保存位置”写在 %APPDATA%/Tencent/xwechat/config/*.ini（内容就是一个目录）。"""
    out = []
    base = os.path.join(os.environ.get('APPDATA', ''), 'Tencent', 'xwechat', 'config')
    for ini in glob.glob(os.path.join(base, '*.ini')):
        try:
            p = open(ini, encoding='utf-8', errors='ignore').read().strip()
        except OSError:
            continue
        for cand in (os.path.join(p, 'xwechat_files'), p):
            if os.path.isdir(cand):
                out.append(cand)
    return out


def wechat_roots():
    cfg = load()
    if cfg.get('wechat_root'):
        return [cfg['wechat_root']]
    cands = _roots_from_wechat_config()
    cands += [os.path.join(HOME, 'Documents', 'xwechat_files')]
    cands += [f'{d}:\\xwechat_files' for d in 'CDEFG'] if sys.platform == 'win32' else []
    seen, out = set(), []
    for c in cands:
        k = os.path.normcase(os.path.abspath(c))
        if k not in seen and os.path.isdir(c):
            seen.add(k); out.append(c)
    return out


def accounts():
    """所有带 db_storage 的账号目录，按最近活跃排序。返回 [(目录名, 完整路径)]。"""
    found = []
    for root in wechat_roots():
        for d in glob.glob(os.path.join(root, '*', 'db_storage')):
            acct_dir = os.path.dirname(d)
            msg = glob.glob(os.path.join(d, 'message', 'message_*.db'))
            mt = max((os.path.getmtime(f) for f in msg), default=0)
            found.append((mt, os.path.basename(acct_dir), acct_dir))
    found.sort(reverse=True)
    return [(n, p) for _, n, p in found]


class Account:
    """一个微信账号：原始数据在哪、自己的 wxid、本工具给它的数据目录。"""

    def __init__(self, name, path):
        self.name = name                     # 目录名，如 wxid_xxx_2fa4
        self.path = path                     # 微信账号目录
        self.db = os.path.join(path, 'db_storage')
        self.attach = os.path.join(path, 'msg', 'attach')
        self.msgdir = os.path.join(path, 'msg')
        self.wxid = re.sub(r'_[0-9a-f]{4}$', '', name)   # 自己的 wxid
        self.home = os.path.join(DATA, name)
        self.mirror = os.path.join(self.home, 'mirror')
        self.archive = os.path.join(self.home, 'archive.db')
        self.keyfile = os.path.join(self.home, 'keys.bin')

    def __repr__(self):
        return f'Account({self.name})'


def account(name=None):
    cfg = load()
    name = name or os.environ.get('WXKIT_ACCOUNT') or cfg.get('account')
    accts = accounts()
    if not accts:
        raise SystemExit('!! 没找到微信数据目录（xwechat_files）。电脑版微信装了并登录过吗？'
                         '如果放在别处，用 wx config wechat_root <目录> 指定。')
    if name:
        for n, p in accts:
            if n == name or n.startswith(name):
                return Account(n, p)
        raise SystemExit(f'!! 没有账号 {name}。可选：' + '、'.join(n for n, _ in accts))
    return Account(*accts[0])


def log_dir():
    d = os.path.join(DATA, 'logs'); os.makedirs(d, exist_ok=True); return d
