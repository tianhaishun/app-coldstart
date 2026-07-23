# 变更记录（CHANGELOG）

本文件记录项目的重要变更，按时间倒序。详细提交信息见 `git log`。

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
