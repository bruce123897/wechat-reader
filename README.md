当前定制包：[下载 WechatReader.zip](packages/WechatReader.zip)，版本 2.3.1，内含完整源码与隔离测试。维护者 Bruce，微信 bruce_jaychou。仓库已公开，可直接浏览、下载或复制链接给本地 AI 工具。

# WeChat for AI

**让 AI Agent 直接读电脑上的微信聊天记录。**

可接入 DeepSeek Harness（DSH）、Qwen Code、Kimi Code、Codex、Claude Code、Cursor 等本地 AI 工具。[复制安装提示词](#复制链接让-ai-帮你安装) · [查看接入说明](references/ai-clients.md)

维护者：Bruce ｜ 微信：bruce_jaychou ｜ 版本 2.3.1

装不上、用不了、想要新功能，或者想聊聊怎么让 AI 帮你处理微信和生意，都可以加我。转发请保留作者信息。

在本机离线读取你自己的微信电脑版聊天记录，支持文字、语音（自动转文字）、图片、视频、文件。微信归档与离线语音处理在本机进行；首次安装会下载依赖和模型，朋友圈媒体缓存也需要联网。让云端 AI 分析聊天片段时，片段会按所用 AI 工具的配置发送给模型服务商。

## 安装（一次性，约 5 分钟）

**前提**：电脑版微信 4.x 已安装并登录过。

1. 解压到任意目录
2. **双击 `安装.bat`**（推荐）。它会自动完成所有步骤，中途不要关窗口；装完窗口会停住给你看结果，不会一闪而过。

   > 不想用 .bat 也可以打开 PowerShell 输入：
   > ```
   > powershell -ExecutionPolicy Bypass -File install.ps1
   > ```

3. 到「密钥」这一步，脚本会自动重启一次微信来获取密钥：已经登录过的会自动重新登录、无需你操作；全新设备请在弹出的微信窗口里扫码登录。
4. 看到「✅ WeChat for AI 安装完成」就好了。语音模型那一步就算没下成功也不影响文字和图片，可以以后再装。

装好后 `wx` 命令在任何终端里都能用。

## 常用命令

```
wx new              最近的新消息
wx read 张三        读某人的聊天记录
wx find 关键词      搜索聊天记录
wx doctor           自检（看是否一切正常）
```

## 聊天图片打不开时

Windows 上保持微信登录目标账号，并在电脑版微信打开一张聊天图片，然后运行：

```
wx key --image
```

优先复用或导入已有图片密钥，没有时自动读取微信进程并用本机 V2 图片验证，验证通过后加密保存。该命令不重启微信，默认扫描时限 60 秒；需要更长时间时用 `wx key --image --timeout 120`。已有密钥失效时用 `wx key --image --force`，获取失败会保留原缓存并返回失败状态。

此功能恢复了在用版本的自动获取路径，并补充了超时、完整样本检查和失败状态处理。当前验证使用隔离生成的图片和模拟进程读取；未对真实微信进程做新一轮实测。

## 如果网络不好

安装时如果下载卡住，加代理参数：
```
powershell -ExecutionPolicy Bypass -File install.ps1 -Proxy http://127.0.0.1:7890
```

## 作为 AI 技能使用

Wechat Reader 不限定模型或品牌。**DeepSeek Harness（DSH）、Qwen Code、Kimi Code、Codex、Claude Code、Cursor、Gemini CLI、GitHub Copilot** 等本地 Agent，都可以通过 Skill 说明和 `wx` 命令接入。需要当前客户端能够读取本地文件，并在装有微信的这台 Windows 电脑上运行终端命令。

### 复制链接，让 AI 帮你安装

把下面整段复制到你正在使用的本地 AI 工具：

```text
请帮我安装并装备 wechat-reader 技能：
https://github.com/bruce123897/wechat-reader

先阅读仓库 README.md、SKILL.md 和 references/ai-clients.md，识别我当前使用的 AI 客户端与实际 Skill 目录。
先检查本机是否已有可用的 wx；如果已有，就复用它，不重复安装运行环境。
如果没有，按 README 在固定目录安装运行程序；需要微信扫码登录时让我操作，需要重启微信时先告知我。
将仓库最新版 SKILL.md 和整个 references 文件夹一起安装到当前客户端的 wechat-reader 技能目录；已有同名技能时先比较并保留备份。
安装后刷新技能列表或重新打开会话，只验证 wx --help 和 wx doctor，告诉我安装目录、是否已发现技能、命令是否可用及剩余问题。
如果网络或客户端限制导致无法访问仓库，请明确说明原因，并改用我提供的本地解压文件夹，不要声称安装成功。
本次安装验收不读取或导出聊天正文；后续按我的查询请求使用该技能。
```

**仓库已公开，可直接复制链接给本地 AI 工具。** 若网络受限，可下载项目后让 AI 读取本地解压文件夹。普通网页聊天窗口只收到链接，并不等于已经获得本机终端和微信数据访问能力。

### 常见工具的 Skill 目录

下表是个人级默认目录；`~` 表示用户主目录。使用自定义配置目录时，以该客户端的实际设置为准。

| AI 工具 | 个人级技能目录 |
|---|---|
| DeepSeek Harness（DSH） | `~/.dsh/skills/wechat-reader/`（设置了 `DSH_HOME` 时使用 `DSH_HOME/skills/wechat-reader/`） |
| Qwen Code（通义千问） | `~/.qwen/skills/wechat-reader/` |
| Kimi Code CLI | `~/.kimi-code/skills/wechat-reader/`（设置了 `KIMI_CODE_HOME` 时随配置移动） |
| Codex | `~/.agents/skills/wechat-reader/` |
| Claude Code | `~/.claude/skills/wechat-reader/` |
| Cursor | `~/.cursor/skills/wechat-reader/` |
| Gemini CLI | `~/.gemini/skills/wechat-reader/` |
| GitHub Copilot 本地 Agent | `~/.copilot/skills/wechat-reader/` |

先按上文装好 `wx` 运行程序，再将 **`SKILL.md` 和整个 `references/` 文件夹** 一起放进对应技能目录，保持相对路径不变。复制技能说明不会自动安装 Python 依赖、注册 `wx` 或完成微信接入。

TRAE、CodeBuddy，以及使用 DeepSeek、Qwen、Kimi、GLM 等模型的其他本地 Agent，也可在具备本地文件与终端能力时，让 AI 显式读取 `SKILL.md` 并调用 `wx`；原生 Skill 的安装入口请按当前工具文档选择，不能只凭模型名判断安装目录。

[查看详细接入说明、验证步骤和官方资料](references/ai-clients.md)。目录依据官方文档核对，尚未逐一进行各客户端的真实安装验收。
