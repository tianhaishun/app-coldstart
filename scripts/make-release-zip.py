#!/usr/bin/env python3
# ──────────────────────────────────────────────────────────────────────────────
# 打包发布 ZIP：安装包 + 安装说明 + Word 发布说明。
#
# 只包含必要文件，不带工程源码。
#
# 用法：
#   python scripts/make-release-zip.py
# ──────────────────────────────────────────────────────────────────────────────

import json
import sys
import zipfile
from pathlib import Path
from datetime import date

ROOT = Path(__file__).resolve().parent.parent
PKG_PATH = ROOT / "package.json"
RELEASE_DIR = ROOT / "release"
PUBLISH_DIR = ROOT / "publish"


def main():
    # 读取版本号
    with open(PKG_PATH, "r", encoding="utf-8") as f:
        pkg = json.load(f)
    version = pkg["version"]
    product_name = pkg.get("build", {}).get("productName", "AppColdStart")

    # 找安装包
    setup_files = list(RELEASE_DIR.glob("*-setup.exe"))
    if not setup_files:
        print("[FAIL] 未找到安装包（release/*-setup.exe）")
        sys.exit(1)
    setup_file = setup_files[0]

    # 发布说明
    publish_version_dir = PUBLISH_DIR / f"v{version}"
    docx_file = publish_version_dir / f"发布说明-v{version}.docx"

    # 输出 ZIP
    zip_name = f"{product_name}-v{version}.zip"
    zip_path = PUBLISH_DIR / zip_name

    # 安装说明
    readme_content = f"""App 冷启测速 v{version} — 安装说明
{'='*50}

一、系统要求
  - Windows 10 / 11（64 位）
  - 无需预先安装 Python（已内置运行时）
  - USB 数据线（连接手机）

二、安装步骤
  1. 双击 {setup_file.name}
  2. 按安装向导完成安装（可选安装目录）
  3. 安装完成后，桌面 / 开始菜单出现 AppColdStart 图标
  4. 双击图标启动

三、首次启动
  - 首次启动需几秒钟初始化 OCR 引擎
  - 将手机用 USB 连接电脑
  - Android：开启 USB 调试
  - iOS：解锁手机并点「信任此电脑」

四、详细说明
  详见「发布说明-v{version}.docx」

五、问题反馈
  作者：{pkg.get('author', '')}
  日期：{date.today().strftime('%Y-%m-%d')}
"""

    # 创建 ZIP
    PUBLISH_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[ZIP] 打包到 {zip_path} ...")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        # 安装包
        print(f"  + {setup_file.name} ({setup_file.stat().st_size / (1024*1024):.0f} MB)")
        zf.write(setup_file, setup_file.name)

        # 安装说明
        zf.writestr("安装说明.txt", readme_content)
        print("  + 安装说明.txt")

        # Word 发布说明
        if docx_file.exists():
            zf.write(docx_file, docx_file.name)
            print(f"  + {docx_file.name}")

    zip_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"\n✅ ZIP 打包完成: {zip_path}")
    print(f"   体积: {zip_mb:.0f} MB")


if __name__ == "__main__":
    main()
