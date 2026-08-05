#!/bin/bash
# ══════════════════════════════════════════════════════════════════════════════
# Start-Mac.command — Mac 双击启动入口（Finder 双击 → Terminal 执行）
# 等价于终端执行：bash start-mac.sh
#
# 说明：
#   - 双击前需有执行权限（git clone 自带的权限即可；若手动拷贝过文件，
#     先在终端执行一次：chmod +x Start-Mac.command）
#   - 全部逻辑在 start-mac.sh 中（单一来源，本文件只是入口壳）
#   - 启动失败时窗口停留 10 秒显示错误信息，避免一闪而过
# ══════════════════════════════════════════════════════════════════════════════

cd "$(dirname "$0")" || exit 1

bash start-mac.sh
status=$?

if [ $status -ne 0 ]; then
    echo ""
    echo "[FAIL] 启动失败（退出码 $status）。窗口将在 10 秒后自动关闭，"
    echo "       请截图或复制上方错误信息；也可在终端里执行 bash start-mac.sh 查看完整输出。"
    sleep 10
fi

exit $status
