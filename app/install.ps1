# WeChat for AI 一键安装（Windows）。维护者 Bruce，微信 bruce_jaychou。
# 推荐：双击本文件夹里的「安装.bat」（出错也不会闪退，窗口会停住给你看结果）。
# 或在 PowerShell 里：powershell -ExecutionPolicy Bypass -File install.ps1
# 做的事：建独立 Python 环境 → 装依赖 →（可选）语音模型 → 装 wx 命令 → 取密钥（扫码/登录捕获）→ 首次同步 → 开机自启 → 自检
# 前提：电脑版微信 4.x 已安装。全程只读微信数据，不改微信、不删数据、不上传。
param(
    [string]$Proxy = "",          # 下载代理，如 http://127.0.0.1:7890；不填会自动探测本机 7890
    [switch]$NoAutostart,         # 不设开机自启
    [switch]$NoShim,              # 不装 wx 命令（测试用）
    [switch]$NoModel,             # 不下载语音模型
    [switch]$NoKey,               # 跳过取密钥（测试用）
    [switch]$Pause                # 结束/出错后暂停，窗口不自动关（给直接右键运行本文件的人用）
)
# 用 Continue 而不是 Stop：否则 pip/curl 等外部命令往 stderr 写一行（哪怕最后成功）就会被当成致命错误、直接崩掉。
# 真正该中止的地方，下面都用显式 throw，交给 trap 统一兜底。
$ErrorActionPreference = "Continue"
$Contact = "bruce_jaychou"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Here) { $Here = (Get-Location).Path }
$Data = if ($env:WXKIT_HOME) { $env:WXKIT_HOME } else { Join-Path (Split-Path -Parent $Here) "data\customized" }
$Venv = Join-Path $Data "venv"
$Py = Join-Path $Venv "Scripts\python.exe"
# 语音模型（GitHub release）；下载时按顺序试这些国内加速前缀，最后才直连 GitHub
$ModelRel = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2"
$GhMirrors = @("https://gh.llkk.cc/", "https://gh-proxy.com/", "")
# pip 源：阿里云优先，清华次之，最后官方源
$PipIndexes = @("https://mirrors.aliyun.com/pypi/simple/", "https://pypi.tuna.tsinghua.edu.cn/simple/", "")

function Step($m) { Write-Host "`n== $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host $m -ForegroundColor Yellow }
function Ok($m)   { Write-Host $m -ForegroundColor Green }
function Hold { if ($Pause) { Write-Host ""; Read-Host "按回车键关闭本窗口" | Out-Null } }

# 顶层兜底：任何没接住的错误都在这里打印清楚，而不是让窗口一闪而过
trap {
    Write-Host "`n安装过程中出错：$_" -ForegroundColor Red
    Write-Host "把这个窗口整个截图发给维护者 Bruce，微信 $Contact，一般都能很快解决。" -ForegroundColor Yellow
    Hold
    exit 1
}

Write-Host "`nWeChat for AI —— 让 AI 直接读电脑上的微信聊天记录" -ForegroundColor Green
Write-Host "维护者 Bruce ｜ 微信 $Contact"
New-Item -ItemType Directory -Force $Data | Out-Null

# 根因修复（v2.3.0）：绝不从临时目录注册后台/命令，否则它们会指向随时被系统清理的 Temp 路径。
# 一旦检测到自己在临时目录运行，先把整包复制到固定目录 $Data\app，再从那里继续安装与注册。
$tempRoots = @($env:TEMP, $env:TMP, (Join-Path $env:LOCALAPPDATA 'Temp'), (Join-Path $HOME 'AppData\Local\Temp')) | Where-Object { $_ }
$inTemp = $false
foreach ($tr in $tempRoots) { try { if ($Here.ToLower().StartsWith($tr.ToLower())) { $inTemp = $true } } catch {} }
if ($inTemp) {
    $AppDir = Join-Path $Data "app"
    Warn "检测到正在从临时目录运行（$Here）。为避免后台指向会被清理的 Temp，先复制到固定目录：$AppDir"
    New-Item -ItemType Directory -Force $AppDir | Out-Null
    Copy-Item (Join-Path $Here '*') -Destination $AppDir -Recurse -Force
    $Here = $AppDir
    Ok "已复制到固定目录，后续安装与开机自启都指向这里"
}

# 代理：没指定就看本机 7890 端口（常见的本地代理）开没开
if (-not $Proxy) {
    try { $c = New-Object Net.Sockets.TcpClient; $c.Connect("127.0.0.1", 7890); $c.Close(); $Proxy = "http://127.0.0.1:7890" } catch {}
}
if ($Proxy) { $env:HTTPS_PROXY = $Proxy; $env:HTTP_PROXY = $Proxy; Write-Host "检测到代理 $Proxy（直连 GitHub 时会用上）" }

# 预检：电脑版微信装了没
$wxFound = $false
try { if (Get-Process Weixin, WeChat -ErrorAction SilentlyContinue) { $wxFound = $true } } catch {}
foreach ($k in 'HKCU:\Software\Tencent\Weixin', 'HKCU:\Software\Tencent\WeChat') {
    try { if ((Get-ItemProperty $k -ErrorAction SilentlyContinue).InstallPath) { $wxFound = $true } } catch {}
}
if (-not $wxFound) { Warn "提示：没检测到电脑版微信。请先安装并登录一次电脑版微信 4.x，否则第 5 步取密钥会失败。" }

Step "1/7 准备 Python 环境"
if (-not (Test-Path $Py)) {
    $sys = $null
    foreach ($cand in @("python", "py")) {
        try { $v = & $cand -c "import sys;print(sys.version_info>=(3,10))" 2>$null; if ($v -eq "True") { $sys = $cand; break } } catch {}
    }
    if ($sys) { & $sys -m venv $Venv }
    else {
        Warn "没检测到 Python 3.10+，正在自动安装（不影响你系统里别的东西）…"
        # 优先 winget（Win10/11 自带，走微软 CDN，国内一般能连）
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            try {
                winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
                $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
                foreach ($cand in @("python", "py")) { try { $v = & $cand -c "import sys;print(sys.version_info>=(3,10))" 2>$null; if ($v -eq "True") { $sys = $cand; break } } catch {} }
            } catch { Warn "winget 装 Python 没成功，换别的方式…" }
        }
        if ($sys) { & $sys -m venv $Venv }
        else {
            # 退路：uv + 国内 python 镜像
            if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
                try { Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression; $env:Path = "$HOME\.local\bin;$env:Path" } catch {}
            }
            if (Get-Command uv -ErrorAction SilentlyContinue) {
                $env:UV_PYTHON_INSTALL_MIRROR = "https://mirrors.ustc.edu.cn/github-release/astral-sh/python-build-standalone/releases/download"
                uv venv --python 3.12 $Venv
            }
        }
    }
}
if (-not (Test-Path $Py)) { throw "Python 环境没建起来。最省事：去微软商店或 python.org 装一个 Python 3.12，再重新运行本脚本。" }

Step "2/7 安装依赖"
& $Py -m pip install --disable-pip-version-check -q --upgrade pip 2>$null
$req = Join-Path $Here "requirements.txt"
$okdep = $false
foreach ($idx in $PipIndexes) {
    $a = @("-m", "pip", "install", "--disable-pip-version-check", "-q", "--retries", "5", "--timeout", "60", "-r", $req)
    if ($idx) { $h = ([Uri]$idx).Host; $a += @("-i", $idx, "--trusted-host", $h) }
    & $Py @a
    if ($LASTEXITCODE -eq 0) { $okdep = $true; if ($idx) { Ok "依赖已用国内源 $idx 装好" } else { Ok "依赖装好" }; break }
    Warn "这个源装不全（网络波动），换下一个源重试…"
}
if (-not $okdep) { throw "依赖没装上（网络问题）。可以加 -Proxy http://127.0.0.1:7890 再运行一次。" }

Step "3/7 语音模型（可选，失败会自动跳过，不影响文字和图片）"
$Model = Join-Path $Data "models\sensevoice"
if ($NoModel) { Warn "按要求跳过（-NoModel）" }
elseif (Test-Path (Join-Path $Model "model.int8.onnx")) { Ok "已有" }
else {
    $got = ""
    try { & $Py -c "import sys;sys.path.insert(0,r'$Here');from wxkit import voice;print(voice.model_dir() or '')" | Set-Variable got } catch {}
    if ($got) { Ok "已从本机其他工具复制：$got" }
    else {
        $tmp = Join-Path $env:TEMP "sensevoice.tar.bz2"
        Remove-Item -Force $tmp -ErrorAction SilentlyContinue
        $dl = $false
        foreach ($m in $GhMirrors) {
            $url = "$m$ModelRel"
            Write-Host "下载语音模型（约 160MB）：$url"
            $a = @("-L", "--fail", "--connect-timeout", "15", "-o", $tmp, $url)
            if ($Proxy -and -not $m) { $a = @("-x", $Proxy) + $a }   # 只有在直连 GitHub 时才套代理
            try { & curl.exe @a } catch {}
            if ($LASTEXITCODE -eq 0 -and (Test-Path $tmp)) { $dl = $true; break }
            Warn "这个下载源不行，换下一个…"
        }
        if ($dl) {
            try {
                $x = Join-Path $env:TEMP "sensevoice_x"; Remove-Item -Recurse -Force $x -ErrorAction SilentlyContinue
                New-Item -ItemType Directory -Force $x | Out-Null
                tar -xjf $tmp -C $x
                New-Item -ItemType Directory -Force $Model | Out-Null
                Get-ChildItem -Recurse $x -Include "model.int8.onnx", "tokens.txt" | Copy-Item -Destination $Model
                Remove-Item -Recurse -Force $x, $tmp -ErrorAction SilentlyContinue
                if (Test-Path (Join-Path $Model "model.int8.onnx")) { Ok "语音模型装好" } else { Warn "模型文件不完整，先跳过（以后重跑脚本再装）" }
            } catch { Warn "模型解压失败，先跳过（不影响文字/图片；以后联网好点时重跑本脚本即可）" }
        }
        else { Warn "语音模型暂时没下下来——先跳过，不影响文字和图片。以后想要语音转文字，网络好点时重跑本脚本即可。" }
    }
}

Step "4/7 安装 wx 命令"
if ($NoShim) { Warn "跳过（-NoShim）" }
else {
    $Bin = Join-Path $HOME ".local\bin"; New-Item -ItemType Directory -Force $Bin | Out-Null
    $Entry = Join-Path $Here "wx.py"
    # 路径里的用户目录写成 %USERPROFILE%，其余中文靠 chcp 65001 + UTF-8 存盘；结束后恢复原代码页
    $cp = { param($p) if ($p.StartsWith($HOME, [StringComparison]::OrdinalIgnoreCase)) { "%USERPROFILE%" + $p.Substring($HOME.Length) } else { $p } }
    $cmd = "@echo off`r`nfor /f `"tokens=2 delims=:`" %%a in ('chcp') do set _wxcp=%%a`r`nchcp 65001 >nul`r`nset PYTHONIOENCODING=utf-8`r`n`"$(& $cp $Py)`" `"$(& $cp $Entry)`" %*`r`nset _wxrc=%errorlevel%`r`nchcp %_wxcp% >nul`r`nexit /b %_wxrc%`r`n"
    [IO.File]::WriteAllText((Join-Path $Bin "wx.cmd"), $cmd, (New-Object Text.UTF8Encoding $false))
    $sh = "#!/bin/sh`nexport PYTHONIOENCODING=utf-8`nexec `"$($Py -replace '\\','/')`" `"$($Entry -replace '\\','/')`" `"`$@`"`n"
    [IO.File]::WriteAllText((Join-Path $Bin "wx"), $sh, (New-Object Text.UTF8Encoding $false))
    $up = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($up -notlike "*$Bin*") { [Environment]::SetEnvironmentVariable("Path", "$Bin;$up", "User"); Write-Host "已把 $Bin 加入 PATH（新开的终端生效）" }
}
function Wx { & $Py (Join-Path $Here "wx.py") @args }
$env:PYTHONIOENCODING = "utf-8"

Step "5/7 密钥（通过微信登录捕获；微信已登录的会自动重启一次，自动重登，无需重新扫码）"
if ($NoKey) { Warn "跳过（-NoKey）" }
else {
    Wx key
    if ($LASTEXITCODE) {
        Warn "密钥这一步没自动完成。常见情况：微信还没登录。办法："
        Warn "  1) 确认电脑版微信已登录 → 重新运行本脚本（前面装好的会跳过）"
        Warn "  2) 已有该账号的数据库密钥时：wx key --set <64位十六进制>"
        Warn "  （没密钥不影响继续装；后台会每隔几分钟自动重试，登录后自己补上。）"
    }
    else { Ok "密钥已获取并验证" }
}

Step "6/7 首次同步（把聊天记录整理进本地档案库，约 1 分钟）"
Wx sync
if ($LASTEXITCODE) { Warn "首次同步没完成（多半是还没拿到密钥）；拿到密钥后后台会自动补同步。" }

Step "7/7 后台同步 + 自检"
if ($NoAutostart) { Warn "跳过开机自启" }
else { Wx daemon install; if ($LASTEXITCODE) { Warn "开机自启没装上，可稍后手动：wx daemon install" } }
Wx doctor

Write-Host "`n✅ WeChat for AI 安装完成" -ForegroundColor Green
Write-Host "试试：wx new   /   wx find 某人   /   wx read 某人；或者直接让 AI 帮你查微信"
Write-Host "`n维护者 Bruce ｜ 微信 $Contact" -ForegroundColor Cyan
Write-Host "装不上、用不了、想要新功能，或者想聊聊怎么让 AI 帮你处理微信和生意，都可以加我"
Hold
