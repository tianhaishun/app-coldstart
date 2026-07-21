@echo off
REM 安装 pre-commit hook 到 .git\hooks\（cmd / 双击版）
REM 用法：双击 或 hooks\install.bat
REM
REM 策略：拷贝（不用软链）。Windows 软链需管理员权限，拷贝零依赖、最稳。
REM 纯 ASCII（对齐 AGENTS.md 3.4：cmd 用 GBK 解析，中文易乱码）。

setlocal enabledelayedexpansion
set "ROOT=%~dp0.."
set "SRC=%ROOT%\hooks\pre-commit"
set "DST=%ROOT%\.git\hooks\pre-commit"

if not exist "%SRC%" (
    echo [FAIL] Not found: %SRC%
    exit /b 1
)

if not exist "%ROOT%\.git\hooks" mkdir "%ROOT%\.git\hooks"

REM Backup existing non-sample hook
if exist "%DST%" (
    findstr /C:"pre-commit 钩子（仓库内版本" "%DST%" >nul 2>nul
    if errorlevel 1 (
        copy /Y "%DST%" "%DST%.bak" >nul
        echo [INFO] Backed up old hook to %DST%.bak
    )
)

copy /Y "%SRC%" "%DST%" >nul
if errorlevel 1 (
    echo [FAIL] Copy failed
    exit /b 1
)

echo [OK] pre-commit hook installed to %DST%
echo       Re-run hooks\install.bat after editing hooks\pre-commit
endlocal
