@echo off
setlocal
title App Cold Start Profiler - 一键启动（客户端）
chcp 65001 >nul
set "ROOT=%~dp0"

echo ============================================
echo   App Cold Start Profiler - 客户端一键启动
echo ============================================
echo.
echo   自动校验/补齐 Node.js、Python、adb、scrcpy 与 npm/Python 依赖，
echo   然后启动 Electron 桌面客户端（主要使用方式）。
echo.
echo   仅首次运行需要联网下载缺失依赖，之后秒开。
echo   浏览器模式（备用，局域网访问）请用 Start-Web.bat。
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\start-windows.ps1"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo [FAIL] 启动失败（错误码 %RC%）。请把上方输出截图发给维护者。
    pause
    exit /b 1
)
exit /b 0
