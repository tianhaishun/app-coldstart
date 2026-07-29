; ── NSIS 安装器自定义脚本 ────────────────────────────────────
; 借鉴 XYLog Viewer：安装前清理残留的 adb / scrcpy 进程，
; 防止"文件被占用无法覆盖"导致升级失败。
;
; electron-builder 会在 NSIS 脚本的对应位置 !include 本文件。
; customInit 宏在安装器初始化时执行（文件复制之前）。

!macro customInit
  ; 静默杀掉可能残留的进程（/F 强制 /T 含子进程），忽略错误
  ; adb.exe — 后端 Python 退出后 adb daemon 可能残留
  nsExec::ExecToLog 'taskkill /F /T /IM adb.exe'
  Pop $0  ; 丢弃返回值（进程不存在时 taskkill 返回非零，正常）

  ; scrcpy.exe — 镜像/录屏子进程可能残留
  nsExec::ExecToLog 'taskkill /F /T /IM scrcpy.exe'
  Pop $0

  ; 短暂等待文件句柄释放（1000ms）
  Sleep 1000
!macroend
