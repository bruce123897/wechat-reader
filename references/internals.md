# 内部原理、档案库表结构、改代码

## 数据流
```
微信加密库 db_storage/*.db (+ -wal)
   │  mirror.py：只解变化的页和新的 WAL 帧（空闲时一轮 0.04s）
   ▼
明文镜像 ~/.wxdb/<账号>/mirror/*.db      ← 结构和微信原库一样，可随时删（会重建）
   │  ingest.py + parse.py：新消息 + 最近 2 小时的消息（发现撤回、状态变化）
   ▼
档案库 ~/.wxdb/<账号>/archive.db          ← 只增不删；撤回原文、被删的朋友圈都在这
   │  query.py / render.py
   ▼
wx 命令（cli.py）                          ← 只读档案库，不碰镜像和微信
```
后台 daemon.py 是档案库唯一的写入者（语音子进程只写转写结果）。命令全是只读，所以多个查询同时跑没问题。

## 模块（每个文件只做一件事）
| 文件 | 做什么 | 要改什么时看它 |
|---|---|---|
| config.py | 数据目录、账号探测、配置文件 | 换路径、多账号 |
| crypto.py | SQLCipher 4 页解密（AES-256-CBC + HMAC-SHA512，PBKDF2 256000 次） | 微信换加密参数 |
| keys.py | 密钥保存（DPAPI 加密）、验证、从旧文件导入 | 密钥问题 |
| keygrab.py | 重新获取密钥：已有密钥导入、`--set` 手动给、图片异或值反推、图片 AES 交给 imgkey.py | 换号/重装后取不到密钥 |
| imgkey.py | V2 图片 AES 密钥：只读扫描微信进程的 seed，按 wxid 派生并用账号图片验证；带时限 | 图片解不开 |
| mirror.py | 增量镜像 | 同步慢/镜像损坏 |
| contacts.py | 联系人、群成员与群昵称、标签（protobuf 字段） | 名字显示不对 |
| parse.py | **一条原始消息 → kind + 一行文字 + extra**（所有消息格式都在这） | 新消息类型、显示不对 |
| ingest.py | 入库、撤回捕获、重新整理（reparse） | 撤回、漏消息 |
| social.py | 朋友圈、收藏；入库时把每个 media 解析成 media_json | 朋友圈 |
| snsmedia.py | 朋友圈图片/视频：解析 media、下载、ISAAC64 解密、缓存 | 朋友圈图取不到 |
| voice.py | 语音转文字（SenseVoice，离线） | 语音 |
| media.py | 聊天图片/视频/文件定位，图片解密（V1/V2 .dat） | 聊天图片打不开 |
| timeutil.py | 时间解析（北京时间） | 时间写法 |
| query.py | 查询接口（脚本也用它） | 写脚本 |
| render.py | 紧凑输出、截断、输出上限 | 输出格式 |
| daemon.py | 后台循环、单实例锁、开机自启 | 后台 |
| cli.py | 每个命令一个 `cmd_xxx` 函数，参数在 `build()` | 加命令 |

## 档案库表（`wx sql` 可直接查）
- `msgs(id, shard, chat, local_id, svrid, ts, sender, is_me, kind, text, extra, at, recalled_at, chash)`
  - chat：会话 wxid（群以 @chatroom 结尾）；sender：发送者 wxid；ts：unix 秒
  - kind：text image voice video emoji location card call file link quote record miniapp channels pat transfer redpacket notice solitaire recall friend_added group_join group_leave group_rename group_admin group_disband redpacket_open payment system other
  - text：整理好的一行（转账、引用、聊天记录等都已展开）；extra：JSON（转账 amount/tid/paysub/payer/receiver，引用 ref/ref_who，图片 file，文件 name/size/md5…）
  - at：群里 'me'（@我）/ 'all'；recalled_at：撤回时间（原文仍在 text）；kind='recall' 的行 = 工具运行前就撤回、没捕获到原文
- `names(user, remark, nick, alias, kind, labels)`：kind friend/group/official/other；labels 是标签 id 逗号串
- `members(room, user, gnick)`、`labels(id, name)`
- `sessions(chat, unread, summary, last_ts, sort_ts, hidden, draft, last_sender)`
- `voices(svrid, chat, ts, txt, state)`：state 0 待转 1 已转 2 本地没数据
- `sns(tid, user, ts, text, media, loc, link, likes, comments, gone, media_json)`、`sns_notes`（别人给我点赞评论）、`favs(id, type, ts, src, text)`
  - `media`：媒体数量（旧列，向后兼容）；`media_json`：每个媒体的结构化信息（v2.3.0 新增，老库升级后后台第一轮就自动 ALTER 补列并全量回填，不用等 sns.db 变化）
- 示例：`wx sql "select kind, count(*) from msgs where ts > strftime('%s','now','-7 day') group by kind"`

## 朋友圈图片/视频（snsmedia.py）
- 原始 XML 在镜像库 sns.db 的 `SnsTimeLine.content`；入库时 social.py 解析每个 `<media>` 存进 `sns.media_json`。
- 每个 media：`type`（2 图片 / 6 视频）、`url`/`key`/`token`/`md5`、`thumb_*`、`enc_key`、`live_*`（LivePhoto 附带的小视频）。
  属性顺序不固定（新帖多了 md5），一律按属性名取，不用位置正则；URL 要 `html.unescape`。
- 下载：图片 `url + "?token=…&idx=1"`，直连 qpic.cn（失败再走环境代理）；视频 url 自带 token，直接用。
  视频 CDN（`*.video.qq.com`）必须带微信 UA（`sns_video_ua`，默认开），否则 400（X-retmsg: sns illegal ua）。
- 解密（ISAAC64，大端，整块异或）：
  - 图片：seed = `url` 的 `key`（十进制 uint64），整文件异或。
  - 视频：seed = `<enc key>`，只异或前 131072 字节。视频的 url/thumb 的 `key="0"` 是占位符，真 seed 在 `<enc key>`。
  - LivePhoto 小视频：seed = liveMedia 的 `<enc key>`。没有 key 的 url = 未加密，原样保存。
- 缓存：`~/.wxdb/<账号>/sns_media/<tid>/<i>.jpg | <i>_video.mp4 | <i>_thumb.jpg`，已存在直接给路径。
- 后台预取（`sns_prefetch`，默认开）：新帖的图片+视频排队下载进缓存，单独线程、限速（≥0.5s/个）、异常全吞写日志、每 60s 一轮、每轮最多 10 个；进度游标存 `sns_prefetch.json`；默认范围=最近 7 天+升级后新帖；不一次性回填全部历史。
  单个媒体超过 `sns_media_max_mb`（默认 200）的视频只存缩略图并记日志。被删的帖（gone=1）已缓存的照常保留。
- 过期兜底（未实现，研究结论）：CDN 404 后本机微信缓存 `D:\xwechat_files\<acct>\cache\<月>\Sns\Img\<2位>\<30位>`（V2 格式，可用 media.decrypt_dat + wx key --image 解）。
  文件名=2 位子目录+30 位十六进制（2+30=32，疑似某值 md5 拆分），但未破解其输入：试过 md5/sha1/sha256（及截断）对 url、去 /0、CDN 路径段、带/不带 /mmsns/、md5 属性、媒体 id、key、token（url 与 thumb 两套）均不匹配；按大小反向内容匹配也不中（缓存是缩放显示版，非 /0 原图）；sns.db 无 url→文件映射表。暂按“过期”处理。

## 写脚本
```python
import sys, os; sys.path.insert(0, os.path.expanduser('~/.claude/skills/wechat-reader'))
from wxkit.query import DB, extra
db = DB()
for r in db.msgs(chat=db.chat('张三'), since=..., kinds=['transfer']):
    print(r['ts'], r['text'], extra(r).get('amount'))
```
要读原始 XML（极少需要）：镜像库 `~/.wxdb/<账号>/mirror/message_<shard>.db`，表名 `Msg_` + md5(chat)，行 = local_id。

## 微信库里的关键事实
- local_type 64 位：低 32 位主类型，高 32 位子类型（如 8589934592049 = 49 + 2000<<32 = 转账）
- 正文可能 zstd 压缩（开头 28 b5 2f fd）；群消息正文开头是 `wxid:\n`
- 撤回 = 微信把原来那一行原地改写成 `<sysmsg type="revokemsg">`，所以原文必须在撤回前存下（后台 3 秒一轮）
- 同一笔转账两条消息（paysubtype 1 发起 / 3 收款 / 4 退还），transferid 相同
- 转账方向：发起那条的发送者是付款方，收款那条的发送者是收款方。**收款那条里的 receiver_username 不是收款人**（常常是会话对方），不能拿来判断方向
- 图片本地文件名藏在 packed_info_data 里的 32 位 hex；V2 图片 = AES-ECB 头 + 明文 + 异或尾，异或值可从 JPG 结尾 FF D9 反推
- V2 图片 AES 密钥按 `md5(f'{seed}{wxid}').hexdigest()[:16]` 派生，XOR 为 `seed & 0xFF`。扫描候选按 XOR 优先验证，失败再试其余候选；只有同一账号图片样本都通过才保存。拒绝空样本和截断 AES 段；不向日志打印候选 seed 或密钥。
- `python -B -m unittest discover -s tests -v` 执行隔离回归测试。图片、密钥文件均使用临时目录；进程读取用模拟接口，不扫描真实微信。
- 消息按时间分片在 message_0.db、message_1.db…，(shard, chat, local_id) 唯一

## 移植到 Mac（还没做，改这几处即可）
解密、解析、入库、查询、渲染、语音都是纯 Python，跨平台。只跟 Windows 绑定的是：
| 位置 | Windows 做法 | Mac 要换成 |
|---|---|---|
| config.py 账号探测 | `文档\xwechat_files`、各盘 `xwechat_files` | `~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files` |
| keys.py 保存 | DPAPI 加密 keys.bin | 钥匙串（`security add-generic-password`），非 win32 分支现在是明文兜底 |
| keys.py 导入 | Electron `Local State` + safeStorage | Mac 的 safeStorage 密钥在钥匙串 “… Safe Storage” |
| daemon.py 单实例锁 / 自启 | msvcrt 文件锁 / 计划任务 | `fcntl.flock` / launchd plist（`~/Library/LaunchAgents`） |
| voice.py 低优先级 | 进程优先级类 | `os.nice(10)` |
| install.ps1 | PowerShell | 写一个对应的 install.sh |

## 改代码
- 改了 parse.py（显示规则）：`wx daemon stop && wx sync --reparse && wx daemon start`，已捕获的撤回原文不会被覆盖。
- 任何时候档案库都不要删：撤回原文和被删的朋友圈只有它有。镜像可以删。
