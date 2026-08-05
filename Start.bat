@echo off
setlocal
title App Cold Start Profiler (Client)

REM Windows client entry point. All dependency detection/installation lives in
REM scripts\start-windows.ps1 so the .bat remains a small double-click wrapper.
set "ROOT=%~dp0"
echo ============================================
echo   App Cold Start Profiler - One-click Start
echo   Launches the Electron desktop client.
echo ============================================
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\start-windows.ps1" -Mode Client
set "EXITCODE=%ERRORLEVEL%"
if not "%EXITCODE%"=="0" (
    echo.
    echo [FAIL] Startup failed with exit code %EXITCODE%.
    echo        Review the error above and run Start.bat again after fixing it.
    pause
    exit /b %EXITCODE%
)
exit /b 0
