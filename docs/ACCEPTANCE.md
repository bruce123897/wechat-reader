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

正在进行，尚不表示真实同步闭环完成。
