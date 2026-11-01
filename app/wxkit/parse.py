"""单条原始消息 → (kind, text, extra)。只在入库时跑一次，之后所有查询都读整理好的结果。

local_type 是 64 位：低 32 位是主类型，高 32 位是子类型（appmsg 的 <type>）。
  1 文字  3 图片  34 语音  42 名片  43 视频  47 表情  48 位置  50 通话  10000/10002 系统
  49 = 卡片类，子类型：57 引用回复  19 转发的聊天记录  6 文件  5/4/1/3 链接  33/36 小程序
      51 视频号  62 拍一拍  2000 转账  2001 红包  87 群公告  53 接龙  8 表情
新增类型只要在这里加一个分支；kind 的取值见 KINDS。
"""
import html
import re

KINDS = ('text image voice video emoji location card call file link quote record miniapp channels '
         'pat transfer redpacket notice solitaire recall friend_added group_join group_leave group_rename '
         'group_admin group_disband redpacket_open payment system other').split()

_S = re.S


def tag(x, name):
    """取第一个 <name>…</name> 的内容（去 CDATA、反转义）。"""
    m = re.search(rf'<{name}(?:\s[^>]*)?>(.*?)</{name}>', x, _S)
    if not m:
        return ''
    v = m.group(1).strip()
    if v.startswith('<![CDATA['):
        v = v[9:-3] if v.endswith(']]>') else v[9:]
    return html.unescape(v).strip()


def attr(x, name):
    m = re.search(rf'\b{name}="([^"]*)"', x)
    return html.unescape(m.group(1)) if m else ''


def plain(x):
    """系统消息里的 XML/链接标签去掉，只留人能看的字。"""
    x = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', x, flags=_S)
    x = re.sub(r'<[^>]+>', '', x).replace('ecs_img', '').replace('ecs_jump_info', '')
    return re.sub(r'\s+', ' ', html.unescape(x)).strip()


def short(s, n):
    s = re.sub(r'\s+', ' ', s or '').strip()
    return s if len(s) <= n else s[:n] + '…'


def split_group_prefix(content):
    """群消息正文开头是 'wxid_xxx:\\n'，拆出来。"""
    m = re.match(r'^([A-Za-z0-9_\-@.]+):\n', content)
    return (m.group(1), content[m.end():]) if m else (None, content)


def _secs(ms):
    try:
        v = int(ms) / 1000
        return f'{v:.0f}秒' if v >= 1 else '1秒'
    except ValueError:
        return ''


# ---------- 各类型 ----------
def _quote(x):
    reply = tag(x, 'title')
    blk = re.search(r'<refermsg>(.*?)</refermsg>', x, _S)
    if not blk:
        return 'quote', reply, None
    b = blk.group(1)
    rtype = tag(b, 'type'); who = tag(b, 'displayname'); rc = tag(b, 'content')
    _, rc = split_group_prefix(rc)
    if rtype == '1':
        ref = rc
    elif rtype == '49':
        ref = tag(rc, 'title') or '[卡片]'
    else:
        ref = {'3': '[图片]', '34': '[语音]', '43': '[视频]', '47': '[表情]', '48': '[位置]'}.get(rtype, '[消息]')
    return 'quote', reply, {'ref_who': who, 'ref': short(ref, 120), 'ref_svrid': tag(b, 'svrid')}


_REC_TYPES = {'2': '[图片]', '3': '[语音]', '4': '[视频]', '5': '[链接]', '6': '[位置]', '8': '[文件]',
              '17': '[聊天记录]', '19': '[小程序]', '22': '[视频号]', '23': '[视频号]'}


def _record(x):
    title = tag(x, 'title')
    inner = tag(x, 'recorditem')
    items = []
    for dt, it in re.findall(r'<dataitem\b[^>]*?datatype="(\d+)"[^>]*>(.*?)</dataitem>', inner, _S):
        who = tag(it, 'sourcename')
        head = _REC_TYPES.get(dt, '')
        body = tag(it, 'datadesc') or tag(it, 'datatitle')
        body = f'{head} {body}'.strip() if head else body or '[内容]'
        items.append(f'{who}: {body}' if who else body)
    text = f'[聊天记录] {title}' + ('｜' + ' ／ '.join(items) if items else '')
    return 'record', text, {'title': title, 'n': len(items)}


def _transfer(x):
    fee = tag(x, 'feedesc') or ''
    m = re.search(r'([\d,]+(?:\.\d+)?)', fee) or re.search(r'转账([\d,.]+)元', tag(x, 'des'))
    amount = float(m.group(1).replace(',', '')) if m else None
    sub = tag(x, 'paysubtype')
    tid = tag(x, 'transferid') or tag(x, 'transcationid')
    state = {'1': '发起', '3': '已收款', '4': '已退还', '8': '已领取', '7': '待领取'}.get(sub, f'状态{sub}')
    ex = {'amount': amount, 'paysub': sub, 'tid': tid, 'payer': tag(x, 'payer_username'),
          'receiver': tag(x, 'receiver_username'), 'memo': tag(x, 'pay_memo')}
    amt = f'{amount:g}元' if amount is not None else '?元'
    text = f'[转账 {amt} {state} 单号尾{tid[-8:] if tid else "?"}]' + (f' 备注:{ex["memo"]}' if ex['memo'] else '')
    return 'transfer', text, ex


def _redpacket(x):
    wish = tag(x, 'receivertitle') or tag(x, 'sendertitle') or tag(x, 'des')
    sid = re.search(r'sendid=(\d+)', x)
    return 'redpacket', f'[红包] {wish}', {'sendid': sid.group(1) if sid else ''}


def _appmsg(x, sub):
    if sub == 57:
        return _quote(x)
    if sub == 19:
        return _record(x)
    if sub == 24:
        k, text, ex = _record(x)
        return 'record', text.replace('[聊天记录]', '[笔记]', 1), ex
    if sub == 2000:
        return _transfer(x)
    if sub == 2001:
        return _redpacket(x)
    title = tag(x, 'title'); des = tag(x, 'des'); url = tag(x, 'url')
    src = tag(x, 'sourcedisplayname') or tag(x, 'appname')
    if sub in (6, 74):
        size = tag(x, 'totallen')
        try:
            sz = f'{int(size) / 1048576:.1f}MB' if int(size) >= 1048576 else f'{int(size) // 1024 + 1}KB'
        except ValueError:
            sz = ''
        return 'file', f'[文件] {title}' + (f' ({sz})' if sz else '') + (' (发送中)' if sub == 74 else ''), \
            {'name': title, 'size': size, 'ext': tag(x, 'fileext'), 'md5': tag(x, 'md5')}
    if sub == 62:
        return 'pat', f'[拍一拍] {title}', None
    if sub == 8:
        return 'emoji', '[表情]', None
    if sub == 47:
        return 'redpacket', '[外部红包]', None
    if sub == 17:
        return 'location', '[位置共享]', None
    if sub in (115, 124):
        return 'other', '[微信礼物]', None
    if sub in (33, 36):
        return 'miniapp', f'[小程序] {title}' + (f'（{src}）' if src else ''), {'url': url}
    if sub == 51:
        return 'channels', f'[视频号] {tag(x, "nickname")}: {short(tag(x, "desc"), 80)}', None
    if sub == 87:
        body = tag(tag(x, 'announcement'), 'datadesc') or tag(x, 'textannouncement')
        return 'notice', f'[群公告] {short(body, 300)}', None
    if sub == 53:
        return 'solitaire', f'[接龙] {short(title, 300)}', None
    if sub == 63:
        return 'link', f'[直播] {title}', None
    if sub in (1, 3, 4, 5, 76, 92):
        head = '[音乐]' if sub in (3, 76) else '[链接]'
        text = f'{head} {title}' + (f' — {short(des, 60)}' if des else '') + (f'（{src}）' if src else '')
        return 'link', text, {'url': url}
    return 'other', f'[卡片{sub}] {title}' + (f' — {short(des, 60)}' if des else ''), {'url': url} if url else None


_SYS_RULES = [
    (r'撤回了一条消息$', 'recall'),
    (r'^你已添加了|现在可以开始聊天了|以上是打招呼的消息', 'friend_added'),
    (r'加入了群聊|加入群聊', 'group_join'),
    (r'移出群聊|退出了群聊|移出了群聊', 'group_leave'),
    (r'修改群名为', 'group_rename'),
    (r'群管理员|新群主|群主', 'group_admin'),
    (r'解散该群聊', 'group_disband'),
    (r'领取了.*红包|红包已被领完', 'redpacket_open'),
    (r'转账', 'payment'),
    (r'通话', 'call'),
]


def _template(x):
    """sysmsgtemplate：把 $name$ 换成 <link name="name"> 里列出的昵称。"""
    tpl = tag(x, 'template')
    for name in re.findall(r'\$(\w+)\$', tpl):
        blk = re.search(rf'<link\s+name="{name}".*?</link>', x, _S)
        blk = blk.group(0) if blk else ''
        nicks = re.findall(r'<nickname>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</nickname>', blk, _S)
        # link_profile 列昵称；link_plain 直接给文字；revoke 之类的按钮链接没有文字，去掉
        val = '、'.join(html.unescape(n) for n in nicks) or tag(blk, 'plain')
        tpl = tpl.replace(f'${name}$', val)
    return tpl


def _system(x, name_of):
    st = re.search(r'<sysmsg type="(\w+)"', x)
    st = st.group(1) if st else ''
    if st == 'revokemsg':
        return 'recall', plain(tag(x, 'content')) or '撤回了一条消息', None
    if st == 'sysmsgtemplate':
        text = plain(_template(x))
    elif st == 'mmchatroomtopmsg':
        return 'system', '[置顶了一条消息]', None
    elif st in ('paymsg', 'delchatroommember'):
        text = plain(tag(x, 'content') or tag(x, 'plain') or x)
    elif st:
        text = plain(tag(x, 'content') or tag(x, 'plain') or tag(x, 'text') or '') or f'[系统消息 {st}]'
    else:
        text = plain(x)
    text = re.sub(r'\$([\w\-@.]+)\$', lambda m: name_of(m.group(1)), text)
    for pat, kind in _SYS_RULES:
        if re.search(pat, text):
            return kind, text, None
    return 'system', text, None


def _voip(x):
    m = tag(x, 'diaplay_content') or tag(tag(x, 'VoIPBubbleMsg'), 'msg') or tag(x, 'msg') or '通话'
    return 'call', f'[通话] {m}', None


def parse(base, sub, content, source='', name_of=lambda u: u):
    """content 已去掉群前缀。返回 (kind, text, extra)。"""
    x = content or ''
    if base == 1:
        return 'text', x, None
    if base == 3:
        return 'image', '[图片]', {'md5': attr(x, 'md5')}
    if base == 34:
        return 'voice', f'[语音 {_secs(attr(x, "voicelength"))}]', {'ms': attr(x, 'voicelength')}
    if base == 43:
        return 'video', f'[视频 {attr(x, "playlength") or "?"}秒]', {'md5': attr(x, 'md5')}
    if base == 47:
        return 'emoji', '[表情]', None
    if base == 48:
        return 'location', f'[位置] {attr(x, "poiname")} {attr(x, "label")}'.strip(), None
    if base in (42, 66, 67):
        return 'card', f'[名片] {attr(x, "nickname")}', {'user': attr(x, 'username')}
    if base == 50:
        return _voip(x)
    if base in (10000, 10002):
        return _system(x, name_of)
    if base == 49:
        return _appmsg(x, sub)
    return 'other', f'[类型{base}] {short(plain(x), 80)}', None


def at_info(source, me):
    """群消息里 @ 了谁：'me' / 'all' / None。"""
    m = re.search(r'<atuserlist>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</atuserlist>', source or '', _S)
    if not m:
        return None
    lst = [s.strip() for s in m.group(1).split(',')]
    return 'me' if me in lst else 'all' if 'notify@all' in lst else None
