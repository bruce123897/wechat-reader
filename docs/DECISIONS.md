# 技术决策

## D001：先完成标准库数据核心

采用 Python 和 SQLite，先验证独立消息模型、事务、幂等导入与查询。HTTP、网页、MCP 接口后续复用核心服务。这样可以先定位数据正确性问题，再引入外围依赖。

## D002：真实接入和演示样例分开验收

演示数据明确标注为合成。真实微信适配器单独安排可行性验证；数据接入能力由本项目代码实现并验收。

## D003：保留真实开发历史

每个可检查工作单元单独提交，记录改动目的与验证结果；不补造历史日期，不将后续阶段提前标记完成。

## D004：版本快照与安全分页

唯一源标识为账号、分片、会话及 local_id；server_id 不作跨账号唯一键。当前消息及不可变版本一起提交，分页固定版本水位并按时间/id翻页，防止同步中的新增或更新时间改变造成重复/漏读。查询时间区间为 [since, until)，ISO 时间必须明确时区。

## D005：独立运行范围

运行输出只允许本项目 data/，源文件仅二进制只读打开，不在源路径建立 SQLite sidecar。核心使用标准库；真实解密使用 cryptography==46.0.3，压缩正文使用 zstandard==0.25.0，传递版本见 requirements-wechat.lock。Windows DPAPI 密钥文件只读导入，不提交密钥或路径。

## D006：完整变化分片对账与独立语音任务

每轮双遍读取 DB/WAL 验证内容一致，完整扫描变化分片，使用指纹持久游标跳过无变化分片。不只回看近期消息，优先捕获古老原地变化；此实现首次全量较慢，后续页级优化不能牺牲正确性。消息/版本/游标同事务。独立语音库在同步后对账补登记，崩溃间隙下轮修复；worker按需读取语音源，不在文字同步里运行模型。

## D007：经真实样本验证的语音依赖

本地 faster-whisper==1.2.1 + pilk==0.2.4 + av==16.1.0，完整传递依赖锁定 requirements-voice.lock。pilk 使用 pcm_rate=24000；faster-whisper1.2.1 与 av19 的 metadata_errors API 不兼容，已实际遇到并固定到16.1.0，参考[上游缺陷](https://github.com/SYSTRAN/faster-whisper/issues/1589)。模型来自[Systran/faster-whisper-tiny](https://huggingface.co/Systran/faster-whisper-tiny)，本次revision为 d90ca5fe260221311c53c58e660288d3deb8d356，文件sha256保留在data模型manifest。推理只用显式本地路径、CPU/int8，不自动下载，不上传音频。

