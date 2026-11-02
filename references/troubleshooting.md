# 出问题怎么办

先跑 `wx doctor`，按 ✗ 后面的提示做。下面是具体情况：

| 现象 | 原因 | 处理 |
|---|---|---|
| 输出顶部 `⚠ 后台同步没在运行` | 后台程序没起来 | `wx daemon start`；开机自启：`wx daemon install`；看日志 `~/.wxdb/logs/daemon.log` |
| 查不到刚发的消息 | 后台落后，或电脑版微信没登录/没在线 | `wx sync`（会等后台跑完一轮）；手机上的消息要电脑版微信在线才会落到本地 |
| `没有可用的数据库密钥` | 微信重装、换号、换电脑 | `wx key`（从本机已有密钥文件自动导入）；导入不到时关闭微信 → 重新打开 → 立即运行 `wx key`；还不行就用 `wx key --set <64位hex>` |
| 图片 `没有图片密钥` | V2 图片要 AES 密钥 | `wx key --image`（优先复用/导入，再从 Windows 微信进程自动获取并用图片验证） |
| `wx key --image` 提示没有完整 V2 图片 | 本机没有可验证的样本，或图片文件不完整 | 在电脑版微信登录目标账号，打开一张聊天图片后再试 |
| `wx key --image` 提示没有候选或验证失败 | 微信未运行、无读取权限，或登录账号与图片账号不同 | 确认微信运行且登录目标账号；不会保存未通过验证的候选 |
| `wx key --image` 达到扫描时限 | 默认总时限为 60 秒 | 可用 `wx key --image --timeout 120` 延长 |
| 已有图片密钥但新图仍解不开 | 缓存密钥可能已经失效 | `wx key --image --force`；失败保留原缓存，但命令返回失败状态 |
| 图片“本地没有” / 只有缩略图 | 电脑版没下载原图 | 在电脑版微信里点开那张图，再 `wx media` |
| 朋友圈图显示 `[已过期 404]` | CDN 链接失效（发布约 1~2 周后、或对方删帖） | 没救了，只能提前靠后台预取（`sns_prefetch` 默认开）；本机微信缓存的映射规则未破解，拿不到 |
| 朋友圈视频 `[视频需开启微信 UA 开关…]` | `sns_video_ua` 被关了 | `wx config sns_video_ua true`（默认本就是开的） |
| 朋友圈视频只存了缩略图 | 视频超过 `sns_media_max_mb`（默认 200MB） | 调大：`wx config sns_media_max_mb 500`，再 `wx sns-media tid:… --kind video --force` |
| 不想后台自动存朋友圈图/视频 | — | `wx config sns_prefetch false`（已存的不动） |
| 语音只有 `[语音 N秒]` 没文字 | 还在后台排队转，或电脑没下载这条语音 | `wx voice` 看进度；`wx voice --run 60` 立即转一批 |
| `"X" 对应多个会话` | 同名的人/群 | 用输出里的 wxid 代替名字 |
| `档案库还没建好` | 第一次用 | `wx sync`（全量约 1 分钟） |
| 显示规则改了想重整旧消息 | — | `wx daemon stop && wx sync --reparse && wx daemon start` |
| 多个微信号 | — | `wx --account wxid开头 …`，或 `wx config account <目录名>` |
| 微信数据不在默认位置 | — | `wx config wechat_root D:\xwechat_files` |

数据位置：`~/.wxdb/`（`WXKIT_HOME` 可改）。`mirror/` 可删可重建；**`archive.db` 不要删**（撤回原文只有它有）。
停用：`wx daemon uninstall && wx daemon stop`。
