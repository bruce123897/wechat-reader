"""语音转文字（本地离线，SenseVoice 模型）。

后台程序发现有待转的语音时，单独起一个低优先级子进程跑 `python -m wxkit.voice`，转完就退出，
这样平时后台程序本身不用把 200MB 的模型留在内存里。
流程：media_*.db 的 VoiceInfo(svr_id → silk 数据) → pilk 解码成 16k PCM → SenseVoice 识别。
结果写进档案库 voices 表，并补到对应消息的 text 里（[语音 7秒] 转写内容），这样搜索也能搜到。
"""
import array
import glob
import os
import shutil
import sqlite3
import sys
import tempfile
import time

from . import archive, config

MODEL_FILES = ('model.int8.onnx', 'tokens.txt')


def model_dir():
    d = os.path.join(config.DATA, 'models', 'sensevoice')
    if all(os.path.exists(os.path.join(d, f)) for f in MODEL_FILES):
        return d
    # 本机已有的模型（例如以前装过的工具里带的）直接复制一份过来
    for old in glob.glob(os.path.expanduser(r'~/Documents/*/models/sensevoice')):
        if all(os.path.exists(os.path.join(old, f)) for f in MODEL_FILES):
            os.makedirs(d, exist_ok=True)
            for f in MODEL_FILES:
                shutil.copy2(os.path.join(old, f), d)
            return d
    return None


_rec = None


def recognizer(threads=2):
    global _rec
    if _rec is None:
        import sherpa_onnx
        d = model_dir()
        if not d:
            raise RuntimeError('没有语音模型：运行安装脚本下载，或把 sensevoice 模型放到 ' + os.path.join(config.DATA, 'models'))
        _rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=os.path.join(d, 'model.int8.onnx'), tokens=os.path.join(d, 'tokens.txt'),
            use_itn=True, num_threads=threads)
    return _rec


def transcribe(data):
    import pilk
    i = data.find(b'#!SILK_V3')
    if 0 <= i < 16:
        data = data[i:]
    d = tempfile.mkdtemp()
    try:
        s, p = os.path.join(d, 'a.silk'), os.path.join(d, 'a.pcm')
        open(s, 'wb').write(data)
        pilk.decode(s, p, pcm_rate=16000)
        pcm = array.array('h', open(p, 'rb').read())
    finally:
        shutil.rmtree(d, ignore_errors=True)
    st = recognizer().create_stream()
    st.accept_waveform(16000, [v / 32768 for v in pcm])
    recognizer().decode_stream(st)
    return st.result.text.strip()


def import_legacy(c):
    """旧工具的转写缓存（~/wxdb/voice_cache.db）直接导入，不用重转。"""
    p = os.path.expanduser('~/wxdb/voice_cache.db')
    if archive.kv_get(c, 'voice_legacy') or not os.path.exists(p):
        return 0
    old = sqlite3.connect(f'file:{p}?mode=ro', uri=True)
    rows = [(t, s) for s, t in old.execute('select svr_id, txt from v') if t]
    old.close()
    with c:
        c.executemany('update voices set txt=?, state=1 where svrid=?', rows)
        archive.kv_set(c, 'voice_legacy', len(rows))
    apply_text(c)
    return len(rows)


def apply_text(c, svrids=None):
    """把转写结果补进消息正文。"""
    q = ("update msgs set text = substr(text, 1, instr(text, ']')) || ' ' || "
         "(select txt from voices v where v.svrid = msgs.svrid) "
         "where kind='voice' and svrid in (select svrid from voices where state=1 and txt != ''"
         + (f" and svrid in ({','.join(map(str, svrids))})" if svrids else '') + ')')
    with c:
        c.execute(q)


def _blob(mirror, svrid, conns):
    for m in conns:
        r = m.execute('select voice_data from VoiceInfo where svr_id=?', (svrid,)).fetchone()
        if r and r[0]:
            return r[0]
    return None


def pending(c):
    return c.execute('select count(*) from voices where state=0').fetchone()[0]


def work(acct, limit=None, budget=None):
    """转写待处理语音，新的优先。budget：最多跑多少秒。返回处理条数。"""
    c = archive.connect(acct, write=True)
    import_legacy(c)
    conns = [sqlite3.connect(f'file:{f}?mode=ro', uri=True)
             for f in sorted(glob.glob(os.path.join(acct.mirror, 'media_*.db')))]
    t0 = time.time(); n = 0; done = []
    rows = c.execute('select svrid from voices where state=0 order by ts desc' + (f' limit {int(limit)}' if limit else '')).fetchall()
    for (svrid,) in rows:
        if budget and time.time() - t0 > budget:
            break
        data = _blob(acct.mirror, svrid, conns)
        if not data:
            # 电脑版没下载过这条语音；最近 3 天的先留着，可能稍后才下载
            old = c.execute('select ts from voices where svrid=?', (svrid,)).fetchone()[0] < time.time() - 3 * 86400
            if old:
                with c:
                    c.execute('update voices set state=2 where svrid=?', (svrid,))
            continue
        try:
            txt = transcribe(data); st = 1
        except Exception as e:
            txt = f'(识别失败:{e.__class__.__name__})'; st = 2
        with c:
            c.execute('update voices set txt=?, state=? where svrid=?', (txt, st, svrid))
        done.append(svrid); n += 1
        if len(done) >= 20:
            apply_text(c, done); done = []
    if done:
        apply_text(c, done)
    return n


def _low_priority():
    if sys.platform == 'win32':
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x40)  # IDLE
    else:
        os.nice(19)


if __name__ == '__main__':
    from .daemon import Lock
    if not Lock(os.path.join(config.DATA, 'voice.lock')).acquire():
        sys.exit(0)   # 已经有一个在转了
    _low_priority()
    print(work(config.account(), budget=float(sys.argv[1]) if len(sys.argv) > 1 else None))
