# 实际验收记录

路线已切换：以下为此前 `src/` 独立实现的验收记录，不代表当前 `app/` 定制源码已验收。当前维护者信息为 Bruce / bruce_jaychou；本次定制检查单独记录在 `docs/CUSTOMIZATION.md`。

日期：2026-11-01。本页仅记脱敏统计。合成测试与真实本机数据分开记录。

## M1：合成归档与查询

统一入口（已运行）：

```powershell
& 'D:\项目\wechat-reader\.venv\Scripts\python.exe' 'D:\项目\wechat-reader\scripts\acceptance.py'
```

初次执行 8 项通过。覆盖跨账号/分片、幂等、消息更新、已采集撤回、时间半开区间、固定版本快照分页、上下文、长消息、事务回滚续跑、查询期间同步、只读连接、联系人重名及阅读位置。

```powershell
& 'D:\项目\wechat-reader\.venv\Scripts\python.exe' -m wechat_reader --db 'D:\项目\wechat-reader\data\synthetic.db' import 'D:\项目\wechat-reader\examples\synthetic.json'
& 'D:\项目\wechat-reader\.venv\Scripts\python.exe' -m wechat_reader --db 'D:\项目\wechat-reader\data\synthetic.db' search '项目'
```

首次导入 2 条；再次导入 inserted=0 / unchanged=2；关键词返回 1 条合成消息。分页 token 固定归档版本水位，插入及更新时间改变后仍与起始快照一致。

受控反向检查：第 7 条时主动抛出异常，整批回滚，消息数 0、游标未提交、doctor.ok=false；恢复完整导入后 doctor.ok=true，结果与一次完整导入相同。

## 真实本机验证

### M2：独立真实源

本项目 Source 直接读取微信 db_storage，只读本机已有 DPAPI 密钥文件并将加密文件复制到本项目 data/keys；不读取旧档案库或镜像、不调用 wx。SQLCipher4 使用逐页 HMAC-SHA512 认证，WAL 校验 checksum 且只应用最后提交之前的帧；源数据双遍读取哈希一致后，SQLite 只对内存明文快照查询。加密格式依据 [SQLCipher 官方设计](https://www.zetetic.net/sqlcipher/design/) 与 [API](https://www.zetetic.net/sqlcipher/sqlcipher-api/)，实现为本项目独立代码。

实际运行：

```powershell
& 'D:\项目\wechat-reader\.venv\Scripts\python.exe' 'D:\项目\wechat-reader\scripts\local_acceptance.py' --setup
```

发现 2 个账号目录，1 个密钥可用，另 1 个未解锁；只验证可用账号。首次入库 inserted=215513、4 个批次，58.360 秒；联系人 8840、群成员关系 8988、会话 390（其中未读会话 54）。消息空发送者 4156 条为源字段为空，保留未知，不根据语气补造。

真实验证前后 8 个源 DB/WAL 文件 SHA256 相同。原工具进程、任务、档案未修改。未直接制造真实微信撤回或发消息；撤回/古老更新场景通过独立源合成测试验证。

7 项源适配测试包含真实 SQLite WAL 合成夹具、错误密钥与页认证、WAL 损坏及未提交帧、空新分片、源不变、变化游标和 schema 失败。统一入口现已覆盖这些测试。

### M3/M4：真实同步及查询

首次同步后原样重复：inserted=0、updated=0、batches=0（指纹一致跳过）。真实归档分页前两页各2条且不重叠，上下文返回3条；会话与联系人统计见上。

受控 OSError 仅注入本项目同步入口，没有改源：doctor.ok=false、sync_errors=1；恢复真实源同步后 doctor.ok=true、sync_errors=0、dangling_versions=0。原始脱敏 JSON 在 data/local-acceptance.json；脚本可复现，运行资料不入 Git。

新增独立撤回事件关联（同账号/会话/server_id），只保留此前已采集正文；先观察到撤回后再出现的陈旧源正文不得冒充恢复。精确 user 优先于名称匹配。所有变化分片全量重扫，能发现很早的原地更新；没有只看最近两小时的漏检窗口。每个变化分片是独立事务，批次中断后已提交分片保留、未提交分片重跑。`sync --full` 重解析全部源而不重复插入。

实际全量重读：inserted=0、updated=0、unchanged=215513、batches=4；doctor.ok=true。新增 CLI/同步测试验证错误退出码2、JSON导出、数据输出目录限制、单实例锁、部分分片成功后失败续跑与一次完整同步相同。统一测试此时为 37 项通过。

### M5：媒体、语音、入口及安装

```powershell
& 'D:\项目\wechat-reader\scripts\install.ps1' -Voice
& 'D:\项目\wechat-reader\.venv\Scripts\python.exe' 'D:\项目\wechat-reader\scripts\local_media_acceptance.py'
```

安装脚本已实际执行：本项目虚拟环境及固定版本依赖安装成功，pip check 无冲突，统一验收43项通过（后续增加测试见最终记录）。真实本地媒体结果：最近20条图片20条解码可读；最近20条视频消息只找到JPEG缩略图，variant=thumbnail，不能算20条视频本体可读；全部348条文件消息中218可用、89本地未找到、8文件内容哈希不符、33多候选歧义。没有将缺失或错误版本当作可用。DAT容器额外抽样20份，14份PNG/JPG，6份WXGF明确unsupported；源附件样本哈希前后一致。

真实CLI链路 `voice enqueue → voice run --id → voice status` 使用源库独立提取SILK，24k单声道PCM解码后本地tiny模型返回done（转写11字符；正文不公开）。本次语音样本为2.14秒，仅证明可运行，不证明准确率。故障阶段曾分别遇到pilk参数TypeError和av19接口TypeError，队列均报告failed并可retry；修复后成功。缺媒体/缺模型/未知格式/过期worker/多worker争抢/登记中断原子回滚由自动测试验证。

模型显式下载4个文件共78203619字节，revision见DECISIONS；此后推理local_files_only。私人语音和转写仅在data/。统一安装不会修改全局wx、已装Skill或旧任务。

仓库内 Skill 已用 skill-creator 的 quick_validate.py（Python -X utf8）验证有效；首次默认GBK读取失败，改UTF8模式后通过。Skill仅位于skills/wechat-reader-local，没有安装到全局目录。

后台首次启动暴露Windows虚拟环境launcher PID与实际daemon PID不同的问题，已改为以锁保护的最新心跳状态确认；修复后 daemon start 返回started=true，实际进程独立运行。心跳线程在长同步期间持续更新，不把长批次误报为退出；最终运行状态见本页末尾。

执行范围偏差：首次手工pip安装解密依赖遗漏缓存/临时目录参数，可能写入默认用户缓存及TEMP；包本体仍在项目.venv。后续安装脚本限制到data/tmp并禁用pip缓存。该偏差不能通过“包在venv”抹去，见BLOCKED第4项；未对范围外内容执行清理。

媒体角色复核：全部474条视频消息中122条为文件头验证过的MP4本体，307条仅JPEG封面，13条歧义、32条缺失；最近20条全部仅封面。结果新增requested_kind/media_kind/format/source_media_missing字段，视频封面强制variant=thumbnail且source_media_missing=true，原图身份不明确时标unclassified。已读一个1697892字节视频的头部确认ftyp偏移4；不能把缩略图计入视频本体成功数。

最终 `local_media_acceptance.py` 已用上述角色逻辑完整运行：图片最近20条也均为可用缩略图，不能算原图；474条视频统计与上文一致，并通过MediaResolver.read完整读取1697892字节MP4本体；348条文件统计一致，完整读取281520字节文件样本；真实语音仍done（11字符）。历史DAT解密抽样与“最近20条仅缩略图”是不同样本口径。

## 最终复验

统一入口 `scripts/acceptance.py`：**51项通过**。最终增加媒体角色、同批首次观察撤回不保留伪原文、并发语音缓存、源指纹相同仍须验证当前密钥的回归。并发缓存测试确实暴露了Windows替换已有打开文件的PermissionError；改为临时完整文件硬链接发布不可变内容、已存在时核验哈希后通过，没有删除断言或跳过测试。

`doctor --source --max-age 120`：ok=true / integrity=ok / sync_errors=0 / dangling_versions=0；源4库加密页认证通过。`daemon start --interval 20` 返回started=true，随后真实轮询running=true、error=null；stop/start均已验，交付保留本项目daemon运行。语音队列done=1、pending=1245；未自动把历史语音全量转写，不把任务登记数算成转写成功数。

最终真实归档复验：215513消息、8840联系人、8988群关系；选取一个真实群读取78名成员，精确联系人查询成功；独立acceptance阅读位置推进成功，导出一个时间窗口1条消息；再次源前后8文件哈希一致。当前无变化同步约0.875秒（单次观测，不作延迟保证）。故障注入后自检false，恢复后true。

`scripts/check_repository.py`：known-private-value扫描无发现、原始4a64050历史仍为祖先。GitHub MCP元数据确认仓库visibility=private。`git diff --check`无格式错误。文档中的初次统计与最终复验分别记录；data/local-*.json为最近一次，data/acceptance/保留后续时间戳证据。

功能未全部完成：第二账号密钥、WXGF转换、明文探针清理及写入范围偏差见BLOCKED.md。朋友圈/收藏/转账统计与网页/MCP/向量问答尚未开发，仍是后续批次。
