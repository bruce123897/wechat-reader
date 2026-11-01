# 阻断与明确边界

1. 第二个发现的账号没有可用的本机数据库密钥。已尝试其现有 DPAPI 文件的只读解锁与 db 字段验证，明确失败；没有读取它的旧档案库冒充接入。下一步是为该账号提供有效、本人授权的密钥文件，再 configure/probe。当前仅已解锁账号完成真实接入。
2. WXGF 图像容器尚未完成可靠转换，返回 unsupported；普通图片/视频/文件及文字功能继续运行。后续验证成熟解码器，不能把容器字节冒充 JPEG。
3. 本次临时明文探针 data/probe/contact0.db、data/probe/message_0.db、data/probe/session.db 及 data/test-source 中失败测试残留，自动审批两次拒绝删除，原因是 `blocked by policy`。未继续绕过；文件仍在项目 data/ 内并被 Git 忽略。可由用户手工清理；统计证据与运行归档保留。

语音本地模型已下载，实际转写正在修复解码依赖调用，不标记完成。
