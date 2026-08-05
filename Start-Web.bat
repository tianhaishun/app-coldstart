@echo off
setlocal
title App Cold Start Profiler (Web Backup)

REM Browser mode is a backup entry point. It uses the same dependency bootstrap
REM as the client, but does not install/use Node.js, Electron, or scrcpy.
set "ROOT=%~dp0"
echo ============================================
echo   App Cold Start Profiler - Browser Backup
echo ============================================
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\start-windows.ps1" -Mode Web
set "EXITCODE=%ERRORLEVEL%"
if not "%EXITCODE%"=="0" (
    echo.
    echo [FAIL] Browser startup failed with exit code %EXITCODE%.
    pause
    exit /b %EXITCODE%
)
exit /b 0
