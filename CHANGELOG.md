# 变更记录（CHANGELOG）

本文件记录项目各版本的功能变更与修复，按版本倒序。版本遵循语义化版本规范。

---

## v2.1.0（开发中）— 2026-08

### 新增
- **一键启动脚本（客户端）**：`Start.bat`（Windows，内部调用 `scripts/start-windows.ps1`）与 `Start-Mac.command` / `start-mac.sh`（macOS）。自动校验/补齐 Node.js、Python（全无时按官方 embeddable 自动构建 python-embed）、adb、scrcpy、iOS 工具链与 npm/Python 依赖后启动 Electron 客户端；只补缺失、不破坏既有环境，下载均为已验证的官方地址。原 Web 模式启动器保留为 `Start-Web.bat`（备用，局域网访问）
- **Word 性能报告导出**：按参考模板格式生成 docx（多设备表格、首/二次截尾均值、iOS 首次调整、异常样本标记），`/api/export_report_docx` 下载

### 修复
- **Word 导出必现 500**：中文文件名进 Content-Disposition 触发 starlette latin-1 编码异常，改为 ASCII 回退名 + RFC 5987 `filename*` 扩展名
- **模板回退误采纳**：设备分辨率未知时不再随机采纳其他设备的持久化模板（原逻辑给未知分辨率打满分）
- **单实例锁幽灵进程**：拿不到锁的实例不再无窗口常驻，完整走启动流程（仅 second-instance 聚焦属于持锁实例）
- **单设备模式日志不可见**：默认单设备模式下测速轮询日志（WAIT/SKIP/MATCH）恢复可见，复制日志/Excel 审计/报告附件同步恢复
- **测试隔离**：pytest 将项目持久化目录隔离到临时目录，测试结果不再受本机真实模板文件影响

### 工程
- 统计/导出核心口径抽为 `static/stats-core.js`（trimValues/sec 单点维护），由 `npm run test:js`（node --test）覆盖；测试增至 pytest 58 项 + node 7 项

---

## v2.0.0 — 2026-07-30

### 新增
- **Electron 桌面客户端**：从纯 Web（Start.bat + 浏览器）升级为原生桌面应用。主进程管理 Python 后端生命周期（venv/pip/uvicorn/健康检查），后端就绪后创建 BrowserWindow 加载前端页面。支持单实例锁定、全局异常兜底、设备热插拔即时感知（adb track-devices）。NSIS 安装器（可选安装目录、桌面/开始菜单快捷方式、卸载程序），安装预清理脚本防 adb/scrcpy 进程残留锁文件
- **scrcpy 实时镜像 / 录屏**：独立置顶窗口 30fps 镜像（不抢 adb 命令锁），支持后台录屏（720p/30fps，Windows MKV / Mac MP4）。环境变量 `ADB` + `SCRCPY_SERVER_PATH` 复用后端同一 adb-server
- **iOS 冷启动测试支持**：`server.py` 新增 `IosDevice` 类（pymobiledevice3 截图 + idevice_id CLI 设备检测），`/api/devices` 合并返回 Android + iOS 双平台设备（`platform` 字段）。Session 平台感知路由（AdbDevice / IosDevice 鸭子类型）。前端设备下拉框区分平台（🤖 Android / 🍎 iOS 图标）。AMDS 服务检测（`sc query`）
- **全自动测速循环**：一键自动跑「卸装→测首次→杀进程→测二次」× N 轮，复用单一 `performance.now()` 时钟 + 奇偶分组统计。失败即停策略，可中途停止，页面内报告 + CSV 导出
- **模板比对停表**：cv2.matchTemplate 区域搜索替代全屏 OCR，停表精度从 ±1-2s 提升到毫秒级（3ms/次）。用户点画面选定启动元素 → 后端截小区域存模板 → 运行时区域比对
- **项目持久化**：启动模板 / 跳过按钮 / 包名按项目分开存储（Electron 模式持久化到 userData/projects，开发模式到仓库根 projects/）
- **OC-2 主题**：OpenCode 风格暖灰极简设计，主色暖桃 `#fab283`，去毛玻璃 / 去渐变 / 文字灰度化，圆角统一 3px
- **一键发布系统**：`npm run release` 自动构建安装包 + 生成 Word 发布说明文档（可导入飞书 / 钉钉 / 语雀等在线文档）

### 修复
- **路径穿越**：`static_fallback` 直接拼接路径未验证边界，`GET /..%2Fserver.py` 可读源码。修复：resolve 后用 `relative_to(STATIC_DIR)` 验证
- **远程脚本**：删除 Tailwind Play CDN + Google Fonts，编译离线 `static/static.css`（21KB），成品零网络依赖
- **锁不全**：新增 `Session.device_op()` 上下文管理器，8 个会触发 adb 的端点统一加锁
- **adb 失败被吞**：`check=False` 吞非零退出码导致覆盖安装被当干净重装。修复：空输出立即抛错
- **历史记录混算**：引入 `runStartIndex` + `run_id` 批次隔离，防止多次自动运行结果交叉污染
- **纯色模板误命中**：TM_CCOEFF_NORMED 对纯色返回 1.0 满置信度。修复：set_marker_template 拒绝灰度标准差 < 15 的区域
- **Windows 录屏视频无法拖动**：改为发送 Ctrl+C 优雅收尾让 scrcpy 写完索引，容器固定 `.mkv` 对截断更鲁棒

### 维护
- `on_event` → `lifespan`（FastAPI 推荐）
- 请求模型加边界约束（ColdStartReq.mode 用 Literal，x/y 用 Field(ge=0,le=1)）
- `requirements.txt` 版本上界（防破坏性更新）
- `innerHTML` XSS 防御（escapeHtml 补全）
- `fetchShotOnce` 15s 超时兜底（防 liveBusy 锁死）
- `INSTALL_FAILED` 错误码中文翻译（24 条）
- `.gitattributes` 行尾规范化
- pre-commit hook：防误提交 .venv + ast 语法检查
- 后端纯函数 pytest 测试（29 项）

### 修复（构建）
- **应用图标**：将 32×32 占位图标替换为 256×256 多分辨率 ICO（深蓝渐变秒表 + 黄色闪电），满足 electron-builder NSIS 最小尺寸要求
- **winCodeSign 缓存解压失败**：手动解压 winCodeSign-2.6.0.7z 并用 `-snl-` 参数处理 macOS 符号链接，解决非管理员/未开启开发者模式下的构建报错

---

## v1.x — 2026-06 ~ 2026-07-23（纯 Web 版本）

> 以下为 Electron 化之前的历史记录，保留备查。

---

## 2026-07-29 · scrcpy 镜像 + iOS 支持 + OC-2 主题 + 工程加固（`867b33a`）

### scrcpy 实时镜像 / 录屏
- 新增 `electron/scrcpy-manager.js`：独立置顶窗口镜像 + 后台录屏（720p/30fps）
- 环境变量 `ADB` + `SCRCPY_SERVER_PATH` 复用后端同一 adb-server，不抢命令锁
- UI 左栏加镜像/录屏按钮，菜单栏加入口
- Windows 录屏用 MKV（截断鲁棒），Mac 用 MP4

### iOS 冷启动测试支持
- `server.py` 新增 `IosDevice` 类（pymobiledevice3 截图 + idevice_id CLI 设备检测）
- `/api/devices` 合并返回 Android + iOS 双平台设备（`platform` 字段）
- Session 平台感知路由（AdbDevice / IosDevice 鸭子类型）
- 前端设备下拉框区分平台（🤖 Android / 🍎 iOS 图标）
- AMDS 服务检测（`sc query`）

### OC-2 主题精简化
- 主色从蓝 `#034cff` 改为暖桃 `#fab283`，对齐 OpenCode 品牌视觉
- 去毛玻璃（backdrop-filter）、去渐变、文字灰度化
- 圆角统一 3px，hover 状态一致化
- 下拉框原生弹窗修复（实色背景 + 可读文字）

### UI 布局优化
- 左栏精简：直播开关移至顶栏，删除截图诊断条和缩放控件
- 删除标题栏 Logo 文字（被工具栏遮挡）

### 工程加固
- `adb kill-server` 退出清理（防 adb.exe 残留文件锁）
- 全局异常兜底 `uncaughtException` / `unhandledRejection` + `main-error.log`
- `adb track-devices` 设备热插拔即时感知（替代 5s 轮询，降为 15s 兜底）
- NSIS 安装器预清理脚本（`build/installer.nsh`，taskkill 残留进程）
- `INSTALL_FAILED` 错误码中文翻译（24 条）
- `fetchShotOnce` 15s 超时兜底（防 liveBusy 锁死）
- `innerHTML` XSS 防御（escapeHtml 补全）
- `requirements.txt` 版本上界（防破坏性更新）
- 后端纯函数 pytest 测试（29 项：`_safe_apk_filename` / `_safe_project_id` / `_raw_screencap_to_bgr`）

---

## 2026-07-23 · 安全与可靠性加固（第三方审核修复）

依据 GPT 5.6 SOL 代码审核意见，修复 6 个高危 + 5 个次要问题。每项均实测验证。

### 安全修复
- **[高1] 路径穿越**（`9797cdc`）：`static_fallback` 直接拼接路径未验证边界，
  `GET /..%2Fserver.py` 可读源码。修复：resolve 后用 `relative_to(STATIC_DIR)` 验证。
- **[高2] 远程脚本**（`6b81458`）：删除 Tailwind Play CDN + Google Fonts，
  按 AGENTS.md §3.3 编译离线 `static/static.css`（21KB 最小化）。成品零网络依赖。

### 可靠性修复
- **[高3] 锁不全**（`ddd108e`）：`_lock` 只覆盖截图/OCR，cold_start/force_stop/reinstall 等
  11 个端点裸访问 `SESSION.device`。修复：新增 `Session.device_op()` 上下文管理器，
  8 个会触发 adb 的端点统一加锁。
- **[高4] adb 失败被吞**（`36ba00d`）：`check=False` 吞掉非零退出码，device offline 时
  uninstall 输出为空仍继续 install，覆盖安装被当干净重装。修复：空输出立即抛错。
- **[高5] 历史记录混算**（`dda069d`）：自动报告 filter 所有 `source==='auto'` 记录，
  第二次运行混入第一次。修复：引入 `runStartIndex` + `run_id` 批次隔离。
- **[高6] 纯色模板误命中**（`9fe94e7`）：TM_CCOEFF_NORMED 对纯色返回 1.0 满置信度，
  启动瞬间立即停表。修复：set_marker_template 拒绝灰度标准差 < 15 的区域。

### 次要改进（`a76de39`）
- README 漂移修正（删虚构的 cold_start HOME 回桌面、改"服务端精确打点"）
- `on_event` → `lifespan`（FastAPI 推荐，on_event 已弃用）
- 请求模型加边界约束（ColdStartReq.mode 用 Literal，x/y 用 Field(ge=0,le=1)）

---

## 2026-07-22 · 自动测速功能完善

### 自动停表优化
- **删除文字匹配模式**（`708dbcc`）：实测 RapidOCR 全图推理 1373ms/次，精度 ±1-2s 不可接受。
  只保留模板比对（matchTemplate 3ms）。用户决策："1s 误差太大，直接放弃"。
- **设坐标不再真实点击设备**（`cd254c6`）：setCoords 去掉 POST tap，避免干扰设备。
- **卸装安装 log 加日期时间**（`cae12f5`）：autoLog 从 `[HH:MM:SS]` → `[YYYY/MM/DD HH:MM:SS]`。

### 体验修复
- **循环时保持直播**（`ea0b6fd`）：用户反馈"卸装重装看起来没生效"。根因是循环时停了直播，
  画面冻结。修复：循环时保持直播，只在 OCR 停表阶段临时停。

---

## 2026-07-21 · 自动测速 + 模板比对（核心功能上线）

### 全自动测速循环（`490d098`）
- 一键自动跑「卸装→测首次→杀进程→测二次」× N 轮
- 复用 §2.1 单一 performance.now() 时钟，§2.3 奇偶分组统计
- 失败即停策略，可中途停止
- 页面内报告 + CSV 导出

### 模板比对停表（`81acc56`）
- cv2.matchTemplate 区域搜索替代全屏 OCR，停表精度从 ±1-2s 提升到毫秒级
- 用户点画面选定启动元素 → 后端截小区域存模板 → 运行时区域比对
- 性能基准：matchTemplate 3ms/次，比全图搜索快 49 倍

---

## 2026-07-21 · APK 可信度 + Git 规范

### APK 处理（`6cc9ea4`）
- upload_apk 保留原始文件名（不再覆盖到固定 `_cst_upload.apk`）
- 安全过滤：basename 防路径穿越，非 ASCII 替换下划线，同名追加 hash

### Git 项目管理
- `.gitattributes` 行尾规范化（`4ae75c2`）
- pre-commit hook：防误提交 .venv + ast 语法检查（`86251cb`，修正 `64db976`）
- 项目更名为「App 冷启测速 / app-coldstart」（`a4421da`）

---

## 2026-07-21 · 依赖修复 + 启动

- **补全 python-multipart 依赖**（`902e011`）：`/api/upload_apk` 用 UploadFile，
  FastAPI 要求 python-multipart，原 requirements.txt 漏写导致后端起不来。

---

## 2026-06 · v1 → v2 重写（项目初始化 `c06b395`）

- v1 的 `server.ps1`（PowerShell）重写为 Python FastAPI
- 计时回滚到 v1 的纯 `performance.now()` 方案（详见 AGENTS.md §6 教训一、二）
- 新增 OCR（RapidOCR）+ 实时画面 + 点选坐标

---

## 待办（未完成）

- **目录重命名**：`冷启动计时器_Windows_v1.5` → `app-coldstart`（ZCode bash 锁定，需手动执行）
- **远程仓库 push**：GitLab 上创建 app-coldstart 仓库后，更新 remote URL 并 push
- **真机端到端验证**：所有代码改动已就绪，待用户插 Android 设备跑完整流程
