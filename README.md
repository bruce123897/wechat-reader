# Wechat Reader

本地微信聊天记录读取与归档工具，围绕后台同步、消息解析、会话查询和上下文检索构建完整使用流程。

已独立接入本机 Windows 微信 4.x 源数据库，提供 SQLite 归档、后台增量同步、联系人与群、查询、媒体和本地语音任务队列。真实本机验收已入库 215,513 条消息；验收边界见 [ACCEPTANCE](docs/ACCEPTANCE.md)，剩余事项见 [BLOCKED](BLOCKED.md)。

目标流程：微信数据源 → 消息解析与增量归档 → 会话与上下文检索 → CLI / Skill 调用。

Python + SQLite 为数据核心；网页、MCP 和向量检索尚未实现。

功能范围见 [SCOPE](docs/SCOPE.md)，项目路线见 [ROADMAP](docs/ROADMAP.md)，技术取舍见 [DECISIONS](docs/DECISIONS.md)。仓库保持私有。

代码、文档、修复通过真实 Git 提交持续保存。私人聊天、数据库、密钥和运行日志不纳入仓库。

## 安装与自检

Python 3.12+，本项目独立虚拟环境；不覆盖全局 `wx` 或已安装 Skill。

```powershell
& 'D:\项目\wechat-reader\scripts\install.ps1' -Voice
& 'D:\项目\wechat-reader\.venv\Scripts\python.exe' 'D:\项目\wechat-reader\scripts\acceptance.py'
```

不需要语音依赖时省略 `-Voice`。依赖按 lock 文件固定；pilk 在 Windows 可能需要已有 C++ 编译工具。可选模型由下面的显式命令下载约 78MB 到本项目 data/models，不上传音频：

```powershell
& 'D:\项目\wechat-reader\.venv\Scripts\python.exe' 'D:\项目\wechat-reader\scripts\download_voice_model.py'
```

## 接入自己的微信数据

```powershell
& 'D:\项目\wechat-reader\.venv\Scripts\python.exe' -m wechat_reader discover
& 'D:\项目\wechat-reader\.venv\Scripts\python.exe' -m wechat_reader configure --source '实际账号目录'
& 'D:\项目\wechat-reader\.venv\Scripts\python.exe' -m wechat_reader probe
& 'D:\项目\wechat-reader\.venv\Scripts\python.exe' -m wechat_reader sync
& 'D:\项目\wechat-reader\.venv\Scripts\python.exe' -m wechat_reader doctor --max-age 120
```

`discover` 在本机输出账号路径，请不要对外转发。`configure` 的 source 是含 db_storage 的账号目录。默认只读导入同账号现有 DPAPI keys.bin；也可 `--key-file '密钥文件路径'` 明确提供含 db（64位hex）的 JSON 或 DPAPI JSON。验证通过后密钥文件复制到本项目 data/keys；不自动抓取进程内存、不依赖旧工具进程或档案。只配置一个活动源，多账号数据可累计归档并按 account 隔离查询；其他账号需各自有效密钥。

当前支持观测到的微信 4.x / 4096页 / SQLCipher4 配置。变化分片完整对账，不限制更新回看窗口；指纹一致时跳过，`sync --full` 强制重解析。源文件只读，不建立源侧临时库/SHM。写入只允许本项目 data/。明文归档属于私人数据，Git 忽略并不等于加密。

## 查询与导出

所有命令默认 JSON 输出，失败退出码为2。`--db` 是顶层参数，可用 data/ 内其他归档路径；未指定时使用 data/archive.db。

`doctor` 默认检查归档与同步错误；加 `--source` 同时校验当前源目录及密钥（即使源指纹没变化也验证）。

```powershell
& 'D:\项目\wechat-reader\.venv\Scripts\python.exe' -m wechat_reader find '关键词'
& 'D:\项目\wechat-reader\.venv\Scripts\python.exe' -m wechat_reader inbox --unread
& 'D:\项目\wechat-reader\.venv\Scripts\python.exe' -m wechat_reader search '关键词' --limit 50
& 'D:\项目\wechat-reader\.venv\Scripts\python.exe' -m wechat_reader read --account ACCOUNT --chat USER --since '2026-11-01T00:00:00+08:00' --until '2026-11-02T00:00:00+08:00'
& 'D:\项目\wechat-reader\.venv\Scripts\python.exe' -m wechat_reader context 123 --radius 6
& 'D:\项目\wechat-reader\.venv\Scripts\python.exe' -m wechat_reader msg 123
& 'D:\项目\wechat-reader\.venv\Scripts\python.exe' -m wechat_reader members ROOM --account ACCOUNT
& 'D:\项目\wechat-reader\.venv\Scripts\python.exe' -m wechat_reader new --account ACCOUNT --consume
& 'D:\项目\wechat-reader\.venv\Scripts\python.exe' -m wechat_reader export --account ACCOUNT --chat USER --format json --output 'D:\项目\wechat-reader\data\exports\chat.json'
```

将 ACCOUNT/USER/ROOM/数字编号替换为实际查询返回值。备注 remark、昵称 nick、微信号 alias 分开；重名需明确 user。过滤还支持 `--sender`、`--kind`；时间为 Unix秒或带时区ISO，区间 `[since,until)`。关键词是字面子串。`next_cursor` 配合相同过滤条件的 `--cursor` 稳定翻页，即使同步期间更新消息时间也保持起始快照。

`new` 返回归档事件（包含消息更新/撤回）；默认不移动本工具阅读位置，显式 `--consume` 才消费返回部分，不改微信未读数。`read`、`search`、`msg` 不截断单条长消息。导出读取全部匹配消息，支持 json/csv/md，CSV对公式前缀转义。

## 后台与语音

```powershell
& 'D:\项目\wechat-reader\.venv\Scripts\python.exe' -m wechat_reader daemon start --interval 20
& 'D:\项目\wechat-reader\.venv\Scripts\python.exe' -m wechat_reader daemon status
& 'D:\项目\wechat-reader\.venv\Scripts\python.exe' -m wechat_reader daemon stop
& 'D:\项目\wechat-reader\.venv\Scripts\python.exe' -m wechat_reader media 123 --copy
& 'D:\项目\wechat-reader\.venv\Scripts\python.exe' -m wechat_reader voice enqueue --id 123
& 'D:\项目\wechat-reader\.venv\Scripts\python.exe' -m wechat_reader voice run --id 123 --model 'D:\项目\wechat-reader\data\models\faster-whisper-tiny'
& 'D:\项目\wechat-reader\.venv\Scripts\python.exe' -m wechat_reader voice work --limit 10 --model 'D:\项目\wechat-reader\data\models\faster-whisper-tiny'
& 'D:\项目\wechat-reader\.venv\Scripts\python.exe' -m wechat_reader voice status --id 123
```

后台仅启动本项目独立进程，锁、心跳和日志均在 data/；不替换旧计划任务、不重启微信。停止使用请求文件，完成当前安全批次后退出。同步自动登记未撤回语音到独立队列；`voice run/work` 在另一个进程按需解密语音源库、转写并持久保存，文字查询不用等待。worker 失败、模型不可用、文件不存在均有明确状态；`voice retry --id 123` 可重试。tiny 模型仅验证可运行，转写准确度未作人工评测。

本地图片支持普通格式及V1/V2 DAT解密；WXGF封装明确 unsupported。用 media_kind、format、variant 区分本体与缩略图；source_media_missing=true 表示原媒体未找到，即使封面可读也不能说视频可读。媒体未下载、哈希不符、同名歧义均有明确状态。撤回只保留撤回前已采集的正文，不能恢复此前未采集内容。

## 合成演示与真实复验

```powershell
& 'D:\项目\wechat-reader\.venv\Scripts\python.exe' -m wechat_reader --db 'D:\项目\wechat-reader\data\synthetic.db' import 'D:\项目\wechat-reader\examples\synthetic.json'
& 'D:\项目\wechat-reader\.venv\Scripts\python.exe' 'D:\项目\wechat-reader\scripts\local_acceptance.py'
& 'D:\项目\wechat-reader\.venv\Scripts\python.exe' 'D:\项目\wechat-reader\scripts\local_media_acceptance.py'
```

local 脚本会读取已配置的本机数据，输出统计而不输出正文；首次配置且恰好一个账号可解锁时可给 local_acceptance 加 `--setup`。运行前若自有后台正在同步，可先停止本项目 daemon 以避免单实例锁竞争。保留 data/archive.db、data/keys、模型、配置与验收 JSON 以续跑；不用的明文探针见 BLOCKED.md。

仓库内 Skill：[wechat-reader-local](skills/wechat-reader-local/SKILL.md)，直接读取入口即可调用。本次未写入全局技能目录。

