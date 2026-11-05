# AI 工具接入指南

Wechat Reader 使用标准 `SKILL.md` 描述工作方式，实际读取由本机 `wx` 命令完成。AI 客户端需要能读取文件并运行本地终端；读取功能针对 Windows 电脑版微信 4.x。

## 复制链接安装

[README 中的安装提示词](../README.md#复制链接让-ai-帮你安装)可以直接复制给正在使用的 AI。仓库地址：

```text
https://github.com/bruce123897/wechat-reader
```

仓库已公开，AI 可直接读取链接中的安装说明。网络或客户端访问受限时，请下载项目并提供本地完整解压目录。把链接发给普通网页聊天窗口不会自动提供本机运行环境。

## 先准备运行程序

1. 固定存放完整项目文件夹，并阅读 [README](../README.md) 的安装步骤。若已有可用的 `wx`，复用现有安装。
2. 未安装时，按 README 执行 `安装.bat` 或 `install.ps1`。安装可能下载依赖/模型、重启微信获取密钥、进行首次同步并注册后台任务；扫码登录由本人完成。
3. 在 AI 实际使用的终端里确认 `wx --help` 能返回帮助。安装了 Skill 不代表已经安装运行程序。

## 安装 Skill 文件

把仓库最新版的 `SKILL.md` 与整个 `references/` 一起复制到所选目录：

```text
wechat-reader/
├── SKILL.md
└── references/
    ├── ai-clients.md
    ├── commands.md
    ├── internals.md
    └── troubleshooting.md
```

保持目录相对关系，其他已有参考文件一并保留。复制时不需要带入私人数据库、密钥、聊天导出或运行日志。

| 工具 | 默认个人级目录 | 项目级目录 |
|---|---|---|
| DeepSeek Harness（DSH） | `~/.dsh/skills/wechat-reader/` | `.dsh/skills/wechat-reader/` |
| Qwen Code | `~/.qwen/skills/wechat-reader/` | `.qwen/skills/wechat-reader/` |
| Kimi Code CLI | `~/.kimi-code/skills/wechat-reader/` | `.kimi-code/skills/wechat-reader/` |
| Codex | `~/.agents/skills/wechat-reader/` | `.agents/skills/wechat-reader/` |
| Claude Code | `~/.claude/skills/wechat-reader/` | `.claude/skills/wechat-reader/` |
| Cursor | `~/.cursor/skills/wechat-reader/` | `.cursor/skills/wechat-reader/` |
| Gemini CLI | `~/.gemini/skills/wechat-reader/` | `.gemini/skills/wechat-reader/` |
| GitHub Copilot 本地 Agent | `~/.copilot/skills/wechat-reader/` | `.github/skills/wechat-reader/` |

`~` 为用户主目录，Windows 上通常对应 `%USERPROFILE%`。DSH 的 `DSH_HOME`、Kimi Code 的 `KIMI_CODE_HOME` 或其他客户端的配置可改变默认位置，安装时应读取实际配置。

DSH、Qwen Code、Kimi Code、Codex、Cursor、Gemini CLI 和 Copilot 也支持共享的 `.agents/skills` 路径；DSH 的用户共享目录可由 `DSH_AGENTS_HOME` 改写。同一技能已有多个副本时，先检查发现结果与实际加载路径，避免旧副本覆盖新说明。

## 刷新与验收

复制后使用客户端的刷新入口或重新开启会话。Gemini CLI 可用 `/skills reload`，Kimi Code CLI 可用 `/skill:wechat-reader` 手动调用；其他工具先在技能列表中确认名称。

只做以下安装验收，不默认读取聊天正文：

```text
请加载 wechat-reader 技能，先运行 wx --help 和 wx doctor，
报告命令能否运行、是否发现技能、微信接入及后台状态是否正常，
并列出失败原因和下一步；不要把只看到 SKILL.md 当作接入成功。
```

`wx doctor` 失败时按实际输出处理。若 `wx` 不存在但程序已安装，先检查当前终端 PATH 或使用已定位的入口，不重复创建安装。远程 SSH、云端 Agent 或容器只看到 Skill 文件时，也不会因此获得用户电脑上的微信数据。

## 其他国产工具和自定义 Agent

TRAE、腾讯 CodeBuddy，以及接入 DeepSeek、通义千问、Kimi、智谱 GLM 等模型的自定义 Agent，可以按各自当前文档的 Skill 导入入口安装本文件。没有确认原生 Skill 目录、但具备本地工具能力时，可直接对 AI 说：

```text
请读取我提供的 wechat-reader 文件夹中的 SKILL.md。
之后按该说明调用本机 wx 命令，先运行 wx --help 和 wx doctor。
```

这属于显式加载指令文件；是否支持自动发现、斜杠命令和长期启用，取决于客户端，不能由模型品牌推定。当前项目提供 CLI + Skill 接入，没有因此新增 MCP 服务。

## 数据处理

微信归档和离线语音处理在本机进行。依赖/模型下载及朋友圈媒体缓存会联网；把聊天片段交给云端模型分析时，片段会按客户端配置发送给模型服务商。使用本地模型还是云端模型由所用 AI 工具决定。

## 依据与验证范围

以下为 2026-11-05 核对的官方文档/源码。路径兼容说明已核对，尚未逐一在这些客户端中完成真实安装和微信接入验收。

- [DeepSeek Harness Skills](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/skills.md)；[默认主目录源码](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/skill/skill-filesystem/src/index.ts)。
- [Qwen Code Skills](https://github.com/QwenLM/qwen-code/blob/main/docs/users/features/skills.md)。
- [Kimi Code Skills](https://www.kimi.com/code/docs/en/kimi-code-cli/customization/skills.html)。
- [Codex Skills](https://learn.chatgpt.com/docs/build-skills)。
- [Claude Code Skills](https://code.claude.com/docs/en/skills)。
- [Cursor Skills](https://cursor.com/docs/skills)。
- [Gemini CLI Skills](https://geminicli.com/docs/cli/skills/)。
- [GitHub Copilot Skills](https://docs.github.com/en/copilot/concepts/customizations/agent-skills)。
