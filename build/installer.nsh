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
  ; 文件复制完成后，预热 Python 运行时
  ; 加载 OCR ONNX 模型等重量级模块，约需 10-30 秒
  ; 这样首次启动应用时无需再等待
  DetailPrint "正在初始化 Python OCR 引擎（请稍候，约 10-30 秒）..."
  SetDetailsPrint listonly

  nsExec::ExecToLog '"$INSTDIR\resources\python-embed\python.exe" "$INSTDIR\resources\backend\scripts\warmup.py"'
  Pop $0

  ${If} $0 == 0
    DetailPrint "Python 引擎初始化完成"
  ${Else}
    DetailPrint "Python 引擎初始化跳过（非致命）"
  ${EndIf}

  SetDetailsPrint lastused
!macroend
