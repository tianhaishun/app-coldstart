#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
# App 冷启测速 — macOS 一键启动（Electron 客户端）
#
# 职责（与 CLAUDE.md 约定一致）：
#   brew 补齐缺失依赖（node / python3 / android-platform-tools / libimobiledevice /
#   scrcpy）+ scrcpy 符号链接 + npm install + 启动客户端。
#
# 原则：只补缺失、不破坏既有环境；安装来源均为 brew 官方 formula。
#
# 用法：
#   双击 Start-Mac.command（入口壳）
#   或终端：bash start-mac.sh
# ══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"

step() { echo; echo "[==] $*"; }
ok()   { echo "  [OK] $*"; }
skip() { echo "  [SKIP] $*"; }
fail() {
  echo; echo "[FAIL] $*"
  echo "  请保留上方输出，反馈给维护者。"
  exit 1
}

# ── 1. Homebrew ───────────────────────────────────────────────────────────────
step "检查 Homebrew ..."
if ! command -v brew >/dev/null 2>&1; then
  fail "未找到 brew。请先安装（官方）：https://brew.sh"
fi
ok "brew $(brew --version | head -1)"

# ── 2. 依赖（只装缺失）───────────────────────────────────────────────────────
step "检查依赖（只补缺失）..."
ensure_brew() {  # $1=命令名 $2=formula
  if command -v "$1" >/dev/null 2>&1; then
    ok "$1 已安装"
  else
    echo "  安装 $2 ..."
    brew install "$2" || fail "brew install $2 失败"
  fi
}
ensure_brew node      node
ensure_brew python3   python3
ensure_brew adb       android-platform-tools
ensure_brew idevice_id libimobiledevice
ensure_brew scrcpy    scrcpy

# ── 3. scrcpy 符号链接（对齐打包版目录结构，scrcpy-manager 优先读 ROOT/scrcpy）──
step "scrcpy 符号链接 ..."
SCRCPY_BIN="$(command -v scrcpy || true)"
if [ -n "$SCRCPY_BIN" ]; then
  mkdir -p "$ROOT/scrcpy"
  if [ ! -e "$ROOT/scrcpy/scrcpy" ]; then
    ln -s "$SCRCPY_BIN" "$ROOT/scrcpy/scrcpy"
    ok "scrcpy -> $SCRCPY_BIN"
  else
    skip "scrcpy 链接已存在"
  fi
  # brew 版 scrcpy-server 在 share 目录，链路后录屏/镜像才能对齐打包版行为
  SERVER_PATH="$(brew --prefix)/share/scrcpy/scrcpy-server"
  if [ -f "$SERVER_PATH" ] && [ ! -e "$ROOT/scrcpy/scrcpy-server" ]; then
    ln -s "$SERVER_PATH" "$ROOT/scrcpy/scrcpy-server"
    ok "scrcpy-server -> $SERVER_PATH"
  fi
else
  echo "  [WARN] scrcpy 未安装，镜像/录屏不可用（测速本身不受影响）"
fi

# ── 4. npm 依赖 ───────────────────────────────────────────────────────────────
step "npm 依赖 ..."
if [ -d "$ROOT/node_modules/electron" ]; then
  skip "node_modules/electron 已存在"
else
  echo "  首次运行：npm install（约 1-3 分钟）..."
  (cd "$ROOT" && npm install --prefer-offline --no-audit --no-fund) || fail "npm install 失败"
  ok "npm 依赖安装完成"
fi

# ── 5. 启动客户端 ─────────────────────────────────────────────────────────────
step "启动客户端 ..."
echo "  启动 Electron（首次启动会自动建 .venv / 复用内嵌运行时，请耐心等待）..."
(cd "$ROOT" && npm start) || fail "客户端退出异常"
