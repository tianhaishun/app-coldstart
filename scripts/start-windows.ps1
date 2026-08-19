# ══════════════════════════════════════════════════════════════════════════════
# App 冷启测速 — Windows 一键启动（Electron 客户端）
#
# 职责（与 CLAUDE.md 约定一致）：
#   校验/补齐 Node.js、Python、adb、scrcpy、iOS 工具链 + npm/Python 依赖，
#   然后启动 Electron 客户端（npm start）。
#
# 原则：
#   - 只补缺失、不破坏既有环境（系统已装的 Node/Python 优先，下载版放 .runtime/）
#   - 下载 URL 全部为已验证的官方/上游地址（写入前逐一验证过 200）
#
# 由根目录 Start.bat 调用（-NoProfile -ExecutionPolicy Bypass），也可手动：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start-windows.ps1
# ══════════════════════════════════════════════════════════════════════════════

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Root = Split-Path -Parent $PSScriptRoot   # scripts/ 的上一级 = 仓库根
$RuntimeDir = Join-Path $Root ".runtime"   # gitignored：下载的便携运行时

# ── 官方下载地址（改动前先验证可访问；版本号集中在此便于升级）──
$NodeVersion = "24.15.0"      # Node.js LTS
$NodeUrl = "https://nodejs.org/dist/v$NodeVersion/node-v$NodeVersion-win-x64.zip"
$AdbUrl = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
$ScrcpyVersion = "3.3.3"
$ScrcpyUrl = "https://github.com/Genymobile/scrcpy/releases/download/v$ScrcpyVersion/scrcpy-win64-v$ScrcpyVersion.zip"
$PyVersions = @("3.11.9", "3.11.8", "3.11.7", "3.11.6", "3.11.5")
$GetPipUrl = "https://bootstrap.pypa.io/get-pip.py"

function Write-Step($msg) { Write-Host "`n[==] $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Skip($msg) { Write-Host "  [SKIP] $msg" -ForegroundColor DarkGray }
function Exit-Fail($msg) {
  Write-Host "`n[FAIL] $msg" -ForegroundColor Red
  Write-Host "  请保留上方输出，反馈给维护者。"
  exit 1
}

function Invoke-Native {
  # PS 5.1 会把原生命令的 stderr 包装成 NativeCommandError，在本脚本的
  # $ErrorActionPreference='Stop' 下升级为【终止错误】（实测：adb start-server
  # 的 daemon 提示、npm/pip 的进度输出都走 stderr，2>$null 也拦不住，必现踩坑）。
  # 本函数调用期间临时降为 Continue，stderr 按普通输出处理；退出码不变，
  # 调用方照常读 $LASTEXITCODE 判断成败。
  param([scriptblock]$Body)
  $prev = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try { & $Body } finally { $ErrorActionPreference = $prev }
}

function Download-File($url, $dest) {
  Write-Host "  下载: $url"
  try {
    # PS 5.1 默认只走 TLS 1.0，必须显式开启 TLS 1.2 才能访问 https
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing -TimeoutSec 300
  } catch {
    throw "下载失败：$url（$($_.Exception.Message)）"
  }
  $mb = [math]::Round((Get-Item $dest).Length / 1MB, 1)
  Write-Host "  完成: $dest ($mb MB)"
}

# ── 1. Node.js ────────────────────────────────────────────────────────────────
Write-Step "检查 Node.js ..."
$NodeDir = Join-Path $RuntimeDir "node"
$NodeExe = Join-Path $NodeDir "node.exe"
if (Get-Command node -ErrorAction SilentlyContinue) {
  Write-Ok "系统已装 Node.js $(node --version)（优先使用）"
} elseif (Test-Path $NodeExe) {
  Write-Ok "使用本地便携 Node.js（.runtime\node）"
  $env:PATH = "$NodeDir;$env:PATH"
} else {
  Write-Host "  未检测到 Node.js，下载官方便携版到 .runtime\node（不改系统环境）..."
  New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
  $zip = Join-Path $RuntimeDir "node-download.zip"
  Download-File $NodeUrl $zip
  $tmp = Join-Path $RuntimeDir "node-tmp"
  if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
  Expand-Archive -Path $zip -DestinationPath $tmp
  if (Test-Path $NodeDir) { Remove-Item -Recurse -Force $NodeDir }  # 清理半成品
  Move-Item (Join-Path $tmp "node-v$NodeVersion-win-x64") $NodeDir
  Remove-Item -Recurse -Force $tmp
  Remove-Item -Force $zip
  $env:PATH = "$NodeDir;$env:PATH"
  Write-Ok "Node.js $NodeVersion 就绪（.runtime\node，未改系统环境）"
}
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
  Exit-Fail "未找到 npm（Node.js 安装不完整），请重新安装 Node.js"
}

# ── 2. npm 依赖 ───────────────────────────────────────────────────────────────
Write-Step "检查 npm 依赖 ..."
if (Test-Path (Join-Path $Root "node_modules\electron")) {
  Write-Skip "node_modules\electron 已存在，跳过 npm install"
} else {
  Write-Host "  首次运行：npm install（约 1-3 分钟）..."
  Push-Location $Root
  try {
    Invoke-Native { npm install --prefer-offline --no-audit --no-fund }
    if ($LASTEXITCODE -ne 0) { throw "npm install 退出码 $LASTEXITCODE" }
  } finally { Pop-Location }
  Write-Ok "npm 依赖安装完成"
}

# ── 3. Python 运行时 ──────────────────────────────────────────────────────────
# 客户端优先级：python-embed（内置）→ .venv → 系统 Python（自动建 venv 装依赖）。
# 脚本只需保证三者至少存在其一；全无时按 build-python-embed.py 同款流程构建。
Write-Step "检查 Python 运行时 ..."
$EmbedPy = Join-Path $Root "python-embed\python.exe"
if (Test-Path $EmbedPy) {
  Write-Ok "内置 python-embed 已就绪（客户端优先使用，零联网）"
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
  Write-Ok "系统 Python：$(python --version 2>&1)（客户端首次启动会自动建 .venv 装依赖）"
} else {
  Write-Host "  未检测到任何 Python，自动构建 python-embed（官方 embeddable + pip 装依赖，约 3-5 分钟）..."
  $EmbedDir = Join-Path $Root "python-embed"
  New-Item -ItemType Directory -Force -Path $EmbedDir | Out-Null
  New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null

  # 3.1 下载 embeddable（多版本依次尝试）
  $zip = Join-Path $RuntimeDir "python-embed-download.zip"
  $downloaded = $false
  foreach ($v in $PyVersions) {
    try {
      Write-Host "  尝试 Python $v ..."
      Download-File "https://www.python.org/ftp/python/$v/python-$v-embed-amd64.zip" $zip
      $downloaded = $true
      break
    } catch {
      Write-Host "  失败: $($_.Exception.Message)"
      if (Test-Path $zip) { Remove-Item -Force $zip }
    }
  }
  if (-not $downloaded) { Exit-Fail "所有 Python 版本下载均失败，请检查网络" }

  Expand-Archive -Path $zip -DestinationPath $EmbedDir
  Remove-Item -Force $zip

  # 3.2 启用 site-packages（embeddable 默认注释了 import site）
  $pth = Get-ChildItem $EmbedDir -Filter "python*._pth" | Select-Object -First 1
  if ($pth) {
    $c = Get-Content $pth.FullName -Raw
    $c = $c -replace "#import site", "import site"
    if ($c -notmatch "Lib/site-packages") { $c += "`nLib/site-packages`n" }
    if ($c -notmatch "(?m)^\.$") { $c += ".`n" }
    [System.IO.File]::WriteAllText($pth.FullName, $c, (New-Object System.Text.UTF8Encoding($false)))
  } else {
    Exit-Fail "未找到 python*._pth，embeddable 包结构异常"
  }

  # 3.3 pip + 运行时依赖
  Download-File $GetPipUrl (Join-Path $EmbedDir "get-pip.py")
  Push-Location $EmbedDir
  try {
    Invoke-Native { & (Join-Path $EmbedDir "python.exe") get-pip.py --no-warn-script-location }
    if ($LASTEXITCODE -ne 0) { throw "get-pip 退出码 $LASTEXITCODE" }
  } finally { Pop-Location }
  Remove-Item -Force (Join-Path $EmbedDir "get-pip.py")

  Write-Host "  安装运行时依赖（几分钟，请勿关闭窗口）..."
  Push-Location $Root
  try {
    Invoke-Native { & (Join-Path $EmbedDir "Scripts\pip.exe") install -r requirements.txt --no-warn-script-location }
    if ($LASTEXITCODE -ne 0) { throw "pip install 退出码 $LASTEXITCODE" }
  } finally { Pop-Location }
  Write-Ok "python-embed 构建完成"
}

# ── 4. adb ────────────────────────────────────────────────────────────────────
Write-Step "检查 adb ..."
$AdbExe = Join-Path $Root "adb\adb.exe"
if (Test-Path $AdbExe) {
  Write-Ok "内置 adb 已就绪"
} else {
  Write-Host "  下载官方 platform-tools ..."
  New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
  $zip = Join-Path $RuntimeDir "adb-download.zip"
  Download-File $AdbUrl $zip
  $tmp = Join-Path $RuntimeDir "adb-tmp"
  if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
  Expand-Archive -Path $zip -DestinationPath $tmp
  New-Item -ItemType Directory -Force -Path (Join-Path $Root "adb") | Out-Null
  Copy-Item (Join-Path $tmp "platform-tools\*") (Join-Path $Root "adb") -Recurse -Force
  Remove-Item -Recurse -Force $tmp
  Remove-Item -Force $zip
  Write-Ok "adb 已就绪"
}
# 统一 adb server 版本：机器上可能有多套 adb 共用同一 daemon(5037)，
# 版本不一致会在握手时偶发 connection reset → 检测不到设备
Invoke-Native { & $AdbExe kill-server 2>$null | Out-Null }
Invoke-Native { & $AdbExe start-server 2>$null | Out-Null }

# ── 5. scrcpy ─────────────────────────────────────────────────────────────────
Write-Step "检查 scrcpy ..."
if (Test-Path (Join-Path $Root "scrcpy\scrcpy.exe")) {
  Write-Ok "内置 scrcpy 已就绪"
} else {
  Write-Host "  下载官方 scrcpy v$ScrcpyVersion ..."
  New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
  $zip = Join-Path $RuntimeDir "scrcpy-download.zip"
  Download-File $ScrcpyUrl $zip
  $tmp = Join-Path $RuntimeDir "scrcpy-tmp"
  if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
  Expand-Archive -Path $zip -DestinationPath $tmp
  $src = Get-ChildItem $tmp -Directory | Select-Object -First 1
  if (-not $src) { Exit-Fail "scrcpy 压缩包结构异常" }
  if (Test-Path (Join-Path $Root "scrcpy")) { Remove-Item -Recurse -Force (Join-Path $Root "scrcpy") }  # 清理半成品
  Move-Item $src.FullName (Join-Path $Root "scrcpy")
  Remove-Item -Recurse -Force $tmp
  Remove-Item -Force $zip
  Write-Ok "scrcpy 已就绪"
}

# ── 6. iOS 工具链（可选，仅 iOS 测速需要）──────────────────────────────────
# Windows 版 iOS 工具链（ios\idevice_id.exe + 配套 DLL）随安装包内置，
# 仓库内无已验证的官方下载地址，缺了只提示不下载。
Write-Step "检查 iOS 工具链（可选）..."
if (Test-Path (Join-Path $Root "ios\idevice_id.exe")) {
  Write-Ok "iOS 工具链已就绪"
} else {
  Write-Host "  [WARN] 未找到 ios\idevice_id.exe：iOS 设备检测不可用，Android 测速不受影响。" -ForegroundColor Yellow
  Write-Host "         源码仓库使用请从维护者处获取工具链放入 ios\ 目录（安装包内置，无需此步）。" -ForegroundColor Yellow
}

# ── 7. 启动客户端 ─────────────────────────────────────────────────────────────
Write-Step "启动客户端 ..."
Write-Host "  启动 Electron（首次启动会自动建 .venv / 复用 python-embed，请耐心等待）..."
Push-Location $Root
try {
  Invoke-Native { npm start }
  if ($LASTEXITCODE -ne 0) { throw "客户端退出码 $LASTEXITCODE" }
} finally { Pop-Location }
