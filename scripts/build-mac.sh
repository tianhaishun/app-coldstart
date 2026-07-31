#!/bin/bash
# ══════════════════════════════════════════════════════════════════════════════
# Mac 一键打包脚本
#
# 用法（在 Mac 终端中执行）：
#   git clone git@git.7k7k.com:tianhaishun/app-coldstart.git
#   cd app-coldstart
#   bash scripts/build-mac.sh
#
# 脚本会自动：
#   1. 检查 / 安装 Homebrew 依赖（Node.js、Python、scrcpy、iOS 工具链）
#   2. npm install
#   3. electron-builder 打包 .dmg
#   4. 输出文件位置
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

echo -e "${BOLD}═══════════════════════════════════════════════${RESET}"
echo -e "${BOLD}  App 冷启测速 — Mac 打包${RESET}"
echo -e "${BOLD}═══════════════════════════════════════════════${RESET}"
echo ""

# ── 1. 检查 Homebrew ──────────────────────────────────────────────────────────
info "检查 Homebrew..."
if ! command -v brew &>/dev/null; then
    warn "Homebrew 未安装，正在安装..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Apple Silicon 需要加到 PATH
    if [[ -f /opt/homebrew/bin/brew ]]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    fi
fi
echo "  Homebrew: $(brew --version | head -1)"

# ── 2. 安装依赖 ────────────────────────────────────────────────────────────────
info "安装构建依赖..."

# Node.js
if ! command -v node &>/dev/null; then
    echo "  安装 Node.js..."
    brew install node
fi
echo "  Node.js: $(node --version)"

# Python
if ! command -v python3 &>/dev/null; then
    echo "  安装 Python..."
    brew install python@3.11
fi
echo "  Python: $(python3 --version)"

# scrcpy（镜像/录屏用）
if ! command -v scrcpy &>/dev/null; then
    echo "  安装 scrcpy..."
    brew install scrcpy
fi
echo "  scrcpy: $(scrcpy --version 2>&1 | head -1)"

# Android platform tools（提供 adb）
if ! command -v adb &>/dev/null; then
    echo "  安装 android-platform-tools..."
    brew install --cask android-platform-tools
fi
echo "  adb: $(adb --version 2>&1 | head -1)"

# iOS 工具链
if ! command -v idevice_id &>/dev/null; then
    echo "  安装 libimobiledevice..."
    brew install libimobiledevice
fi
if ! command -v ideviceinstaller &>/dev/null; then
    echo "  安装 ideviceinstaller..."
    brew install ideviceinstaller
fi
echo "  idevice_id: OK"

# ── 3. npm install ────────────────────────────────────────────────────────────
info "安装 npm 依赖..."
npm install
echo "  npm 依赖安装完成"

# ── 4. 打包 ────────────────────────────────────────────────────────────────────
info "开始打包 .dmg（约 2-3 分钟）..."
echo ""

export CSC_IDENTITY_AUTO_DISCOVERY=false
npm run build:mac

# ── 5. 输出结果 ────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}═══════════════════════════════════════════════${RESET}"
echo -e "${GREEN}${BOLD}  打包完成！${RESET}"
echo -e "${BOLD}═══════════════════════════════════════════════${RESET}"
echo ""

# 找到 .dmg 文件
DMG=$(find release -name "*.dmg" -type f 2>/dev/null | head -1)
if [[ -n "$DMG" ]]; then
    SIZE=$(du -h "$DMG" | cut -f1)
    echo -e "  安装包: ${GREEN}${DMG}${RESET} (${SIZE})"
    echo ""
    echo "  在 Finder 中打开："
    echo "    open release/"
    echo ""
    echo "  首次打开 .dmg 安装后可能需要："
    echo "    xattr -cr /Applications/AppColdStart.app"
else
    warn "未找到 .dmg 文件，请检查 release/ 目录"
    ls -la release/ 2>/dev/null
fi
