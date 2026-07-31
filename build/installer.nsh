; ── NSIS 安装器自定义脚本 ────────────────────────────────────
; 1. 安装前：清理残留的 adb / scrcpy 进程（防文件锁）
; 2. 安装后：预热 Python OCR 引擎（首次启动秒开）
;
; electron-builder 会在 NSIS 脚本的对应位置 !include 本文件。

!macro customInit
  ; 静默杀掉可能残留的进程（/F 强制 /T 含子进程），忽略错误
  nsExec::ExecToLog 'taskkill /F /T /IM adb.exe'
  Pop $0

  nsExec::ExecToLog 'taskkill /F /T /IM scrcpy.exe'
  Pop $0

  Sleep 1000
!macroend

!macro customInstall
  ; ── 文件复制完成后，分步预热 Python 运行时 ──
  ; 每步前面 DetailPrint 一行说明，用户能在安装窗口实时看到进度
  SetDetailsPrint listonly

  DetailPrint "──────────────────────────────"
  DetailPrint "正在初始化运行时环境..."
  DetailPrint "──────────────────────────────"

  ; Step 1: 核心库
  DetailPrint "[1/4] 加载核心库 (FastAPI / uvicorn / OpenCV / NumPy)..."
  nsExec::ExecToLog '"$INSTDIR\resources\python-embed\python.exe" -c "import fastapi; import uvicorn; import cv2; import numpy; import PIL"'
  Pop $0

  ; Step 2: OCR 引擎（最慢，加载 ONNX 模型）
  DetailPrint "[2/4] 初始化 OCR 引擎 (加载 ONNX 模型，约 10-20 秒)..."
  nsExec::ExecToLog '"$INSTDIR\resources\python-embed\python.exe" -c "from rapidocr_onnxruntime import RapidOCR; RapidOCR()"'
  Pop $0

  ; Step 3: iOS 工具链
  DetailPrint "[3/4] 加载 iOS 工具链 (pymobiledevice3)..."
  nsExec::ExecToLog '"$INSTDIR\resources\python-embed\python.exe" -c "import pymobiledevice3"'
  Pop $0

  ; Step 4: 完成确认
  DetailPrint "[4/4] 验证后端可启动..."
  nsExec::ExecToLog '"$INSTDIR\resources\python-embed\python.exe" -c "import uvicorn"'
  Pop $0

  DetailPrint "──────────────────────────────"
  DetailPrint "初始化完成！首次启动将秒开。"
  DetailPrint "──────────────────────────────"

  SetDetailsPrint lastused
!macroend
