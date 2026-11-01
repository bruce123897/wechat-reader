---
name: wechat-reader-local
description: 使用此仓库独立实现的 Wechat Reader 查询本机已授权微信归档、同步状态、联系人群成员和本地媒体。适用于明确使用本项目的聊天读取；不能发送消息。
---

# Wechat Reader

入口使用本项目虚拟环境，不使用全局 `wx`。PowerShell：

```powershell
& 'D:\项目\wechat-reader\.venv\Scripts\python.exe' -m wechat_reader doctor --source --max-age 120
& 'D:\项目\wechat-reader\.venv\Scripts\python.exe' -m wechat_reader status
```

JSON 输出包含完整正文，分页通过 `next_cursor` 继续。时间支持 Unix 秒或带时区的 ISO 格式；区间为 `[since, until)`。

- 找人：`find '关键词'`。分别核对 `remark`（备注）、`nick`（昵称）、`alias`、`user`；重名用精确 user 与 account。
- 查消息：`search '关键词' --account ACCOUNT --chat USER --limit 50`；按发送者/类型/时间可加 `--sender USER --kind quote --since ISO --until ISO`。
- 通读会话：`read --account ACCOUNT --chat USER --since ISO --until ISO`，用 `--cursor TOKEN` 逐页读取。`msg ID` 查看完整长消息；`context ID --radius 6` 查看前后文。
- 会话/群：`inbox --unread`、`members ROOM --account ACCOUNT`。
- 新消息：`new --account ACCOUNT` 默认只查看，显式 `--consume` 才更新本工具阅读位置（不改微信未读）。结果按归档事件序列，包含更新/撤回事件。
- 媒体：`media ID` 返回本地路径/明确缺失原因；`--copy` 复制到自己的缓存。按 `media_kind/format/variant` 核实本体类型；`source_media_missing=true` 时只有封面，不可当成原视频/原图。
- 语音：`voice enqueue --id ID` → `voice run --model 'D:\项目\wechat-reader\data\models\faster-whisper-tiny'` → `voice status --id ID`。转写为机器结果，不当成人工核实原文。
- 导出：`export --account ACCOUNT --chat USER --format json --output 'D:\项目\wechat-reader\data\exports\chat.json'`。

同步异常时先查 doctor/status；需要更新归档时执行 `sync`，独立后台通过 `daemon start/stop/status` 管理。读取到哪个时间以同步成功时间和查询范围为准，后台故障/结果为空不等于没有消息。

关键词用于定位；对“未通知、未付款、已同意”等结论，必须读取相关时间段上下文。发言人按 sender/is_me 判断，名称按联系人字段核对。撤回前没有采集的原文不能恢复。未知类型、媒体缺失、模型不可用均按实际状态报告，不猜测内容。

私人正文、账号、密钥、数据库和原始日志保留在本机 data/；源文件只读，不操作旧工具档案。更多安装、故障与命令见仓库 [README](../../README.md)。
