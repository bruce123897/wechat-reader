当前定制包：[下载 WechatReader.zip](packages/WechatReader.zip)。维护者 Bruce，微信 bruce_jaychou。仓库保持私有。

# WeChat for AI

**让 AI Agent 直接读电脑上的微信聊天记录。**

维护者：Bruce ｜ 微信：bruce_jaychou ｜ 版本 2.3.0

装不上、用不了、想要新功能，或者想聊聊怎么让 AI 帮你处理微信和生意，都可以加我。转发请保留作者信息。

在本机离线读取你自己的微信电脑版聊天记录，支持文字、语音（自动转文字）、图片、视频、文件。数据全程在你自己电脑上，不联网、不上传。

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

## 如果网络不好

安装时如果下载卡住，加代理参数：
```
powershell -ExecutionPolicy Bypass -File install.ps1 -Proxy http://127.0.0.1:7890
```

## 作为 AI 技能使用

把本文件夹放到 `~/.claude/skills/wechat-reader/`，Claude Code 会自动加载。AI 可以直接用 `wx` 命令读取和搜索你的聊天记录。
