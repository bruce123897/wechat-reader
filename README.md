# ChatArchive

本地聊天归档与 AI 检索工具。借鉴 Wechat Reader 的功能与数据流，独立设计并实现。

当前处于开发起点，尚未接入真实微信数据。开发演示使用合成消息，不代表真实聊天或实际巡检结果。

目标流程：数据源 → 统一消息模型 → 增量归档 → 检索与上下文 → 网页 / CLI / MCP → 带原文引用的回答。

计划技术栈：Python、SQLite、FastAPI、React、TypeScript、MCP。按里程碑逐步引入，先交付可验证的数据核心。

项目路线见 [ROADMAP](docs/ROADMAP.md)，技术取舍见 [DECISIONS](docs/DECISIONS.md)，参考来源见 [NOTICE](NOTICE.md)。

代码、文档、修复通过真实 Git 提交持续保存。私人聊天、数据库、密钥和运行日志不纳入仓库。

