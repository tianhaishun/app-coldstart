#!/usr/bin/env python3
# ──────────────────────────────────────────────────────────────────────────────
# 构建嵌入式 Python 运行时（Windows AMD64）。
#
# 流程：
#   1. 下载 Python 3.11 embeddable 包（~10MB）到 python-embed/
#   2. 启用 site-packages（修改 python311._pth）
#   3. 安装 pip（get-pip.py）
#   4. pip install -r requirements.txt（所有运行时依赖）
#   5. 编译 server.py → server.pyc（源码保护）
#   6. 清理不必要的文件（__pycache__、测试、文档）
#
# 产出：python-embed/ 目录，可直接打包进 electron-builder extraResources。
# 用户安装后无需装 Python、无需联网 pip install。
#
# 用法：
#   python scripts/build-python-embed.py          # 构建到 python-embed/
#   python scripts/build-python-embed.py --force   # 强制重建
# ──────────────────────────────────────────────────────────────────────────────

import sys
import os
import shutil
import zipfile
import subprocess
import argparse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EMBED_DIR = ROOT / "python-embed"
REQUIREMENTS = ROOT / "requirements.txt"
SERVER_PY = ROOT / "server.py"

# Python 版本：与 venv 保持一致
PY_VERSION = "3.11.9"  # embeddable 可能没有 3.11.15，用最新稳定 3.11.x
PY_VERSIONS_FALLBACK = ["3.11.9", "3.11.8", "3.11.7", "3.11.6", "3.11.5"]

EMBED_URL_TEMPLATE = "https://www.python.org/ftp/python/{ver}/python-{ver}-embed-amd64.zip"
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"


def download(url, dest):
    """下载文件，带进度提示"""
    print(f"  下载: {url}")
    urllib.request.urlretrieve(url, dest)
    size_mb = Path(dest).stat().st_size / (1024 * 1024)
    print(f"  完成: {dest} ({size_mb:.1f} MB)")


def try_download_embed(target_zip):
    """尝试多个版本 URL，直到成功"""
    # 先试指定版本
    versions = [PY_VERSION] + [v for v in PY_VERSIONS_FALLBACK if v != PY_VERSION]
    for ver in versions:
        url = EMBED_URL_TEMPLATE.format(ver=ver)
        try:
            print(f"  尝试 Python {ver}...")
            download(url, target_zip)
            return ver
        except Exception as e:
            print(f"  失败: {e}")
            if target_zip.exists():
                target_zip.unlink()
            continue
    raise RuntimeError("所有 Python 版本下载均失败，请检查网络或手动下载")


def main():
    parser = argparse.ArgumentParser(description="构建嵌入式 Python 运行时")
    parser.add_argument("--force", action="store_true", help="强制重建（删除已有的 python-embed/）")
    args = parser.parse_args()

    # ── 检查是否已构建 ──
    embed_python = EMBED_DIR / "python.exe"
    embed_pip = EMBED_DIR / "Scripts" / "pip.exe"
    if embed_python.exists() and embed_pip.exists() and not args.force:
        print("[SKIP] python-embed/ 已存在且包含 pip，跳过构建。")
        print("       如需重建：python scripts/build-python-embed.py --force")
        return

    # ── 清理旧目录 ──
    if EMBED_DIR.exists():
        print("[CLEAN] 删除旧的 python-embed/ ...")
        shutil.rmtree(EMBED_DIR, ignore_errors=True)

    EMBED_DIR.mkdir(parents=True)

    # ══════════════════════════════════════════════════════════════════════════
    # 1. 下载 Python embeddable
    # ══════════════════════════════════════════════════════════════════════════
    print("\n[1/6] 下载 Python embeddable ...")
    zip_path = ROOT / "python-embed-download.zip"
    actual_version = try_download_embed(zip_path)

    print("  解压...")
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(EMBED_DIR)
    zip_path.unlink()
    print(f"  解压完成: Python {actual_version}")

    # ══════════════════════════════════════════════════════════════════════════
    # 2. 启用 site-packages
    # ══════════════════════════════════════════════════════════════════════════
    print("\n[2/6] 启用 site-packages ...")
    # embeddable 默认有 python311._pth，其中 import site 被注释掉了
    # 取消注释才能让 pip 和 site-packages 正常工作
    pth_files = list(EMBED_DIR.glob("python*._pth"))
    if pth_files:
        pth = pth_files[0]
        content = pth.read_text(encoding="utf-8")
        # 取消 import site 的注释
        content = content.replace("#import site", "import site")
        # 添加 Lib/site-packages 到搜索路径
        if "Lib/site-packages" not in content:
            content += "\nLib/site-packages\n"
        # 添加当前目录（让 server.pyc 能被 import）
        if "." not in content:
            content += ".\n"
        pth.write_text(content, encoding="utf-8")
        print(f"  修改: {pth.name}")
    else:
        print("  [WARN] 未找到 python*._pth，跳过")

    # ══════════════════════════════════════════════════════════════════════════
    # 3. 安装 pip
    # ══════════════════════════════════════════════════════════════════════════
    print("\n[3/6] 安装 pip ...")
    get_pip = EMBED_DIR / "get-pip.py"
    download(GET_PIP_URL, get_pip)

    result = subprocess.run(
        [str(embed_python), "get-pip.py", "--no-warn-script-location"],
        cwd=str(EMBED_DIR),
        capture_output=True, text=True,
    )
    get_pip.unlink()  # 清理 get-pip.py
    if result.returncode != 0:
        print(f"  [FAIL] pip 安装失败:\n{result.stderr}")
        sys.exit(1)
    print("  pip 安装完成")

    # ══════════════════════════════════════════════════════════════════════════
    # 4. 安装运行时依赖
    # ══════════════════════════════════════════════════════════════════════════
    print("\n[4/6] 安装运行时依赖（可能需要几分钟）...")
    pip_exe = EMBED_DIR / "Scripts" / "pip.exe"
    result = subprocess.run(
        [str(pip_exe), "install", "-r", str(REQUIREMENTS), "--no-warn-script-location"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  [FAIL] 依赖安装失败:\n{result.stderr[-2000:]}")
        sys.exit(1)
    # 只显示最后几行进度
    lines = result.stdout.strip().split("\n")
    for line in lines[-5:]:
        print(f"  {line}")
    print("  依赖安装完成")

    # ══════════════════════════════════════════════════════════════════════════
    # 5. 编译 server.py → server.pyc（源码保护）
    # ══════════════════════════════════════════════════════════════════════════
    print("\n[5/6] 编译 server.py → server.pyc ...")
    # 用 embed Python 编译，确保字节码版本匹配
    # -b 标志：生成平铺 .pyc（不放在 __pycache__），uvicorn 可直接 import
    result = subprocess.run(
        [str(embed_python), "-m", "compileall", "-b", str(SERVER_PY)],
        capture_output=True, text=True,
        cwd=str(ROOT),
    )
    server_pyc = ROOT / "server.pyc"
    if not server_pyc.exists():
        # compileall 可能把 .pyc 放到 __pycache__
        pycache_pyc = ROOT / "__pycache__" / "server.cpython-311.pyc"
        if pycache_pyc.exists():
            shutil.move(str(pycache_pyc), str(server_pyc))
            # 清理空的 __pycache__
            pycache_dir = ROOT / "__pycache__"
            if pycache_dir.exists() and not any(pycache_dir.iterdir()):
                pycache_dir.rmdir()

    if server_pyc.exists():
        size_kb = server_pyc.stat().st_size / 1024
        print(f"  server.pyc 已生成 ({size_kb:.0f} KB)")
    else:
        print("  [WARN] server.pyc 未生成，将保留 server.py 明文")
        server_pyc = None

    # ══════════════════════════════════════════════════════════════════════════
    # 6. 清理不必要的文件（减小体积）
    # ══════════════════════════════════════════════════════════════════════════
    print("\n[6/6] 清理不必要的文件 ...")
    cleaned = 0

    # ── 删除运行时不需要的开发/CLI 依赖 ──
    # 这些是 pymobiledevice3 CLI 和 IPython 拉进来的，server.py 库模式不用
    removable_packages = [
        "jedi", "parso",                          # IDE 自动补全（25MB）
        "IPython", "ipython_pygments_lexers",     # 交互式 shell（5MB）
        "stack_data", "pure_eval", "matplotlib_inline",  # IPython 依赖
        "xonsh",                                   # shell（8MB）
        "pythonwin", "pyreadline3",               # pywin32 GUI / readline
        "av", "av.libs",                           # PyAV FFmpeg 绑定（63MB！）
        "inquirer3", "readchar", "blessed",        # CLI 交互提示
        "coloredlogs",                             # 彩色日志
        "humanfriendly",                           # coloredlogs 依赖
        "tqdm",                                    # 进度条
    ]
    sp_dir = EMBED_DIR / "Lib" / "site-packages"
    saved_bytes = 0
    for pkg in removable_packages:
        # 包目录（尝试多种命名：- 和 _）
        for name in [pkg, pkg.replace("-", "_")]:
            pkg_path = sp_dir / name
            if pkg_path.exists():
                pkg_size = sum(f.stat().st_size for f in pkg_path.rglob("*") if f.is_file())
                shutil.rmtree(pkg_path, ignore_errors=True)
                saved_bytes += pkg_size
                cleaned += 1
        # dist-info
        for di in sp_dir.glob(f"{pkg}*"):
            if di.is_dir() and ".dist-info" in str(di):
                shutil.rmtree(di, ignore_errors=True)
                cleaned += 1
    if saved_bytes > 0:
        print(f"  删除开发/CLI 包: 节省 {saved_bytes / (1024*1024):.0f} MB")

    # ── 删除 __pycache__（非 site-packages 的）──
    for pycache in EMBED_DIR.rglob("__pycache__"):
        if "site-packages" in str(pycache):
            continue
        shutil.rmtree(pycache, ignore_errors=True)
        cleaned += 1

    # ── 删除测试目录 ──
    for pattern in ["**/tests", "**/test"]:
        for test_dir in EMBED_DIR.glob(pattern):
            if test_dir.is_dir():
                shutil.rmtree(test_dir, ignore_errors=True)
                cleaned += 1

    # ── 删除 .dist-info 中的多余文件（保留 METADATA / RECORD / WHEEL）──
    for dist_info in sp_dir.glob("*.dist-info"):
        for f in dist_info.glob("*"):
            if f.name not in ("RECORD", "INSTALLER", "METADATA", "WHEEL"):
                try:
                    if f.is_dir():
                        shutil.rmtree(f, ignore_errors=True)
                    else:
                        f.unlink(missing_ok=True)
                    cleaned += 1
                except PermissionError:
                    pass

    # ── 删除 pip 和 setuptools（运行时不需要）──
    # 注意：如果后续需要往 embed 装包，需重新运行 get-pip.py
    for pkg in ["pip", "setuptools", "wheel"]:
        pkg_dir = EMBED_DIR / "Lib" / "site-packages" / pkg
        if pkg_dir.exists():
            shutil.rmtree(pkg_dir, ignore_errors=True)
            cleaned += 1
        # 删除对应的 .dist-info
        for di in (EMBED_DIR / "Lib" / "site-packages").glob(f"{pkg}*"):
            if di.is_dir() and ".dist-info" in str(di):
                shutil.rmtree(di, ignore_errors=True)
        # 删除 Scripts 里的 pip/setuptools 命令
        scripts_dir = EMBED_DIR / "Scripts"
        if scripts_dir.exists():
            for script in scripts_dir.glob(f"{pkg}*"):
                script.unlink(missing_ok=True)

    print(f"  清理了 {cleaned} 项")

    # ── 统计体积 ──
    total_size = sum(f.stat().st_size for f in EMBED_DIR.rglob("*") if f.is_file())
    print(f"\n{'='*50}")
    print(f"✅ 嵌入式 Python 构建完成")
    print(f"   版本: Python {actual_version} (AMD64)")
    print(f"   体积: {total_size / (1024*1024):.0f} MB")
    print(f"   路径: {EMBED_DIR}")
    if server_pyc:
        print(f"   源码: server.py → server.pyc（字节码保护）")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
