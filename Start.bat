@echo off
setlocal
title App Cold Start Profiler - One-Click Start (Client)

REM NOTE: keep this file pure ASCII (AGENTS.md 3.4).
REM cmd parses .bat files in the system OEM codepage (GBK on zh-CN);
REM UTF-8 Chinese text gets garbled and can even break parsing.
REM Chinese documentation lives in README.md / the HTML UI instead.

set "ROOT=%~dp0"

echo ============================================
echo   App Cold Start Profiler - One-Click Start
echo ============================================
echo.
echo   Checks and installs Node.js, Python, adb, scrcpy and
echo   npm/Python dependencies (only what is missing), then
echo   starts the Electron desktop client.
echo.
echo   First run needs network access to download dependencies.
echo   Browser mode (backup, LAN access): run Start-Web.bat
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\start-windows.ps1"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo [FAIL] Startup failed, exit code %RC%. Send a screenshot
    echo        of the output above to the maintainer.
    pause
    exit /b 1
)
exit /b 0
