#requires -Version 5.1
<#
  Windows one-click bootstrap and launcher.

  The script intentionally does not require Node.js or Python to exist on PATH.
  It prefers a usable system installation, then installs private runtimes under
  .runtime/ from pinned official archives/installers. This keeps the launcher
  usable on machines with different user permissions and PATH settings.

  Client mode:
    runtime checks -> adb -> scrcpy -> iOS toolchain -> Python venv/deps
    -> npm install -> Electron client

  Web mode:
    runtime checks -> adb -> iOS toolchain -> Python venv/deps
    -> uvicorn + browser (no Node/Electron/scrcpy required)
#>

[CmdletBinding()]
param(
    [ValidateSet('Client', 'Web')]
    [string]$Mode = 'Client'
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
# Windows PowerShell 5.1 may negotiate an obsolete TLS version by default.
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Root = Split-Path -Parent $PSScriptRoot
$RuntimeRoot = Join-Path $Root '.runtime'
$TempRoot = Join-Path $env:TEMP 'app-coldstart-bootstrap'
$Port = 8766

function Write-Info([string]$Message) { Write-Host "[INFO] $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message) { Write-Host "[ OK ] $Message" -ForegroundColor Green }
function Write-Warn([string]$Message) { Write-Host "[WARN] $Message" -ForegroundColor Yellow }

function Get-CommandPath([string]$Name) {
    try {
        $command = Get-Command $Name -ErrorAction Stop | Select-Object -First 1
        if ($command.Path) { return $command.Path }
        if ($command.Source) { return $command.Source }
    } catch { return $null }
    return $null
}

function Invoke-Checked([string]$FilePath, [string[]]$Arguments) {
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) { throw "命令失败（$LASTEXITCODE）：$FilePath $($Arguments -join ' ')" }
}

function Download-Verified([string]$Uri, [string]$Path, [string]$Sha256 = '') {
    $parent = Split-Path -Parent $Path
    if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    Write-Info "下载：$Uri"
    Invoke-WebRequest -UseBasicParsing -Uri $Uri -OutFile $Path
    if (-not (Test-Path $Path)) { throw "下载完成但文件不存在：$Path" }
    if ($Sha256) {
        $actual = (Get-FileHash -Algorithm SHA256 -Path $Path).Hash.ToLowerInvariant()
        if ($actual -ne $Sha256.ToLowerInvariant()) {
            Remove-Item -Force -ErrorAction SilentlyContinue $Path
            throw "SHA256 校验失败：$Path`n期望：$Sha256`n实际：$actual"
        }
        Write-Ok 'SHA256 校验通过'
    }
}

function Get-WindowsArchitecture() {
    $arch = $env:PROCESSOR_ARCHITEW6432
    if (-not $arch) { $arch = $env:PROCESSOR_ARCHITECTURE }
    switch ($arch.ToUpperInvariant()) {
        'ARM64' { return 'arm64' }
        'AMD64' { return 'x64' }
        'X86' { return 'x86' }
        default { throw "不支持的 Windows 架构：$arch" }
    }
}

function Get-UsablePython([string]$Candidate) {
    if (-not $Candidate -or -not (Test-Path $Candidate)) { return $null }
    try {
        $text = (& $Candidate -c "import sys; print('%d.%d' % (sys.version_info[0], sys.version_info[1]))" 2>$null | Out-String).Trim()
        $version = [Version]$text
        if ($version -lt [Version]'3.10' -or $version -ge [Version]'3.12') { return $null }
        & $Candidate -c 'import venv, pip' 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        return $Candidate
    } catch { return $null }
}

function Find-SystemPython() {
    $python = Get-CommandPath 'python.exe'
    $usable = Get-UsablePython $python
    if ($usable) { return $usable }
    $launcher = Get-CommandPath 'py.exe'
    if ($launcher) {
        try {
            $resolved = (& $launcher -3 -c 'import sys; print(sys.executable)' 2>$null | Out-String).Trim()
            $usable = Get-UsablePython $resolved
            if ($usable) { return $usable }
        } catch { }
    }
    return $null
}

function Ensure-Node() {
    $systemNode = Get-CommandPath 'node.exe'
    $systemNpm = Get-CommandPath 'npm.cmd'
    if ($systemNode -and $systemNpm) {
        try {
            $versionText = (& $systemNode --version 2>$null | Out-String).Trim()
            $major = [int](([regex]::Match($versionText, '^v(\d+)')).Groups[1].Value)
            if ($major -ge 18) {
                Write-Ok "使用系统 Node.js $versionText"
                return @{ Node = $systemNode; Npm = $systemNpm }
            }
            Write-Warn "系统 Node.js $versionText 过旧，改用项目私有 Node.js"
        } catch { Write-Warn '系统 Node.js 无法验证，改用项目私有 Node.js' }
    } else { Write-Info '未检测到可用 Node.js/npm，准备安装项目私有运行时...' }

    $arch = Get-WindowsArchitecture
    if ($arch -eq 'x86') { throw '当前 Electron 运行时不提供 Windows x86 版本，请使用 Windows x64 或 ARM64。' }
    $nodeVersion = '24.19.0'
    $nodeFile = "node-v$nodeVersion-win-$arch.zip"
    $nodeHash = @{ x64 = '57f71ab3652e797d84acddc79c81cc9ff1c6ddb2a1974cdb83f00fee9bff4c73'; arm64 = '8502f4a50b458d4cc38ed8f2001556c2cd239d464920f74017926ccb1e1c157f' }[$arch]
    $nodeDir = Join-Path $RuntimeRoot 'node'
    $nodeExe = Join-Path $nodeDir 'node.exe'
    $npmCmd = Join-Path $nodeDir 'npm.cmd'
    if (-not (Test-Path $nodeExe) -or -not (Test-Path $npmCmd)) {
        $zip = Join-Path $TempRoot $nodeFile
        $stage = Join-Path $TempRoot 'node-stage'
        try {
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $stage
            Download-Verified "https://nodejs.org/dist/v$nodeVersion/$nodeFile" $zip $nodeHash
            Expand-Archive -Force -Path $zip -DestinationPath $stage
            $top = Get-ChildItem -Directory -Path $stage | Select-Object -First 1
            if (-not $top) { throw 'Node.js 压缩包解压后没有顶层目录' }
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $nodeDir
            New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
            Move-Item -LiteralPath $top.FullName -Destination $nodeDir
        } finally {
            Remove-Item -Force -ErrorAction SilentlyContinue $zip
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $stage
        }
    }
    if (-not (Test-Path $nodeExe) -or -not (Test-Path $npmCmd)) { throw "项目私有 Node.js 安装不完整：$nodeDir" }
    Write-Ok "项目私有 Node.js $(& $nodeExe --version) 已就绪"
    return @{ Node = $nodeExe; Npm = $npmCmd }
}

function Ensure-Python() {
    $system = Find-SystemPython
    if ($system) { Write-Ok "使用系统 Python $(& $system --version 2>&1)"; return $system }

    Write-Info '未检测到可用 Python 3.10+，准备安装项目私有 Python 3.11.9...'
    $arch = Get-WindowsArchitecture
    $pythonFile = switch ($arch) {
        'x64' { 'python-3.11.9-amd64.exe' }
        'arm64' { 'python-3.11.9-arm64.exe' }
        'x86' { 'python-3.11.9.exe' }
        default { throw "不支持的 Python 架构：$arch" }
    }
    $pythonHash = @{
        x64 = '5ee42c4eee1e6b4464bb23722f90b45303f79442df63083f05322f1785f5fdde'
        arm64 = '58f3a4e91b63d5a680ecc77c1db4565a1e3966e8651d4c8b89200d58c1f5c4f3'
        x86 = 'af19e5e2f03e715a822181f2cb7d4efef4eda13fa4a2db6da12e998e46f5cbf9'
    }[$arch]
    $installer = Join-Path $TempRoot $pythonFile
    $pythonDir = Join-Path $RuntimeRoot 'python311'
    try {
        Download-Verified "https://www.python.org/ftp/python/3.11.9/$pythonFile" $installer $pythonHash
        New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
        $args = @('/quiet', 'InstallAllUsers=0', "TargetDir=`"$pythonDir`"", 'Include_pip=1', 'Include_test=0', 'PrependPath=0', 'Include_launcher=0')
        $p = Start-Process -FilePath $installer -ArgumentList $args -Wait -PassThru -WindowStyle Hidden
        if ($p.ExitCode -ne 0) { throw "Python 安装程序失败，退出码：$($p.ExitCode)" }
    } finally { Remove-Item -Force -ErrorAction SilentlyContinue $installer }
    $local = Get-UsablePython (Join-Path $pythonDir 'python.exe')
    if (-not $local) { throw "Python 安装完成但无法使用：$pythonDir" }
    Write-Ok "项目私有 Python $(& $local --version 2>&1) 已就绪"
    return $local
}

function Ensure-VirtualEnvironment([string]$Python) {
    $venv = Join-Path $Root '.venv'
    $venvPython = Join-Path $venv 'Scripts\python.exe'
    $venvCreated = $false
    if (-not (Test-Path $venvPython)) { Write-Info '创建 Python 虚拟环境...'; Invoke-Checked $Python @('-m', 'venv', $venv); $venvCreated = $true }
    $requirements = Join-Path $Root 'requirements.txt'
    $marker = Join-Path $RuntimeRoot 'python-deps.ready'
    $needsInstall = $venvCreated -or -not (Test-Path $marker)
    if (-not $needsInstall) { $needsInstall = (Get-Item $requirements).LastWriteTimeUtc -gt (Get-Item $marker).LastWriteTimeUtc }
    if ($needsInstall) {
        Write-Info '安装/更新 Python 依赖（首次可能需要 1-3 分钟）...'
        Invoke-Checked $venvPython @('-m', 'pip', 'install', '-r', $requirements)
        New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
        Set-Content -Encoding ASCII -Path $marker -Value (Get-Date -Format o)
    } else { Write-Ok 'Python 依赖已就绪' }
    return $venvPython
}

function Ensure-Adb() {
    $adbDir = Join-Path $Root 'adb'
    $required = @('adb.exe', 'AdbWinApi.dll', 'AdbWinUsbApi.dll')
    $missing = $required | Where-Object { -not (Test-Path (Join-Path $adbDir $_)) }
    if (-not $missing) { Write-Ok 'Android platform-tools 已就绪'; return }
    Write-Info '未检测到完整 adb，下载官方 Android platform-tools...'
    $zip = Join-Path $TempRoot 'platform-tools-latest-windows.zip'; $stage = Join-Path $TempRoot 'platform-tools-stage'
    try {
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $stage
        Download-Verified 'https://dl.google.com/android/repository/platform-tools-latest-windows.zip' $zip
        Expand-Archive -Force -Path $zip -DestinationPath $stage
        $source = Join-Path $stage 'platform-tools'
        foreach ($file in $required) { if (-not (Test-Path (Join-Path $source $file))) { throw "platform-tools 缺少：$file" } }
        New-Item -ItemType Directory -Force -Path $adbDir | Out-Null
        foreach ($file in $required) { Copy-Item -Force (Join-Path $source $file) (Join-Path $adbDir $file) }
        Copy-Item -Force -ErrorAction SilentlyContinue (Join-Path $source 'source.properties') (Join-Path $adbDir 'source.properties')
        $missing = $required | Where-Object { -not (Test-Path (Join-Path $adbDir $_)) }
        if ($missing) { throw "adb 安装不完整，缺少：$($missing -join ', ')" }
        Write-Ok '官方 adb 已就绪'
    } finally { Remove-Item -Force -ErrorAction SilentlyContinue $zip; Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $stage }
}

function Ensure-Scrcpy() {
    $dir = Join-Path $Root 'scrcpy'; $required = @('scrcpy.exe', 'scrcpy-server')
    if ((Get-WindowsArchitecture) -eq 'arm64') {
        Write-Warn '当前是 Windows ARM64；scrcpy 使用 x64 上游包，可能依赖系统 x64 仿真运行。'
    }
    $missing = $required | Where-Object { -not (Test-Path (Join-Path $dir $_)) }
    if (-not $missing) { Write-Ok 'scrcpy 已就绪'; return }
    Write-Info '未检测到完整 scrcpy，下载上游 v3.3...'
    $zip = Join-Path $TempRoot 'scrcpy-win64-v3.3.zip'; $stage = Join-Path $TempRoot 'scrcpy-stage'
    try {
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $stage
        Download-Verified 'https://github.com/Genymobile/scrcpy/releases/download/v3.3/scrcpy-win64-v3.3.zip' $zip 'a120cb4be7cde2891af38e83d2008173a0b6b6b5e344b2dfe668d0f892999933'
        Expand-Archive -Force -Path $zip -DestinationPath $stage
        $source = Get-ChildItem -Directory -Path $stage | Select-Object -First 1
        if (-not $source) { throw 'scrcpy 压缩包解压后没有顶层目录' }
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $dir
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        Copy-Item -Recurse -Force (Join-Path $source.FullName '*') $dir
        $missing = $required | Where-Object { -not (Test-Path (Join-Path $dir $_)) }
        if ($missing) { throw "scrcpy 安装不完整，缺少：$($missing -join ', ')" }
        Write-Ok 'scrcpy 已就绪'
    } catch { Write-Warn "scrcpy 安装失败；镜像/录屏不可用，但不影响核心测速：$($_.Exception.Message)" }
    finally { Remove-Item -Force -ErrorAction SilentlyContinue $zip; Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $stage }
}

function Get-AppleMobileDeviceService() {
    try { return Get-Service -ErrorAction Stop | Where-Object { $_.Name -eq 'Apple Mobile Device Service' -or $_.DisplayName -eq 'Apple Mobile Device Service' } | Select-Object -First 1 } catch { return $null }
}
function Test-AppleMobileDeviceService() { return ($null -ne (Get-AppleMobileDeviceService)) }

function Install-AppleMobileDeviceSupport() {
    $winget = Get-CommandPath 'winget.exe'
    if ($winget) {
        Write-Info '使用 winget 安装 Apple iTunes（包含 Apple Mobile Device Support）...'
        & $winget install --id Apple.iTunes --exact --source winget --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
        if ($LASTEXITCODE -eq 0 -or (Test-AppleMobileDeviceService)) { return }
        Write-Warn "winget 安装 iTunes 返回 $LASTEXITCODE，改用 Apple 官方安装程序"
    }
    $installer = Join-Path $TempRoot 'iTunes64Setup.exe'
    try {
        Download-Verified 'https://www.apple.com/itunes/download/win64' $installer
        $p = Start-Process -FilePath $installer -ArgumentList @('/quiet', '/norestart') -Wait -PassThru -WindowStyle Hidden
        if ($p.ExitCode -ne 0) { Write-Warn "Apple iTunes 安装程序返回 $($p.ExitCode)" }
    } catch { Write-Warn "Apple Mobile Device Support 安装失败：$($_.Exception.Message)" }
    finally { Remove-Item -Force -ErrorAction SilentlyContinue $installer }
}

function Ensure-WindowsIos() {
    $iosDir = Join-Path $Root 'ios'
    # Accept either the existing legacy bundle already used by this project or
    # the pinned portable v1.4.1 bundle downloaded by this script.
    $bundles = @(
        @('idevice_id.exe', 'libcrypto-1_1-x64.dll', 'libssl-1_1-x64.dll', 'usbmuxd.dll'),
        @('idevice_id.exe', 'libcrypto-3-x64.dll', 'libssl-3-x64.dll', 'libusbmuxd-2.0.dll')
    )
    $complete = $false
    foreach ($bundle in $bundles) {
        if (($bundle | Where-Object { -not (Test-Path (Join-Path $iosDir $_)) }).Count -eq 0) {
            $complete = $true
            break
        }
    }
    if (-not $complete) {
        Write-Info '未检测到完整 Windows iOS 工具链，下载固定版本 v1.4.1...'
        $zip = Join-Path $TempRoot 'libimobiledevice-v1.4.1-windows-x64-portable.zip'; $stage = Join-Path $TempRoot 'ios-stage'
        try {
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $stage
            Download-Verified 'https://github.com/lilnynho/libimobiledevice-portable/releases/download/v1.4.1/libimobiledevice-v1.4.1-windows-x64-portable.zip' $zip '5cd9b8b1dd75a36f781e6b869624f000c3a55a09e834fe4f70df0f5784f8f7c7'
            Expand-Archive -Force -Path $zip -DestinationPath $stage
            New-Item -ItemType Directory -Force -Path $iosDir | Out-Null
            foreach ($item in (Get-ChildItem -Force -Path $stage)) {
                Copy-Item -LiteralPath $item.FullName -Destination $iosDir -Recurse -Force
            }
            $installed = @('idevice_id.exe', 'libcrypto-3-x64.dll', 'libssl-3-x64.dll', 'libusbmuxd-2.0.dll')
            $missing = $installed | Where-Object { -not (Test-Path (Join-Path $iosDir $_)) }
            if ($missing) { throw "iOS 工具链安装不完整，缺少：$($missing -join ', ')" }
            Write-Ok 'Windows iOS 工具链已就绪'
        } catch { Write-Warn "Windows iOS 工具链安装失败；iOS 功能不可用，但不影响 Android：$($_.Exception.Message)" }
        finally { Remove-Item -Force -ErrorAction SilentlyContinue $zip; Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $stage }
    } else { Write-Ok 'Windows iOS 工具链已就绪' }

    if (-not (Test-AppleMobileDeviceService)) { Write-Info '未检测到 Apple Mobile Device Service，尝试安装 Apple iTunes...'; Install-AppleMobileDeviceSupport; Start-Sleep -Seconds 2 }
    $amds = Get-AppleMobileDeviceService
    if ($amds) {
        if ($amds.Status -ne 'Running') { try { Start-Service -InputObject $amds -ErrorAction Stop; Write-Ok 'Apple Mobile Device Service 已启动' } catch { Write-Warn "Apple Mobile Device Service 已安装但启动失败：$($_.Exception.Message)" } }
        else { Write-Ok 'Apple Mobile Device Service 已就绪' }
    } else { Write-Warn '未检测到 Apple Mobile Device Service；Android 不受影响，iOS 设备可能无法识别。' }
}

function Start-WebBackend([string]$VenvPython) {
    # Windows PowerShell 5.1 会把 ArgumentList 数组拼接成一个命令行字符串，
    # 因此对可能含空格的项目路径显式加引号。
    $quotedRoot = '"' + $Root + '"'
    $args = @('-m', 'uvicorn', 'server:app', '--host', '0.0.0.0', '--port', "$Port", '--app-dir', $quotedRoot)
    Write-Info "启动浏览器备用模式：http://127.0.0.1:$Port/"
    try {
        $ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -ne '0.0.0.0' } | Select-Object -ExpandProperty IPAddress
        foreach ($ip in $ips) { Write-Host "  局域网访问：http://$ip`:$Port/" }
    } catch { Write-Warn '无法自动列出局域网 IPv4 地址，请使用 ipconfig 查看' }
    $process = Start-Process -FilePath $VenvPython -ArgumentList $args -WorkingDirectory $Root -PassThru -NoNewWindow
    try {
        for ($i = 0; $i -lt 30; $i++) {
            Start-Sleep -Seconds 1
            try {
                $health = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 1
                if ($health.StatusCode -eq 200) {
                    Start-Process "http://127.0.0.1:$Port/"
                    Write-Ok '后端已就绪，浏览器已打开。保持此窗口开启；关闭窗口会停止后端。'
                    Wait-Process -Id $process.Id
                    return
                }
            } catch { }
            if ($process.HasExited) { throw "后端进程提前退出，退出码：$($process.ExitCode)" }
        }
        throw '后端 30 秒内未就绪，请检查端口或终端错误输出。'
    } finally {
        if ($process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
    }
}

try {
    Set-Location $Root
    New-Item -ItemType Directory -Force -Path $RuntimeRoot, $TempRoot | Out-Null
    Write-Host '============================================' -ForegroundColor White
    Write-Host "  App Cold Start Profiler - Windows $Mode" -ForegroundColor White
    Write-Host '============================================' -ForegroundColor White
    $node = $null
    if ($Mode -eq 'Client') { $node = Ensure-Node; $env:PATH = "$(Split-Path -Parent $node.Node);$env:PATH" }
    $python = Ensure-Python; $env:CST_PYTHON = $python
    Ensure-Adb; Ensure-WindowsIos
    if ($Mode -eq 'Client') { Ensure-Scrcpy }
    $venvPython = Ensure-VirtualEnvironment $python
    if ($Mode -eq 'Web') { Start-WebBackend $venvPython; exit 0 }
    if (-not (Test-Path (Join-Path $Root 'node_modules\.bin\electron.cmd'))) { Write-Info '安装 npm 依赖（Electron，首次可能需要 1-3 分钟）...'; Invoke-Checked $node.Npm @('install', '--no-audit', '--no-fund') } else { Write-Ok 'npm 依赖已就绪' }
    Write-Info '启动 Electron 客户端...'; & $node.Npm 'start'; exit $LASTEXITCODE
} catch { Write-Host "[FAIL] $($_.Exception.Message)" -ForegroundColor Red; exit 1 }
