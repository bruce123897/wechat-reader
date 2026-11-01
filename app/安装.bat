@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo 正在安装 WeChat for AI，请稍候，不要关闭本窗口……
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
echo.
echo ============================================================
echo  安装脚本已结束（结果在上面）。本窗口不会自动关闭。
echo  若有红色报错，请把整个窗口截图发开发者 Jayden（小林）
echo  微信 LinlinlinJiaQ
echo ============================================================
pause
