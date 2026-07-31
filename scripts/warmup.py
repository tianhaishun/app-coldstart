"""
安装时预热脚本 —— 在 NSIS 安装流程中执行。

预加载所有重量级 Python 模块 + 初始化 OCR 引擎，
这样首次启动应用时无需再等模型加载。

由 build/installer.nsh 调用：
  python-embed\python.exe scripts\warmup.py
"""

import sys
import time

steps = [
    ("加载 FastAPI", "fastapi"),
    ("加载 uvicorn", "uvicorn"),
    ("加载 OpenCV", "cv2"),
    ("加载 NumPy", "numpy"),
    ("加载图像处理", "PIL"),
    ("加载 iOS 工具链", "pymobiledevice3"),
]

for label, module in steps:
    print(f"  初始化 {label}...", flush=True)
    try:
        __import__(module)
    except Exception as e:
        print(f"    跳过（{e}）", flush=True)

# OCR 引擎是最大的初始化开销（加载 ONNX 模型）
print("  初始化 OCR 引擎（RapidOCR ONNX）...", flush=True)
try:
    from rapidocr_onnxruntime import RapidOCR
    RapidOCR()  # 触发模型加载和缓存
    print("  OCR 引擎就绪", flush=True)
except Exception as e:
    print(f"  OCR 跳过（{e}）", flush=True)

print("\n预热完成！", flush=True)
