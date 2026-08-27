# iOS 非越狱自动化点击：技术调研与落地说明

> 结论先行：iOS 非越狱的自动点击在技术上**可行**，唯一正道是苹果官方 XCTest 体系（WebDriverAgent）。
> 本项目已完成全链路工程化落地：识别自动（GM 界面模板比对）→ 点击自动（WDA，需设备侧一次性签名安装）
> /点击手动（未装 WDA 时自动降级半自动）。本文档为该工作的完整交付记录。

---

## 1. 背景与需求

GM 版休闲游戏首次进入会弹出「选择实验」界面；二次冷启动也可能出现不同拦截界面。
冷启动测速依赖「启动成功页元素」做停表判定——GM 界面会挡住成功页导致误判。

需求演进：
1. 提供 iOS 覆盖安装能力（参考脚本 `ios_ipa_installer.py`，用户提供的 tidevice/facebook-wda 技术栈资料）
2. 安装方式 UI 单选：重新安装（清数据，默认）/ 覆盖安装（保数据），GP 与 iOS 同步
3. GM 界面跳过：图片识别 + **首次/二次分别设置、分别生效**
4. iOS 点击问题：非越狱环境如何实现自动点击

## 2. 技术调研结论

### 2.1 参考技术栈映射（用户提供 → 项目内实现）

| 参考方案角色 | 参考库 | 项目内最终实现 | 说明 |
|---|---|---|---|
| 设备 USB 通信（tidevice 角色） | alibaba/tidevice（已停更） | **pymobiledevice3 10.x**（替代表首选，纯 Python，支持 iOS 17+） | 项目既有依赖 |
| 「启动 WDA」（tidevice xctest 招牌能力） | tidevice xctest / Mac Xcode | **pymobiledevice3 `XCUITestService`**（`services/dvt/testmanaged/xcuitest.py`） | Windows 无 Mac 可用；本项目新增 `wda_launch()` 封装 |
| WDA 客户端（facebook-wda 角色） | openatx/facebook-wda | **pymobiledevice3 内置客户端**（`services/wda.py`：`WdaClient`/`WdaServiceClient`） | 等价且零新依赖；`wda_tap()` 坐标点击封装 |

### 2.2 API 差异坑（参考脚本无法直接运行的实证）

用户参考脚本基于旧版 pymobiledevice3，在本项目 venv（10.x）实测存在三处不兼容：

| 脚本写法 | 10.x 实测结果 | 项目内适配 |
|---|---|---|
| `from pymobiledevice3.ipa import IPA` | 模块不存在（`find_spec=None`）→ ImportError | 改用 `InstallationProxyService.get_apps()` + 已知 bundle id 判定是否已安装 |
| `create_using_usbmux(udid=udid)` | 无 `udid` 参数（仅 `serial/identifier`）→ TypeError | 使用 `serial=`（项目既有写法） |
| `ip.upgrade()/ip.install()` | `install` 已移除；`upgrade(ipa_path: str)` 保留 | `upgrade` + `install_from_local(Path)` 双路径 |

### 2.3 iOS 非越狱「点击」的本质约束与可行路径

- 苹果安全模型下，非越狱的一切自动点击方案的最终通道都是 **XCTest 体系**：
  在设备上运行签名后的 **WebDriverAgent Runner**，外部经 HTTP（端口 8100）下发指令。
- 因此唯一的硬性前提 = **把签名后的 WDA 安装进 iPhone**。达成手段多样：

| 签名途径 | 系统 | 有效期 |
|---|---|---|
| Xcode + Appium/WebDriverAgent 源码 | 需 Mac | 免费 ID 7 天 / 付费 365 天 |
| 爱思助手「开发者自动化」 | Windows ✅ | 同上 |
| Sideloadly / AltStore 侧载（社区有打包好的 WDA ipa） | Windows ✅ | 免费 ID 7 天 |
| 公司开发者证书（p12 + 描述文件）重签 | Windows ✅ | 随证书 |

## 3. 实现明细

### 3.1 后端（server.py，IosDevice 扩展）

| 组件 | 说明 |
|---|---|
| `install_overwrite(pkg, ipa_path)` | iOS 覆盖安装：`get_apps()` 判定已装→`upgrade(path)`、未装→`install_from_local(Path)`（10.x 双路径）。同 `/api/reinstall overwrite=true` 接入 |
| `wda_ready()` | 探测 WDA 就绪（lockdown 直连 8100 `/status`）；未就绪时自动尝试 `wda_launch()` 拉起一次再复测 |
| `wda_launch(runner_bundle_id, target_bundle_id=None)` | 经 `TestConfig.create_for` + `XCUITestService.run(timeout=None)` 后台常驻启动 Runner；前置 InstallationProxy 校验（未装→400 中文安装指引）；整个协程提交到 IosLoop 常驻 loop（asyncio 对象不跨 loop 的项目教训遵守）；幂等 |
| `wda_tap(cx, cy)` | WDA 坐标点击：`get_window_size`（逻辑 point）换算归一坐标 → 优先 Appium 扩展 `/session/{sid}/wda/tap/0`、异常回退 W3C `/actions`（pointerMove+Down+Up）；会话失效自动重建 |
| check_auto 跳过命中接入 | iOS 平台命中 GM/弹窗模板时：`wda_ready()` 为真 → `wda_tap` 全自动（同 Android 流程：fired+不停表）；为假 → 返回 additive 字段 `skip_manual_pending=true`（半自动提示，1.5s 节流），前端据此提示手动点击 |

### 3.2 跳过界面模板按阶段分组（GP/iOS 通用）

| 项 | 说明 |
|---|---|
| `SetMarkerReq.phase` | `first` / `second` / `any`（默认 any 兼容旧调用） |
| 存储 | 模板 entry 带 `phase`；旧模板缺省视为 any（两组都匹配） |
| 上限 | 按 phase 分组各 3 个（总额上限放宽，GM 场景不被挤掉） |
| 匹配过滤 | `check_auto?phase=first|second` 只匹配对应阶段 + any；缺省不过滤（兼容） |
| 生命周期 | `_skip_fired_ids` 每次 cold_start 前随 `reset_marker_watch` 清空 → **首次/二次每次都重新识别并跳过** |

### 3.3 前端（static/index.html）

- 「跳过界面模板」拆分为 **【首次】/【二次】两个并排识别区**（各自添加按钮/模板列表/状态行），phase 徽标语义明确
- 自动循环 `measureOnce`：首测传 `phase='first'`、二测传 `'second'`；`waitForLaunchDone(checkSkips, serial, logSer, phase)`
- `skip_manual_pending` 处理：日志每 10s 节流提示「请在手机上手动点掉它 · 不停表」，流程不中断
- 新增「▶ 启动 WDA」按钮（`POST /api/wda_launch`）：Android 忽略；未装 Runner 弹中文指引
- IPA 链路：上传 accept `.ipa`、Electron 对话框 filters 加 IPA、上传后自动调 parse_apk 回填 bundle id 到包名输入框
- 包名下拉改为自绘可滚动列表（datalist 原生弹层太短滚不动）；样式用内联 id 规则（Tailwind arbitrary 类不在编译产物 static.css 的坑）

## 4. 验证记录

| 项 | 结果 |
|---|---|
| pytest | 113 passed（覆盖：安装方式契约/路由、IPA 解析与端点、覆盖安装两端严格校验、跳过 phase 过滤、WDA ready/tap/回退/未就绪降级等） |
| 后端语法 | server.py / adb_helper.py AST 通过；index.html 内嵌 JS node --check 通过 |
| 真机 iPhone（00008101-...） | 截图链路 200（918KB，tunnel 复用 ~690ms/帧）；`/api/wda_launch` 未装 Runner 时返回精确中文指引 ✓；`wda_ready=False` 时循环走半自动路径无回归 ✓ |
| 全自动点击真机验证 | 待设备侧完成 WDA 签名安装后即插即用（工具侧代码已就绪并接通状态机） |

## 5. 使用指引

### Android（GP）
安装方式单选默认「重新安装」；升级场景切「覆盖安装」。跳过界面模板在【首次】组设置即可全自动。

### iOS 半自动（无需任何额外配置）
1. 展开「跳过界面模板」→【首次】/【二次】分别「＋ 添加」→ 点画面上 GM 界面的确定/开始按钮生成模板
2. 跑自动测速：检测到 GM 界面 → 日志提示"请在手机上手动点掉它"→ 手点后流程继续，不停表

### iOS 全自动（一次性配置 WDA）
1. 任选一种签名途径把 WebDriverAgentRunner 安装到 iPhone（见 §2.3 表格；推荐 Windows 用爱思/Sideloadly + 社区打包 ipa）
2. 客户端点「▶ 启动 WDA」（或跑测速时 `wda_ready()` 自动拉起）
3. 之后与 Android 全自动完全一致

## 6. 边界与已知限制

- 免费 Apple ID 签名有效期 7 天，过期后 WDA 失效（工具自动回落半自动，不会中断流程）
- iOS 无法杀后台进程（force_stop 为 no-op）、无 scrcpy 镜像、无 am start -W 对照——均为平台限制，与本功能无关
- `services/wda.py` 的坐标 tap 走其内部 HTTP 原语 `_request_json`（库内私有）；若 pymobiledevice3 大版本变更需回归本文件
- WDA runner 由 XCUITestService 拉起后常驻于 IosLoop；后端进程重启后需重新点「启动 WDA」（runner 进程亦随之结束）

## 7. 相关提交索引

| 提交 | 内容 |
|---|---|
| `adef3e4` | feat(ios+install)：覆盖安装闭环、安装方式单选、IPA 支持、跳过模板分组、iOS 半自动/WDA 骨架 |
| `c3efad0` | perf: _platform_for_serial TTL 缓存（复审抓到的热路径回归） |
| 本次提交 | feat(wda): XCUITest 启动器（Windows 拉 WDA）+ /api/wda_launch + 前端按钮 + phase 文档测试补齐 |
