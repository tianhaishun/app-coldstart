@echo off
setlocal enabledelayedexpansion
title App Cold Start Profiler (Client)

set "ROOT=%~dp0"
set "NPM_INSTALLED="

echo ============================================
echo   App Cold Start Profiler - One-click Start
echo   Launches the desktop client (Electron).
echo ============================================
echo.

REM -- 0. Check Node.js / npm (required by the Electron client) --
where node >nul 2>nul
if errorlevel 1 (
    echo [FAIL] Node.js not found in PATH.
    echo        Install Node.js LTS from https://nodejs.org/  (or: winget install OpenJS.NodeJS.LTS)
    echo        then re-run this script.
    pause
    exit /b 1
)
where npm >nul 2>nul
if errorlevel 1 (
    echo [FAIL] npm not found in PATH. Please reinstall Node.js (npm ships with it).
    pause
    exit /b 1
)
for /f "delims=" %%v in ('node --version') do echo [OK] Node.js %%v

REM -- 1. Platform dependencies (per-environment, only when missing) --

REM 1a. adb (essential): download official Android platform-tools if adb\adb.exe missing
if not exist "%ROOT%adb\adb.exe" (
    echo.
    echo [DEPS] adb not found - downloading Android platform-tools ^(official^)...
    set "ZIP=%TEMP%\_cst_platform-tools.zip"
    set "EXTRACT=%TEMP%\_cst_platform-tools_dir"
    curl.exe -L --fail --silent --show-error -o "!ZIP!" https://dl.google.com/android/repository/platform-tools-latest-windows.zip
    if errorlevel 1 (
        echo [FAIL] Download failed. Check network, then manually place adb.exe
        echo        + AdbWinApi.dll + AdbWinUsbApi.dll into "%ROOT%adb\"
        echo        ^(from Android SDK platform-tools^).
        pause
        exit /b 1
    )
    if exist "!EXTRACT!" rmdir /s /q "!EXTRACT!"
    powershell -NoProfile -Command "Expand-Archive -Force '!ZIP!' '!EXTRACT!'"
    if errorlevel 1 (
        echo [FAIL] Extract failed. Manually place platform-tools into "%ROOT%adb\".
        pause
        exit /b 1
    )
    if not exist "%ROOT%adb" mkdir "%ROOT%adb"
    copy /y "!EXTRACT!\platform-tools\adb.exe" "%ROOT%adb\adb.exe" >nul
    copy /y "!EXTRACT!\platform-tools\AdbWinApi.dll" "%ROOT%adb\AdbWinApi.dll" >nul
    copy /y "!EXTRACT!\platform-tools\AdbWinUsbApi.dll" "%ROOT%adb\AdbWinUsbApi.dll" >nul
    copy /y "!EXTRACT!\platform-tools\source.properties" "%ROOT%adb\source.properties" >nul 2>nul
    del /q "!ZIP!" 2>nul
    rmdir /s /q "!EXTRACT!" 2>nul
    if not exist "%ROOT%adb\adb.exe" (
        echo [FAIL] adb.exe still missing after download - please place it manually.
        pause
        exit /b 1
    )
    echo [OK] adb ready.
)

REM 1b. scrcpy (optional: mirror/record feature): download upstream release if scrcpy\scrcpy.exe missing
if not exist "%ROOT%scrcpy\scrcpy.exe" (
    echo.
    echo [DEPS] scrcpy not found - downloading scrcpy v3.3 ^(upstream, for mirror/record^)...
    set "ZIP=%TEMP%\_cst_scrcpy.zip"
    set "EXTRACT=%TEMP%\_cst_scrcpy_dir"
    curl.exe -L --fail --silent --show-error -o "!ZIP!" https://github.com/Genymobile/scrcpy/releases/download/v3.3/scrcpy-win64-v3.3.zip
    if errorlevel 1 (
        echo [WARN] scrcpy download failed - mirror/record will be hidden.
        echo        You can retry later: put scrcpy.exe + scrcpy-server + DLLs into "%ROOT%scrcpy\"
    ) else (
        if exist "!EXTRACT!" rmdir /s /q "!EXTRACT!"
        powershell -NoProfile -Command "Expand-Archive -Force '!ZIP!' '!EXTRACT!'"
        if not exist "%ROOT%scrcpy" mkdir "%ROOT%scrcpy"
        copy /y "!EXTRACT!\scrcpy-win64-v3.3\*" "%ROOT%scrcpy\" >nul 2>nul
        del /q "!ZIP!" 2>nul
        rmdir /s /q "!EXTRACT!" 2>nul
        if exist "%ROOT%scrcpy\scrcpy.exe" (
            echo [OK] scrcpy ready.
        ) else (
            echo [WARN] scrcpy extract failed - mirror/record will be hidden.
        )
    )
)

REM 1c. iOS toolchain (optional): not auto-downloaded (internal mirror source);
REM     if missing, print how to obtain it. Android testing is unaffected.
if not exist "%ROOT%ios\idevice_id.exe" (
    echo.
    echo [DEPS] iOS toolchain not found - iOS device testing will be disabled.
    echo        To enable: copy idevice_id.exe + its DLLs from XYLog or
    echo        mobiledevicewin/libimobiledevice-win32 ^(upstream release^) into "%ROOT%ios\"
    echo        ^(folder is gitignored; not required for Android tests^)
)

REM -- 2. npm dependencies (Electron binary, ~100MB, first run only) --
if not exist "%ROOT%node_modules" (
    echo.
    echo [SETUP] Installing npm dependencies ^(Electron, first run, 1-3 min^)...
    pushd "%ROOT%"
    call npm install --no-audit --no-fund
    set "NPM_EC=!errorlevel!"
    popd
    if not "!NPM_EC!"=="0" (
        echo [FAIL] npm install failed. Run manually:  cd /d "%ROOT%" ^&^& npm install
        pause
        exit /b 1
    )
)

REM -- 3. Launch the desktop client --
echo.
echo [RUN] Starting App Cold Start Profiler client...
echo       First launch creates the Python venv and installs dependencies
echo       ^(progress shown in the client splash window^). Keep this window open.
echo.
pushd "%ROOT%"
call npm start
set "EXITCODE=!errorlevel!"
popd
echo.
echo [EXIT] Client closed ^(code !EXITCODE!^). You can close this window.
timeout /t 5 /nobreak >nul
