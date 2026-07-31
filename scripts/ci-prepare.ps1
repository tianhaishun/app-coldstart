# ══════════════════════════════════════════════════════════════════════════════
# GitLab Runner 一次性初始化脚本
#
# 在 Windows Runner 机器上运行一次，准备 CI/CD 构建环境。
# 需要管理员权限。
#
# 用法：右键 → 以管理员身份运行 PowerShell → 执行此脚本
#   .\scripts\ci-prepare.ps1
# ══════════════════════════════════════════════════════════════════════════════

param(
    [string]$DepsDir = "C:\ci-deps\app-coldstart"
)

$ErrorActionPreference = "Stop"

function Write-Step($msg) {
    Write-Host "`n[*] $msg" -ForegroundColor Cyan
}

function Test-Command($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

# ── 1. 检查 Node.js ──────────────────────────────────────────────────────────
Write-Step "Checking Node.js..."
if (Test-Command "node") {
    $nodeVer = node --version
    Write-Host "  Node.js found: $nodeVer" -ForegroundColor Green
} else {
    Write-Host "  Node.js NOT found!" -ForegroundColor Yellow
    Write-Host "  Please install Node.js 18+ from https://nodejs.org/" -ForegroundColor Yellow
    Write-Host "  Or run: winget install OpenJS.NodeJS.LTS" -ForegroundColor Yellow
}

# ── 2. 检查 Python ───────────────────────────────────────────────────────────
Write-Step "Checking Python..."
if (Test-Command "python") {
    $pyVer = python --version 2>&1
    Write-Host "  Python found: $pyVer" -ForegroundColor Green
} else {
    Write-Host "  Python NOT found!" -ForegroundColor Yellow
    Write-Host "  Please install Python 3.11+ from https://www.python.org/downloads/" -ForegroundColor Yellow
    Write-Host "  Or run: winget install Python.Python.3.11" -ForegroundColor Yellow
}

# ── 3. 检查 Git ──────────────────────────────────────────────────────────────
Write-Step "Checking Git..."
if (Test-Command "git") {
    $gitVer = git --version
    Write-Host "  Git found: $gitVer" -ForegroundColor Green
} else {
    Write-Host "  Git NOT found!" -ForegroundColor Red
    Write-Host "  Please install Git from https://git-scm.com/" -ForegroundColor Red
}

# ── 4. 创建依赖目录 ──────────────────────────────────────────────────────────
Write-Step "Setting up deps directory: $DepsDir"
if (-not (Test-Path $DepsDir)) {
    New-Item -ItemType Directory -Path $DepsDir -Force | Out-Null
    Write-Host "  Created $DepsDir" -ForegroundColor Green
} else {
    Write-Host "  Already exists" -ForegroundColor Green
}

# ── 5. 检查/放置 scrcpy ──────────────────────────────────────────────────────
Write-Step "Checking scrcpy in deps dir..."
$scrcpyDir = Join-Path $DepsDir "scrcpy"
if (Test-Path "$scrcpyDir\scrcpy.exe") {
    Write-Host "  scrcpy already placed at $scrcpyDir" -ForegroundColor Green
} else {
    Write-Host "  scrcpy NOT found at $scrcpyDir" -ForegroundColor Yellow
    Write-Host "  Please download scrcpy win64 from:" -ForegroundColor Yellow
    Write-Host "    https://github.com/Genymobile/scrcpy/releases" -ForegroundColor Yellow
    Write-Host "  Extract to: $scrcpyDir" -ForegroundColor Yellow
    Write-Host "  (needs scrcpy.exe + scrcpy-server + DLLs)" -ForegroundColor Yellow
}

# ── 6. 检查/放置 ios 工具链 ──────────────────────────────────────────────────
Write-Step "Checking ios toolchain in deps dir..."
$iosDir = Join-Path $DepsDir "ios"
if (Test-Path "$iosDir\idevice_id.exe") {
    Write-Host "  ios toolchain already placed at $iosDir" -ForegroundColor Green
} else {
    Write-Host "  ios toolchain NOT found at $iosDir" -ForegroundColor Yellow
    Write-Host "  Copy from existing project ios/ directory to: $iosDir" -ForegroundColor Yellow
}

# ── 7. 预修复 winCodeSign 缓存 ──────────────────────────────────────────────
Write-Step "Pre-fixing winCodeSign cache..."
$csCache = "$env:LOCALAPPDATA\electron-builder\Cache\winCodeSign\winCodeSign-2.6.0"
if (Test-Path "$csCache\rcedit-x64.exe") {
    Write-Host "  winCodeSign cache OK" -ForegroundColor Green
} else {
    Write-Host "  winCodeSign cache missing - will be auto-downloaded on first build" -ForegroundColor Yellow
    Write-Host "  If build fails with symlink error, see scripts/build-python-embed.py fix" -ForegroundColor Yellow
}

# ── 8. 检查 GitLab Runner ────────────────────────────────────────────────────
Write-Step "Checking GitLab Runner..."
if (Test-Command "gitlab-runner") {
    $runnerVer = gitlab-runner --version 2>&1 | Select-Object -First 1
    Write-Host "  GitLab Runner found: $runnerVer" -ForegroundColor Green

    Write-Host "`n  Registered runners:" -ForegroundColor Cyan
    gitlab-runner verify 2>&1 | ForEach-Object { Write-Host "    $_" }
} else {
    Write-Host "  GitLab Runner NOT found!" -ForegroundColor Yellow
    Write-Host "  Install: https://docs.gitlab.com/runner/install/windows/" -ForegroundColor Yellow
    Write-Host "  Or run as admin:" -ForegroundColor Yellow
    Write-Host "    New-Item -Force -ItemType Directory -Path C:\GitLab-Runner" -ForegroundColor Yellow
    Write-Host "    Invoke-WebRequest -Uri 'https://gitlab-runner-downloads.s3.amazonaws.com/latest/binaries/gitlab-runner-windows-amd64.exe' -OutFile 'C:\GitLab-Runner\gitlab-runner.exe'" -ForegroundColor Yellow
    Write-Host "    cd C:\GitLab-Runner" -ForegroundColor Yellow
    Write-Host "    .\gitlab-runner.exe register" -ForegroundColor Yellow
    Write-Host "    .\gitlab-runner.exe install" -ForegroundColor Yellow
    Write-Host "    .\gitlab-runner.exe start" -ForegroundColor Yellow
}

# ── 总结 ─────────────────────────────────────────────────────────────────────
Write-Host "`n═══════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  Setup Summary" -ForegroundColor Cyan
Write-Host "═══════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  Deps dir:     $DepsDir"
Write-Host "  scrcpy:       $(if (Test-Path "$scrcpyDir\scrcpy.exe") {'READY'} else {'MISSING'})"
Write-Host "  ios:          $(if (Test-Path "$iosDir\idevice_id.exe") {'READY'} else {'MISSING'})"
Write-Host "  Node.js:      $(if (Test-Command 'node') {'READY'} else {'MISSING'})"
Write-Host "  Python:       $(if (Test-Command 'python') {'READY'} else {'MISSING'})"
Write-Host "  GitLab Runner: $(if (Test-Command 'gitlab-runner') {'READY'} else {'MISSING'})"
Write-Host "═══════════════════════════════════════════════════`n" -ForegroundColor Cyan
