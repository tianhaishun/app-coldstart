#!/bin/bash
# ══════════════════════════════════════════════════════════════════════════════
# start-mac.sh — Mac 一键启动脚本（启动 Electron 桌面客户端）
#
# 用法：
#   推荐：Finder 中双击 Start-Mac.command
#   终端：bash start-mac.sh
#
# 负责检测 / 安装当前客户端所需的外部依赖：
#   Homebrew、Node.js/npm、Python 3.10+、Android platform-tools、
#   libimobiledevice / ideviceinstaller、scrcpy，以及 npm/Python 依赖。
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
RUNTIME="$ROOT/.runtime"
VENV="$ROOT/.venv"

# Finder 双击 .command 时 PATH 可能没有加载用户的 shell 配置；
# 先探测两个 Homebrew 架构的固定路径，再决定是否安装。
BREW=""
for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if [[ -x "$candidate" ]]; then
        BREW="$candidate"
        break
    fi
done
if [[ -z "$BREW" ]] && command -v brew >/dev/null 2>&1; then
    BREW="$(command -v brew)"
fi

if [[ -n "$BREW" ]]; then
    eval "$("$BREW" shellenv)"
fi

echo -e "${BOLD}═══════════════════════════════════════════════${RESET}"
echo -e "${BOLD}  App 冷启测速 — Mac 一键启动${RESET}"
echo -e "${BOLD}═══════════════════════════════════════════════${RESET}"
echo ""

# ── 1. Homebrew ───────────────────────────────────────────────────────────────
info "检查 Homebrew..."
if ! command -v brew >/dev/null 2>&1; then
    warn "Homebrew 未安装，正在安装（首次可能需要 5-10 分钟和管理员授权）..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    BREW=""
    for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
        if [[ -x "$candidate" ]]; then
            BREW="$candidate"
            break
        fi
    done
    if [[ -z "$BREW" ]]; then
        fail "Homebrew 安装完成但找不到 brew，请重新打开 Terminal 后再试"
    fi
    eval "$("$BREW" shellenv)"
fi
echo "  Homebrew: $(brew --version | head -1)"

# ── 2. Node.js / npm（Electron 客户端必需）─────────────────────────────────────
NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || true)"
if [[ -z "$NODE_MAJOR" || "$NODE_MAJOR" -lt 18 ]] || ! command -v npm >/dev/null 2>&1; then
    info "未检测到可用 Node.js/npm，安装或更新 Homebrew Node.js..."
    if brew list --formula node >/dev/null 2>&1; then
        brew upgrade node || brew install node
    else
        brew install node
    fi
fi
if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
    fail "Node.js/npm 安装完成但仍不可用，请检查 Homebrew PATH"
fi
NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || true)"
if [[ -z "$NODE_MAJOR" || "$NODE_MAJOR" -lt 18 ]]; then
    fail "Node.js 版本过低（当前 $(node --version 2>/dev/null || echo unknown)，需要 18+）"
fi
echo "  Node.js: $(node --version)"
echo "  npm:     $(npm --version)"

# ── 3. Python 3.10+（后端和 venv 必需）─────────────────────────────────────────
PYTHON_BIN=""
if command -v python3 >/dev/null 2>&1; then
    PYTHON_CANDIDATE="$(command -v python3)"
    if "$PYTHON_CANDIDATE" -c 'import sys; raise SystemExit(0 if (3, 10) <= sys.version_info < (3, 12) else 1)' >/dev/null 2>&1; then
        PYTHON_BIN="$PYTHON_CANDIDATE"
    fi
fi
if [[ -z "$PYTHON_BIN" ]]; then
    info "未检测到可用 Python 3.10+，安装 Homebrew Python 3.11..."
    brew install python@3.11
    PYTHON_PREFIX="$(brew --prefix python@3.11)"
    PYTHON_BIN="$PYTHON_PREFIX/bin/python3.11"
fi
if [[ ! -x "$PYTHON_BIN" ]]; then
    fail "Python 安装完成但不可用：$PYTHON_BIN"
fi
echo "  Python:  $($PYTHON_BIN --version 2>&1)"

# ── 4. Android platform-tools（adb 必需）──────────────────────────────────────
if ! command -v adb >/dev/null 2>&1; then
    info "未检测到 adb，安装 Homebrew android-platform-tools..."
    if ! brew install --cask android-platform-tools; then
        fail "android-platform-tools 安装失败；adb 是核心依赖，无法启动客户端"
    fi
fi
if ! command -v adb >/dev/null 2>&1; then
    fail "android-platform-tools 安装完成但 adb 仍不可用，请检查 Homebrew PATH"
fi
echo "  adb:     $(adb --version 2>&1 | head -1)"

# ── 5. iOS 工具链（安装失败不阻断 Android 核心测速）────────────────────────────
if ! command -v idevice_id >/dev/null 2>&1; then
    info "未检测到 idevice_id，安装 Homebrew libimobiledevice..."
    if ! brew install libimobiledevice; then
        warn "libimobiledevice 安装失败；Android 测速不受影响，iOS 设备功能将不可用"
    fi
fi
if ! command -v ideviceinstaller >/dev/null 2>&1; then
    info "未检测到 ideviceinstaller，安装 Homebrew ideviceinstaller..."
    if ! brew install ideviceinstaller; then
        warn "ideviceinstaller 安装失败；iOS 安装/卸载功能将不可用"
    fi
fi
if command -v idevice_id >/dev/null 2>&1; then
    echo "  idevice_id: $(command -v idevice_id)"
else
    warn "iOS 工具链不可用；Android 测速不受影响，iOS 设备功能将不可用"
fi

# ── 6. scrcpy（镜像/录屏；安装失败不阻断核心测速）──────────────────────────────
if ! command -v scrcpy >/dev/null 2>&1; then
    info "未检测到 scrcpy，安装 Homebrew scrcpy..."
    if ! brew install scrcpy; then
        warn "scrcpy 安装失败；镜像/录屏将不可用，不影响核心测速"
    fi
fi
if command -v scrcpy >/dev/null 2>&1; then
    echo "  scrcpy:  $(scrcpy --version 2>&1 | head -1)"
else
    warn "scrcpy 不可用；镜像/录屏按钮将隐藏，不影响核心测速"
fi

# ── 7. 将 brew scrcpy 链接到客户端约定目录 ─────────────────────────────────────
if command -v scrcpy >/dev/null 2>&1; then
    info "检查 scrcpy 客户端资源..."
    SCRCPY_DIR="$ROOT/scrcpy"
    mkdir -p "$SCRCPY_DIR"
    SCRCPY_BIN="$(command -v scrcpy)"
    SCRCPY_PREFIX="$(brew --prefix scrcpy 2>/dev/null || true)"
    SERVER_CANDIDATES=(
        "$SCRCPY_PREFIX/share/scrcpy/scrcpy-server"
        "$(brew --prefix)/share/scrcpy/scrcpy-server"
    )

    if [[ -L "$SCRCPY_DIR/scrcpy" ]]; then
        rm -f "$SCRCPY_DIR/scrcpy"
    fi
    if [[ ! -e "$SCRCPY_DIR/scrcpy" ]]; then
        ln -s "$SCRCPY_BIN" "$SCRCPY_DIR/scrcpy"
    elif [[ ! -x "$SCRCPY_DIR/scrcpy" ]]; then
        warn "scrcpy/ 中已有不可执行文件，未覆盖：$SCRCPY_DIR/scrcpy"
    fi

    # A stale/broken symlink is removed above and recreated from the current brew path.

    SERVER_SRC=""
    for candidate in "${SERVER_CANDIDATES[@]}"; do
        if [[ -f "$candidate" ]]; then
            SERVER_SRC="$candidate"
            break
        fi
    done
    if [[ -n "$SERVER_SRC" ]]; then
        if [[ -L "$SCRCPY_DIR/scrcpy-server" ]]; then
            rm -f "$SCRCPY_DIR/scrcpy-server"
        elif [[ -e "$SCRCPY_DIR/scrcpy-server" ]]; then
            warn "scrcpy/ 中已有 scrcpy-server 文件，未覆盖"
        else
            ln -s "$SERVER_SRC" "$SCRCPY_DIR/scrcpy-server"
        fi
    else
        warn "未找到 scrcpy-server；镜像/录屏可能不可用，不影响核心测速"
    fi
fi

# ── 8. Python venv 与依赖（脚本负责，不把首次安装隐藏在客户端闪屏）──────────────
export CST_PYTHON="$PYTHON_BIN"
VENV_PYTHON="$VENV/bin/python"
if [[ ! -x "$VENV_PYTHON" ]]; then
    info "创建 Python 虚拟环境..."
    "$PYTHON_BIN" -m venv "$VENV"
fi
if [[ ! -x "$VENV_PYTHON" ]]; then
    fail "Python 虚拟环境创建失败：$VENV"
fi
DEPS_MARKER="$RUNTIME/python-deps.ready"
if [[ ! -f "$DEPS_MARKER" || "$ROOT/requirements.txt" -nt "$DEPS_MARKER" ]]; then
    info "安装 Python 依赖（首次可能需要 1-3 分钟）..."
    "$VENV_PYTHON" -m pip install -r "$ROOT/requirements.txt"
    mkdir -p "$RUNTIME"
    printf '%s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" > "$DEPS_MARKER"
else
    info "Python 依赖已就绪"
fi

# ── 9. npm 依赖 ────────────────────────────────────────────────────────────────
if [[ ! -e "$ROOT/node_modules/.bin/electron" ]]; then
    info "安装 npm 依赖（Electron，首次可能需要 1-3 分钟）..."
    (cd "$ROOT" && npm install --no-audit --no-fund)
fi
if [[ ! -e "$ROOT/node_modules/.bin/electron" ]]; then
    fail "npm 依赖安装完成但 Electron 不存在：$ROOT/node_modules/.bin/electron"
fi

# ── 10. 启动桌面客户端 ─────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}═══════════════════════════════════════════════${RESET}"
info "启动 App 冷启测速客户端..."
echo "  关闭客户端窗口即退出，后端和 adb 会自动清理。"
echo ""
cd "$ROOT"
exec npm start
