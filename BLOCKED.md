# 阻断与明确边界

1. 第二个发现的账号没有可用的本机数据库密钥。已尝试其现有 DPAPI 文件的只读解锁与 db 字段验证，明确失败；没有读取它的旧档案库冒充接入。下一步是为该账号提供有效、本人授权的密钥文件，再 configure/probe。当前仅已解锁账号完成真实接入。
2. WXGF 图像容器尚未完成可靠转换，返回 unsupported；普通图片/视频/文件及文字功能继续运行。后续验证成熟解码器，不能把容器字节冒充 JPEG。
3. 本次临时明文探针 data/probe/contact0.db、data/probe/message_0.db、data/probe/session.db 及 data/test-source 中失败测试残留，自动审批两次拒绝删除，原因是 `blocked by policy`。未继续绕过；文件仍在项目 data/ 内并被 Git 忽略。可由用户手工清理；统计证据与运行归档保留。
4. 写入范围执行偏差：首次 `.venv` pip 安装 cryptography/zstandard 时，未加 --no-cache-dir 且未设置 TEMP/TMP，因此默认用户 pip 缓存/临时目录可能发生范围外写入。包安装目标为项目虚拟环境；不能证明本轮所有写入均限项目。后续安装已使用 --no-cache-dir 和项目 data/tmp；未自行清理范围外缓存，未改动另一项目或共用文档。

语音曾因 pilk 参数与 PyAV19 API 不兼容失败，现已修复并锁定验证版本；真实源提取→SILK解码→本地转写通过。tiny模型准确度尚未人工评测；这是质量边界，不是运行阻断。
