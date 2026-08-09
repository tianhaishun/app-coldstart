@echo off
setlocal enabledelayedexpansion
title App Cold Start Profiler

set "ROOT=%~dp0"
set "VENV=%ROOT%.venv"
set "PY=%VENV%\Scripts\python.exe"
set "PORT=8766"

echo ============================================
echo   App Cold Start Profiler
echo ============================================
echo.

REM -- Check Python --
where python >nul 2>nul
if errorlevel 1 (
    echo [FAIL] Python not found in PATH.
    echo        Please install Python 3.10+ from https://www.python.org/downloads/
    echo        and check "Add Python to PATH" during install.
    pause
    exit /b 1
)

REM -- First run: create venv --
if not exist "%PY%" (
    echo [SETUP] First launch - creating virtual environment...
    python -m venv "%VENV%"
    if errorlevel 1 (
        echo [FAIL] Failed to create venv
        pause
        exit /b 1
    )
    echo [SETUP] Installing dependencies, please wait 1-3 min...
    "%PY%" -m pip install --upgrade pip
    "%PY%" -m pip install -r "%ROOT%requirements.txt"
    if errorlevel 1 (
        echo [FAIL] Dependency install failed. Try manually:
        echo        "%PY%" -m pip install -r "%ROOT%requirements.txt"
        pause
        exit /b 1
    )
    echo [SETUP] Done.
    echo.
)

REM -- Unify adb server before backend --
REM 机器上可能有多套 adb（内置 与 PATH 的 platform-tools）共用同一 daemon(5037)，
REM 版本分支不一会在握手时偶发 connection reset → 检测不到设备。
REM 这里用内置 adb 强制重启 server，让 5037 常驻一个统一版本（与后端 ADB_EXE 优先内置一致）。
if exist "%ROOT%adb\adb.exe" (
    "%ROOT%adb\adb.exe" kill-server >nul 2>nul
    "%ROOT%adb\adb.exe" start-server >nul 2>nul
)

REM -- Start backend --
REM HOST=0.0.0.0 → 允许局域网其他人访问（http://本机IP:8766）
REM 要切回仅本机访问（更安全）：把下面的 0.0.0.0 改成 127.0.0.1
set "HOST=0.0.0.0"
echo [RUN] Starting backend on http://%HOST%:%PORT% ...
echo       本机访问:  http://127.0.0.1:%PORT%/
echo       局域网访问（同网段其他人用）:
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /C:"IPv4"') do echo         http://%%a:%PORT%/
echo       Browser will open automatically. Keep this window open.
echo.
start "App Cold Start Profiler - Backend (DO NOT CLOSE)" "%PY%" -m uvicorn server:app --host %HOST% --port %PORT% --app-dir "%ROOT%"

REM -- Wait for service then open browser --
echo [WAIT] Waiting for service...
set /a TRIES=0
:WAIT_LOOP
set /a TRIES+=1
powershell -NoProfile -Command "try { (Invoke-WebRequest -Uri 'http://127.0.0.1:%PORT%/api/health' -UseBasicParsing -TimeoutSec 1).StatusCode } catch { exit 1 }" >nul 2>nul
if errorlevel 1 (
    if !TRIES! LSS 30 (
        timeout /t 1 /nobreak >nul
        goto WAIT_LOOP
    )
    echo [FAIL] Service not ready in 30s. Check the backend window.
    echo        Hint: port %PORT% may be reserved by Hyper-V.
    echo        Run as admin: netsh interface ipv4 show excludedportrange protocol=tcp
    pause
    exit /b 1
)

echo [OK] Service ready, opening browser...
start "" http://127.0.0.1:%PORT%/

echo.
echo  [OK] Launched. Close the backend window to stop.
echo.
timeout /t 5 /nobreak >nul
