# 📱 App 冷启测速 · Cold Start Profiler

> Electron 桌面客户端 + Python FastAPI 后端。Android（ADB）+ iOS（libimobiledevice）双平台冷启动测速，scrcpy 实时镜像，模板比对自动停表。

---

## ✨ 核心能力

| 能力 | 说明 |
|---|---|
| **自动测速** | 卸装→测首次→杀进程→测二次，全自动循环；模板比对毫秒级停表 |
| **scrcpy 实时镜像** | 独立置顶窗口 30fps 镜像（不抢 adb 锁），支持后台录屏 |
| **iOS 支持** | USB 连接 iPhone，截图 + 模板比对 + 冷启动计时 |
| **项目持久化** | 启动模板 / 跳过模板 / 包名按项目分开存储 |
| **计时精度** | 单一 `performance.now()` 时钟，不校准；详见「数据精度」 |

---

## 🚀 快速开始

桌面客户端是主要形态，通过一键启动脚本运行（**不再需要安装包**）。

### 方式一：一键启动脚本（推荐）

**Windows**：双击 `Start.bat`

**Mac**：终端执行 `bash start-mac.sh`

脚本按当前环境自动处理，只装缺失项，重复执行无副作用：

| 依赖 | Windows（Start.bat） | Mac（start-mac.sh） |
|---|---|---|
| Node.js / npm | 需预装（脚本会检查并提示） | brew 自动安装 |
| adb | 自动下载官方 platform-tools 到 `adb/` | brew 安装 android-platform-tools（PATH 生效） |
| scrcpy（镜像/录屏） | 自动下载上游 v3.3 到 `scrcpy/` | brew 安装并符号链接进 `scrcpy/` |
| iOS 工具链 | 缺失时提示手动拷贝到 `ios/` | brew 安装 libimobiledevice + ideviceinstaller |
| Electron + Python 依赖 | `npm install` + 客户端闪屏内自动建 `.venv` | 同左 |

首次启动较慢（下载 Electron / 建 venv / 装 Python 依赖，1-3 分钟），之后秒开。

> 要求：Windows 需预装 [Node.js LTS](https://nodejs.org/)（`winget install OpenJS.NodeJS.LTS`）；Mac 需装 [Homebrew](https://brew.sh/)（脚本会自动安装）。手机需开启 USB 调试。

### 方式二：浏览器模式（备用，局域网访问用）

双击 `Start-Web.bat` —— 首次自动建 venv + 装依赖，启动后端并打开浏览器（监听 `0.0.0.0`，同网段同事可用电脑 IP 访问）。

> ⚠️ 浏览器模式无 scrcpy 镜像 / 录屏功能，仅 Android 截图轮询。

### 方式三：安装包（历史发布）

v2.0.0 及之前的安装包见 [Release 页面](https://git.7k7k.com/tianhaishun/app-coldstart/-/releases)，内置完整运行时，无需任何前置环境。

---

## 🎯 计时原理

```
POST /api/cold_start
  ↓
服务端 force_stop(pkg)           ← 确保冷启动（iOS 需手动上滑关闭）
服务端 tap / monkey 启动          ← 点击图标或包名启动
返回 { ok, start_wall }           ← start_wall 仅供诊断
  ↓
前端 startTs = performance.now()  ← 响应回来后直接打点
前端轮询 /api/check_auto          ← 截图 + cv2.matchTemplate 模板比对
模板连续命中 → 停表               ← 终点客观化，消除人工反应误差
```

单一 `performance.now()` 时钟，不做跨进程校准。计时起点 = tap 命令执行完、响应返回之后；终点 = 启动成功模板连续确认命中。

### ⚠️ 数据精度（重要）

- **起点误差**：漏掉 adb input tap 工具链执行时间（约 150-250ms，每次都漏，横向对比不受影响）
- **终点误差（自动测速）**：模板比对命中帧 − 当次截图耗时，精度 ±50ms
- **终点误差（手动模式）**：人工按键停止，反应时间约 250-400ms（系统性正偏）

**结论**：同设备同 APK 横向对比有效；绝对值仅供参考；差异 < ~0.3s 视为噪声。

### 📊 分组统计

工具按显示序号奇偶自动分组：
- 奇数序号 = **首次冷启动**（卸装后第一次启动）
- 偶数序号 = **二次冷启动**（杀进程后启动，不卸装）

每组独立去极值平均（n≥3 剔 1 max + 1 min）。iOS 首次冷启动最终均值 **-1 秒**（剔除 TestFlight 测试弹窗时间）。

---

## 📱 iOS 测试说明

iOS 冷启动测试依赖 pymobiledevice3 + libimobiledevice 工具链：

| 能力 | Android | iOS（非越狱） |
|---|---|---|
| 设备检测 | ✅ adb devices | ✅ idevice_id -l |
| 截图 | ✅ screencap raw+gzip | ✅ ScreenshotService PNG |
| 模板比对停表 | ✅ | ✅ |
| App 启动 | ✅ monkey -p | ⚠️ pymobiledevice3 launch |
| 模拟点击 | ✅ input tap | ❌ 非越狱不支持 |
| 杀进程 | ✅ am force-stop | ❌ 需手动上滑关闭 |
| 安装/卸载 | ✅ adb install | ✅ InstallationProxyService |

**前置条件**：Windows 需安装 Apple Mobile Device Service（随 iTunes 安装，或单独装 AMDS 驱动包）。

---

## 🖥️ scrcpy 镜像 / 录屏

基于 [scrcpy](https://github.com/Genymobile/scrcpy)（Android only）：

- **镜像**：点击「📱 镜像」弹出独立置顶窗口，30fps 实时画面，不抢 adb 命令锁
- **录屏**：镜像运行中点「⏺ 录屏」后台录制（720p/30fps），停止后保存到临时目录
- **环境变量复用**：scrcpy 通过 `ADB` + `SCRCPY_SERVER_PATH` 环境变量复用后端同一 adb-server

---

## ⌨️ 快捷键

| 键 | 功能 |
|---|---|
| **Space** | 开始 / 停止计时 |
| **Q** | 卸载重装 APK |
| **W** | 杀进程 |
| **R** | 清空历史 |
| **Del** | 删除最后一条 |
| **B** | 返回键 |
| **H** | 主页键 |
| **O** | 重新抓 OCR |

---

## 🔒 数据安全

- 工具**不上传任何数据**
- 测试记录保存在浏览器 `localStorage`（按项目分桶）
- Electron 模式后端绑 `127.0.0.1`（仅本机）
- `Start.bat` 默认绑 `0.0.0.0`（允许局域网访问）；如需仅本机访问，改 `HOST=127.0.0.1`
- 内置 adb + scrcpy + iOS 工具链，不污染系统 PATH

---

## 🔧 开发

```bash
# 安装依赖
npm install
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pip install -r requirements-dev.txt

# 开发模式（自动开 DevTools）
npm run dev

# 运行测试
.venv\Scripts\python.exe -m pytest tests/ -v

# 打包 Windows 安装包（含内嵌 Python）
npm run build:win

# 一键发布（构建 + Word 文档 + ZIP）
npm run release

# 生成发布文档（Word）
npm run gen-doc

# 重新生成 OC-2 主题 CSS
.venv\Scripts\python.exe static/themes/_bake_oc2.py
```

---

## ❓ 常见问题

### Q1：安装版首次启动很慢？

A：安装过程中已自动初始化 OCR 引擎，启动时显示加载动画，后端就绪后秒开。

### Q2：scrcpy 镜像按钮不亮？

A：安装版已内置 scrcpy。浏览器模式需手动下载 scrcpy 二进制放入 `scrcpy/` 目录。

### Q3：iOS 设备不显示？

A：1) 确认已安装 iTunes 或 AMDS 驱动；2) iPhone 解锁并点「信任此电脑」；3) 用数据线（非充电线）。

### Q4：浏览器显示"后端未启动"？

A：看后端窗口的报错。常见：端口 8766 被占（Hyper-V/WSL 保留），改 `Start.bat` 里的 `PORT`。

### Q5：怎么测 iOS 冷启动？

A：1) 连接 iPhone；2) 选择 🍎 iPhone 设备；3) 手动在 App 切换器中上滑关闭目标 App；4) 按 Space 启动计时；5) 手动点开 App 或用包名启动；6) 模板比对自动停表（需先设好启动成功模板）。

---

## 📄 开源许可

本项目采用 [GPL-3.0](LICENSE) 许可证。

Copyright (C) 2026 田海顺

- 你可以自由使用、修改、分发本软件
- 修改后的代码必须同样以 GPL v3 开源
- 必须保留原始版权声明
- 本软件不提供任何担保

---

## 📋 变更记录

详细的版本演进见 [CHANGELOG.md](CHANGELOG.md)。
项目硬规范（任何协作者必读）见 [AGENTS.md](AGENTS.md)。
