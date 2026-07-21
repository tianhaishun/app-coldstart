#!/usr/bin/env bash
# 安装 pre-commit hook 到 .git/hooks/（Git Bash / Linux / macOS 通用）
# 用法：bash hooks/install.sh
#
# 策略：拷贝（不用软链）。Windows 软链需管理员权限，拷贝零依赖、最稳。
# 缺点：hooks/pre-commit 改动后需重跑此脚本同步。已在文件头注明。

set -euo pipefail
REPO_ROOT="$(git rev-parse --show-toplevel)"
SRC="$REPO_ROOT/hooks/pre-commit"
DST="$REPO_ROOT/.git/hooks/pre-commit"

if [ ! -f "$SRC" ]; then
    echo "❌ 找不到 $SRC" >&2
    exit 1
fi

# 备份已有的非示例 hook
if [ -f "$DST" ] && ! grep -q "pre-commit 钩子（仓库内版本" "$DST" 2>/dev/null; then
    cp "$DST" "$DST.bak.$(date +%s)"
    echo "ℹ 已备份原 hook 到 $DST.bak.*"
fi

cp "$SRC" "$DST"
chmod +x "$DST"
echo "✓ pre-commit hook 已安装到 $DST"
echo "  （改 hooks/pre-commit 后需重跑 bash hooks/install.sh 同步）"
