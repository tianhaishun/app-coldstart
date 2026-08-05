#!/bin/bash
# ══════════════════════════════════════════════════════════════════════════════
# start-mac.sh — Mac 一键启动脚本（启动桌面客户端）
#
# 用法：
#   方式一（推荐）：Finder 中双击 Start-Mac.command（本脚本的入口壳）
#   方式二（终端）：
#     git clone git@git.7k7k.com:tianhaishun/app-coldstart.git
#     cd app-coldstart
#     bash start-mac.sh
#
# 脚本自动完成（只装缺失的，重复执行无副作用）：
#   1. 安装 Homebrew 依赖（按平台环境区分）：
#        - node            → npm install 用
#        - python3         → 客户端首次启动自动建 .venv 装依赖
#        - android-platform-tools → 提供 adb（server.py 回退 PATH 找到）
#        - libimobiledevice / ideviceinstaller → iOS 设备检测
#        - scrcpy          → 镜像/录屏；符号链接进 scrcpy/ 供客户端识别
#   2. npm install（Electron 二进制，首次约 1-3 分钟）
#   3. 启动桌面客户端（`npm start`；Python venv 创建与依赖安装
#      在客户端闪屏窗口内自动完成，无需手动干预）
# ══════════════════════════════════════════════════════════════════════════════

set -e

BOLD="\033[1m"
GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[31m"
RESET="\033[0m"

info()  { echo -e "${GREEN}[*]${RESET} $1"; }
warn()  { echo -e "${YELLOW}[!]${RESET} $1"; }
fail()  { echo -e "${RED}[✗]${RESET} $1"; exit 1; }

ROOT="$(cd "$(dirname "$0")" && pwd)"

echo -e "${BOLD}═══════════════════════════════════════════════${RESET}"
echo -e "${BOLD}  App 冷启测速 — Mac 一键启动${RESET}"
echo -e "${BOLD}═══════════════════════════════════════════════${RESET}"
echo ""

# ── 1. Homebrew ───────────────────────────────────────────────────────────────
info "检查 Homebrew..."
if ! command -v brew &>/dev/null; then
    warn "Homebrew 未安装，正在安装（首次约 5-10 分钟）..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Apple Silicon 需要加到 PATH
    if [[ -f /opt/homebrew/bin/brew ]]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    fi
fi
echo "  Homebrew: $(brew --version | head -1)"

# ── 2. 按平台环境安装依赖（只装缺失项）────────────────────────────────────────
info "检查 / 安装依赖..."

# Node.js（Electron 客户端必需）
if ! command -v node &>/dev/null; then
    echo "  安装 Node.js..."
    brew install node
fi
echo "  Node.js: $(node --version)"

# Python（客户端首次启动用它建 .venv）
if ! command -v python3 &>/dev/null; then
    echo "  安装 Python..."
    brew install python@3.11
fi
echo "  Python: $(python3 --version)"

# Android platform-tools（提供 adb；server.py 会回退到 PATH 使用）
if ! command -v adb &>/dev/null; then
    echo "  安装 android-platform-tools..."
    brew install --cask android-platform-tools
fi
echo "  adb: $(adb --version 2>&1 | head -1)"

# iOS 工具链（检测/操作 iOS 设备；libimobiledevice 提供 idevice_id）
if ! command -v idevice_id &>/dev/null; then
    echo "  安装 libimobiledevice..."
    brew install libimobiledevice
fi
if ! command -v ideviceinstaller &>/dev/null; then
    echo "  安装 ideviceinstaller..."
    brew install ideviceinstaller
fi
echo "  idevice_id: OK"

# scrcpy（镜像/录屏）
if ! command -v scrcpy &>/dev/null; then
    echo "  安装 scrcpy..."
    brew install scrcpy
fi
echo "  scrcpy: $(scrcpy --version 2>&1 | head -1)"

# ── 3. scrcpy 符号链接进项目 scrcpy/ 目录 ─────────────────────────────────────
# 客户端（electron/scrcpy-manager.js）开发模式只认 ROOT/scrcpy/scrcpy +
# scrcpy/scrcpy-server，不回退 PATH。用符号链接指向 brew 安装的二进制，
# 避免重复下载 ~35MB 的 win64 包。
info "链接 scrcpy 到项目 scrcpy/ 目录..."
mkdir -p "$ROOT/scrcpy"
if [[ ! -e "$ROOT/scrcpy/scrcpy" ]]; then
    ln -s "$(command -v scrcpy)" "$ROOT/scrcpy/scrcpy"
fi
BREW_PREFIX="$(brew --prefix scrcpy 2>/dev/null || echo /opt/homebrew)"
SERVER_SRC="$BREW_PREFIX/share/scrcpy/scrcpy-server"
if [[ ! -e "$ROOT/scrcpy/scrcpy-server" ]]; then
    if [[ -f "$SERVER_SRC" ]]; then
        ln -s "$SERVER_SRC" "$ROOT/scrcpy/scrcpy-server"
    else
        warn "未找到 scrcpy-server（$SERVER_SRC）——镜像/录屏将不可用，不影响核心测速"
    fi
fi

# ── 4. npm 依赖（Electron 二进制，首次约 1-3 分钟）────────────────────────────
if [[ ! -d "$ROOT/node_modules" ]]; then
    info "安装 npm 依赖（Electron，首次约 1-3 分钟）..."
    (cd "$ROOT" && npm install --no-audit --no-fund)
fi

# ── 5. 启动桌面客户端 ─────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}═══════════════════════════════════════════════${RESET}"
info "启动 App 冷启测速客户端..."
echo "  首次启动会在客户端闪屏内自动创建 .venv 并安装 Python 依赖，请耐心等待。"
echo "  关闭客户端窗口即退出（后端会自动清理）。"
echo ""
cd "$ROOT"
exec npm start
