"""密钥的保存与获取。

数据库密钥（32 字节）同一账号基本不变；微信重装或换电脑登录才可能变。
保存位置：<账号数据目录>/keys.bin，Windows 下用 DPAPI 加密（只有当前 Windows 用户能解开）。
内容：{"db": 数据库密钥 hex, "img_aes": 图片 AES 密钥, "img_xor": 图片异或值}

获取顺序（wx key 命令）：已保存的 → 导入（旧工具 k.txt / 其他工具的配置）→ 用户手动给（wx key --set）。
"""
import base64
import ctypes
import glob
import json
import os
import sys

from . import crypto


# ---------- Windows DPAPI ----------
def _blob_cls():
    import ctypes.wintypes as W

    class B(ctypes.Structure):
        _fields_ = [('n', W.DWORD), ('p', ctypes.POINTER(ctypes.c_char))]
    return B


def _dpapi(data, protect):
    if sys.platform != 'win32':
        return data
    B = _blob_cls()
    i = B(len(data), ctypes.create_string_buffer(data, len(data))); o = B()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(i), None, None, None, None, 0, ctypes.byref(o)):
        raise OSError('DPAPI 失败')
    out = ctypes.string_at(o.p, o.n); ctypes.windll.kernel32.LocalFree(o.p)
    return out


def load(acct):
    try:
        with open(acct.keyfile, 'rb') as f:
            return json.loads(_dpapi(f.read(), False).decode())
    except (OSError, ValueError):
        return {}


def save(acct, **kv):
    d = load(acct); d.update({k: v for k, v in kv.items() if v is not None})
    os.makedirs(acct.home, exist_ok=True)
    tmp = acct.keyfile + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(_dpapi(json.dumps(d).encode(), True))
    os.replace(tmp, acct.keyfile)
    if sys.platform != 'win32':
        os.chmod(acct.keyfile, 0o600)


def probe_db(acct):
    """拿一个肯定存在的库来验证密钥。"""
    for rel in ('contact/contact.db', 'session/session.db', 'message/message_0.db'):
        p = os.path.join(acct.db, *rel.split('/'))
        if os.path.exists(p) and os.path.getsize(p) >= crypto.PAGE:
            return p
    raise SystemExit('!! 账号目录里没有可用的数据库')


def db_key(acct):
    """返回已验证的原始数据库密钥（bytes）；没有就报错并说明怎么取。"""
    k = load(acct).get('db')
    if k and crypto.check_key(bytes.fromhex(k), probe_db(acct)):
        return bytes.fromhex(k)
    got = import_any(acct)
    if got:
        return got
    raise SystemExit('!! 没有可用的数据库密钥（可能是微信重装/换号了）。运行 wx key 按提示获取。')


# ---------- 导入来源（只读别人的文件，不依赖它们运行） ----------
def _from_legacy_ktxt():
    p = os.path.join(os.path.expanduser('~'), 'wxdb', 'k.txt')
    try:
        return [bytes.fromhex(open(p).read().strip())]
    except (OSError, ValueError):
        return []


def _electron_secrets(appdir, acct_name):
    """读其他 Electron 工具用 safeStorage 加密保存的字段。只读文件，不需要它在运行。"""
    try:
        from Crypto.Cipher import AES
        ls = json.load(open(os.path.join(appdir, 'Local State'), encoding='utf-8'))
        mk = _dpapi(base64.b64decode(ls['os_crypt']['encrypted_key'])[5:], False)
    except Exception:
        return {}
    c = {}
    for f in glob.glob(os.path.join(appdir, '*config*.json')):
        try:
            cfg = json.load(open(f, encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(cfg, dict) and ('decryptKey' in cfg or 'wxidConfigs' in cfg):
            c.update({k: v for k, v in cfg.items() if isinstance(v, str)})
            c.update((cfg.get('wxidConfigs') or {}).get(acct_name) or {})

    def un(v):
        s = base64.b64decode(v[5:])
        return AES.new(mk, AES.MODE_GCM, nonce=s[3:15]).decrypt_and_verify(s[15:-16], s[-16:]).decode()
    out = {}
    for src, dst in (('decryptKey', 'db'), ('imageAesKey', 'img_aes'), ('imageXorKey', 'img_xor')):
        try:
            out[dst] = un(c[src])
        except Exception:
            pass
    return out


def _electron_dirs():
    base = os.environ.get('APPDATA', '')
    return [d for d in glob.glob(os.path.join(base, '*')) if os.path.exists(os.path.join(d, 'Local State'))
            and glob.glob(os.path.join(d, '*config*.json'))]


def import_any(acct):
    """依次尝试各个导入来源，验证通过就保存。返回密钥或 None。"""
    probe = probe_db(acct)
    extra = {}
    cands = _from_legacy_ktxt()
    for d in _electron_dirs():
        s = _electron_secrets(d, acct.name)
        if s.get('db'):
            try:
                cands.append(bytes.fromhex(s['db']))
            except ValueError:
                pass
        for k in ('img_aes', 'img_xor'):
            if s.get(k) and not extra.get(k):
                extra[k] = s[k]
    for k in cands:
        if len(k) == 32 and crypto.check_key(k, probe):
            save(acct, db=k.hex(), **extra)
            return k
    return None


def image_keys(acct):
    """图片密钥：(aes_key bytes 或 None, xor int 或 None)。"""
    d = load(acct)
    if not d.get('img_aes'):
        import_any(acct); d = load(acct)
    aes = d.get('img_aes'); xor = d.get('img_xor')
    return (aes.encode()[:16] if aes else None), (int(xor) if xor not in (None, '') else None)
