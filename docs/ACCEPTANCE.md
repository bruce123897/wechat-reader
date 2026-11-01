# 实际验收记录

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
