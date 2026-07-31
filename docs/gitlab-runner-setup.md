# GitLab Runner 安装与配置指南

> 内网 GitLab CI/CD 流水线需要一台 Windows 机器作为 Runner。
> 本文档指导从零搭建。

---

## 一、安装 GitLab Runner

### 1. 下载 Runner

在准备作为 Runner 的 Windows 机器上：

```powershell
# 以管理员身份打开 PowerShell
mkdir C:\GitLab-Runner -Force
cd C:\GitLab-Runner

# 下载 Runner 二进制
Invoke-WebRequest -Uri "https://gitlab-runner-downloads.s3.amazonaws.com/latest/binaries/gitlab-runner-windows-amd64.exe" `
  -OutFile "gitlab-runner.exe"
```

### 2. 注册 Runner

```powershell
cd C:\GitLab-Runner
.\gitlab-runner.exe register
```

按提示填写：

| 项目 | 填写内容 |
|------|---------|
| GitLab instance URL | `https://git.7k7k.com` |
| Registration token | 从 GitLab 项目 → Settings → CI/CD → Runners 获取 |
| Description | `app-coldstart-windows-runner` |
| Tags | **`windows`**（重要：.gitlab-ci.yml 用此 tag 选择 Runner） |
| Executor | `shell` |

### 3. 安装为系统服务

```powershell
cd C:\GitLab-Runner
.\gitlab-runner.exe install
.\gitlab-runner.exe start
```

验证：
```powershell
.\gitlab-runner.exe verify
```

---

## 二、安装构建依赖

### Node.js 18+

```powershell
winget install OpenJS.NodeJS.LTS
# 或从 https://nodejs.org/ 下载安装
```

### Python 3.11+

```powershell
winget install Python.Python.3.11
# 安装时勾选 "Add Python to PATH"
```

### Git（通常已安装）

```powershell
winget install Git.Git
```

验证：
```powershell
node --version    # v18.x 或更高
python --version  # Python 3.11.x
git --version
```

---

## 三、放置构建依赖（scrcpy + ios 工具链）

CI 流水线需要 `scrcpy/` 和 `ios/` 目录，它们不在代码仓库中（gitignored）。
需要预放在 Runner 机器的 `C:\ci-deps\app-coldstart\` 下。

```powershell
# 创建目录
mkdir C:\ci-deps\app-coldstart -Force

# 放置 scrcpy（从开发机拷贝或下载）
# 下载地址: https://github.com/Genymobile/scrcpy/releases → win64 包
# 解压到 C:\ci-deps\app-coldstart\scrcpy\
# 需包含: scrcpy.exe, scrcpy-server, SDL3.dll, av*.dll 等

# 放置 ios 工具链（从开发机拷贝）
# 从开发机的项目目录 ios/ 整个拷贝到
# C:\ci-deps\app-coldstart\ios\
```

或者从开发机直接拷贝：
```powershell
# 在开发机上打包
cd D:\work\tool\app-coldstart-qoder
tar czf deps.tar.gz scrcpy/ ios/

# 传到 Runner 机器后解压到 C:\ci-deps\app-coldstart\
```

### 验证

```powershell
# 运行一键检查脚本
cd <项目目录>
powershell -ExecutionPolicy Bypass -File scripts\ci-prepare.ps1
```

---

## 四、网络要求

Runner 机器需要能访问以下外网地址（构建时下载依赖）：

| 地址 | 用途 |
|------|------|
| `registry.npmjs.org` | npm 包（electron, electron-builder） |
| `pypi.org` | pip 包（FastAPI, OpenCV, RapidOCR 等） |
| `www.python.org/ftp/` | Python embeddable 下载 |
| `github.com/electron-userland/` | electron-builder 二进制（winCodeSign, nsis） |

如果 Runner 无法访问外网，需要配置内网镜像：
- npm: `npm config set registry https://<内网npm镜像>/`
- pip: `pip config set global.index-url https://<内网pip镜像>/simple`

---

## 五、常见问题

### Q1: winCodeSign 解压失败（符号链接权限）

首次构建时 electron-builder 下载 winCodeSign 可能因符号链接权限失败。

解决：在 Runner 机器上以管理员身份执行：
```powershell
# 启用开发者模式（允许创建符号链接）
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock" /t REG_DWORD /f /v "AllowDevelopmentWithoutDevLicense" /d "1"
```

或手动预解压缓存：
```powershell
$cache = "$env:LOCALAPPDATA\electron-builder\Cache\winCodeSign"
mkdir $cache -Force -ErrorAction SilentlyContinue
# 下载并用 7za 解压，带 -snl- 参数（符号链接当普通文件）
```

### Q2: 构建慢（python-embed 每次重建）

检查 GitLab CI 缓存是否生效：
- Pipelines → 对应 job → 查看日志中是否有 "Checking cache" 和 "Successfully extracted cache"
- 确保 `requirements.txt` 没有频繁变化（缓存 key 基于此文件）

### Q3: Release 未创建

- 确认是 **tag 推送**（不是 branch push），且 tag 格式为 `vX.Y.Z`
- 确认 Runner 有 `windows` tag
- 查看 release job 的日志

---

## 六、完整搭建检查清单

- [ ] GitLab Runner 已安装并注册（tag: `windows`，executor: `shell`）
- [ ] Runner 服务正在运行（`gitlab-runner.exe status`）
- [ ] Node.js 18+ 已安装
- [ ] Python 3.11+ 已安装
- [ ] Git 已安装
- [ ] `C:\ci-deps\app-coldstart\scrcpy\` 就位（含 scrcpy.exe）
- [ ] `C:\ci-deps\app-coldstart\ios\` 就位（含 idevice_id.exe）
- [ ] Runner 能访问外网（或配置了内网镜像）
- [ ] `ci-prepare.ps1` 全部显示 READY
