# AppCold 审核修改 · 开发需求文档 v1

> 来源：第三方代码审核方案（2026-08-08，hermes 审核）→ 本文档将方案转化为可执行需求规格。
> 目标：在不破坏已验证核心逻辑的前提下补齐精度证据、样本可比性和健壮性。
> 执行前必读：项目根 AGENTS.md（先列计划经用户确认、不顺手重构、注释中文、改完验证）。
> 本文档是执行依据：每条需求含接口设计、实现映射、边界条件、验收标准；与方案有出入处标注「⚠ 修订」。

## 0. 执行红线（违反即返工）

1. 禁止改动 v1 计时逻辑：前端 `startTimer`/`measureOnce` 的单一 `performance.now()` 打点、响应回来后起表、命中帧扣 `shot_ms`——全部保持原样（AGENTS §2.1、教训一/二）。
2. 禁止改已有 API 字段名：只允许新增字段/新增端点（向后兼容）；前端读取字段与后端返回必须同步修改。
3. 禁止新增 pip 依赖（如需必须停下来问用户）。
4. 不要顺手重构：改 A 时不动 B。
5. 每项改完必须验证（见 §8 验证矩阵）。

## 1. 总体顺序与依赖

```
A → B → G → E → F → C → D
```

- A、B：P0（精度证据 / 样本可比性），先做。
- G：健壮性底线（fetch 超时），提前到 E/F 之前——后续 E/F 动 check_auto/模板路径，先垫好超时层。
- E、F：连着做（都动 check_auto + DeviceSession，同一面改完一起验证）。
- C、D：纯前端小事，放最后。
- H（温度/降频记录）：本期不做，留档（§7）。

## 2. A. P0-1 系统对照模式（am start -W 交叉验证）

### 2.1 需求描述
当前工具计时口径（前端单时钟 + 模板停表）缺少与系统打点的交叉验证。新增独立对照模式：用行业标准 `am start -W` 连跑 N 次，输出 TotalTime 均值/中位数，与工具历史均值并排展示，证明工具能稳定复现系统趋势。

### 2.2 接口设计

**新增端点** `POST /api/sys_baseline`

请求体（新增请求模型 `SysBaselineReq`）：
```json
{ "package": "com.example", "rounds": 5, "cooldown_s": 3.0, "serial": null }
```
- `rounds`：1~10，默认 5；`cooldown_s`：0~10，默认 3；`serial`：可选（默认当前选中，走 `_target_session`）。

响应：
```json
{
  "ok": true,
  "package": "com.example",
  "component": "com.example/.MainActivity",
  "rounds": 5,
  "samples": [
    { "idx": 1, "this_ms": 120, "total_ms": 450, "wait_ms": 460, "raw": "ThisTime: 120 ..." }
  ],
  "errors": ["第 3 轮解析失败：...（已跳过）"],
  "stats": { "total_mean_ms": 452.0, "total_median_ms": 450.0, "n": 5 }
}
```
- 全部轮次解析失败 → `ok: false` + 400 或 errors 说明（前端展示）。
- `total_ms` 用 **TotalTime**（含进程创建，真冷启动口径）；`this_ms`（ThisTime）与 `wait_ms`（WaitTime）记录供诊断。

### 2.3 实现映射（server.py）
- 新请求模型 `SysBaselineReq`（pydantic，rounds/cooldown_s 带 Field 校验）。
- 新端点 `sys_baseline(req)`：
  1. `ds = _target_session(req.serial)`；`with ds.lock:` 全程持锁（对齐 §2.4 审核修复）。
  2. 组件解析：`ds.device.run(["shell", "cmd", "package", "resolve-activity", "--brief", pkg])` → 输出形如 `com.example/.MainActivity`；空/异常 → `AdbOpError` → HTTP 400「解析不到可启动 Activity」。
  3. 循环 N 轮：`force_stop(pkg)` → `time.sleep(cooldown_s)`（**锁内等待**，串行语义）→ `run(["shell", "am", "start", "-W", component])`（timeout 30s）。
  4. 解析正则（容错）：`ThisTime:\s*(\d+)` / `TotalTime:\s*(\d+)` / `WaitTime:\s*(\d+)`；解析失败该轮记 `errors` 并跳过（不中止）。
  5. 统计：均值 + 中位数（n 为成功样本数）。
- 错误处理：`AdbOpError` → `_err(400, str(e))`（复用现有模式）。

### 2.4 ⚠ 修订（与审核方案差异）
- **差值表述**：`am start -W` 终点是系统首帧绘制；模板停表终点是启动成功页元素就绪。两者口径不同，前端对照区**不得写**「工具均值 − 系统均值 = 工具精度偏差」，改为展示两者数值 + 口径说明文案（演示时被追问的第一问）。
- 前端对照区文案示例：「工具模板停表（就绪页） vs 系统 TotalTime（首帧）——口径不同，并排看趋势一致性」。

### 2.5 前端（static/index.html）
- 自动测速面板附近（autoLaunchHint 下方或报告卡内）加「📊 系统对照」按钮 + 结果展示区（`<pre>` 或表格，纯展示）。
- 点击 → `apiFetch('/api/sys_baseline', {method:'POST', body})` → 展示 N 轮 TotalTime 列表 + 均值/中位数。
- 与当前项目 records 中同包名工具记录均值并排（工具均值从 `records` 现算，按包名过滤——records 无包名字段？⚠ 检查：records 无 package 字段，改用「当前选中项目」范围即可，文案注明）。
- **纯展示，不写 records，不影响任何现有统计**。

### 2.6 验收
- 真机跑 5 轮：samples 有数、stats 正确、UI 并排显示。
- 无 Activity 的包名 → 400 + 前端展示错误。
- 解析失败轮次进 errors 且不中止后续轮。

## 3. B. P0-2 APK 元数据入记录（首冷样本可比性）

### 3.1 需求描述
首冷样本跨版本不可比（不同 APK 版本、同版本重装后 ART 缓存状态不同）。记录 APK 指纹，报告按版本分组，避免版本升级后的首冷变化被误判为性能回归。

⚠ 只用于报告分组，**不叠加**时间戳/指纹等「可信度增强」（教训三）。

### 3.2 接口设计
- `POST /api/reinstall` 响应**新增字段** `apk_info`：
```json
{ "name": "app-1.2.3.apk", "size_bytes": 12345678, "sha256_prefix": "a1b2c3d4e5f6" }
```
- sha256 前 12 位 hex；计算失败（读文件异常）→ `apk_info: null`，不阻塞流程。

### 3.3 实现映射
- **server.py `reinstall` 端点**：锁外（APK 已存在检查之后、持锁之前）算 sha256——百 MB 级 APK 哈希数百 ms，不占设备锁（文件在锁外读，若锁内计算会阻塞该设备其它 I/O）。
  - `hashlib.sha256()` 流式读（chunk 1MB，防大文件内存峰值）。
- **static/index.html `runAutoLoop`**：records.push 两处（first/second）带 `apk: reinstallData.apk_info`——⚠ 注意：**仅首次需要**（二次不重装，沿用本轮 reinstallData）；仅二次模式无 reinstallData → `apk: null`。
- **报告分组**（renderAutoReport + 复制文本 + CSV）：
  - 按 `r.apk?.sha256_prefix` 分组展示首冷均值；组内标 APK 文件名。
  - 旧记录无 apk 字段 → 归入「未记录版本」组（`r.apk?.sha256_prefix || '未记录版本'`）。

### 3.4 验收
- reinstall 响应含 apk_info；sha256 前缀 12 位。
- 旧 localStorage 记录加载不报错（`r.apk?.` 容错）。
- 报告按 APK 分组正确；仅二次模式记录 apk 为 null 不崩。

## 4. G. P1-5 前端 fetch 超时

### 4.1 需求描述
adb 卡死/后端进程崩溃时 fetch 可能长时间挂起，自动循环会卡死。封装 `apiFetch`，关键调用加超时。

### 4.2 实现
- 新增 `async function apiFetch(url, opts = {}, timeoutMs = 15000)`：AbortController + setTimeout；超时抛 `new Error('请求超时（${timeoutMs}ms）')`。
- 替换 7 处关键 fetch（`static/index.html`）：
  - 手动路径：`cold_start`(1638)、`reinstall`(1891)、`force_stop`(1912)
  - 自动循环：`kill_all`(2615)、`force_stop`(2626)、`reinstall`(2736)、`cold_start`(2835)
  - 轮询：`check_auto`(2544)——⚠ **超时不中止整轮**：catch 里记警告并 continue（与现有「请求异常继续重试」行为一致，现有 catch 已处理）。
- 直播链（/api/screenshot 的 Image 加载）已有 `SHOT_TIMEOUT_MS` 机制，**不动**。

### 4.3 验收
- 后端停掉后点「开始」：15s 内报错/中止，不无限挂起。
- check_auto 超时：日志警告 + 继续轮询，不中断整轮。

## 5. E. P1-3 模板匹配阈值可调

### 5.1 需求描述
`MARKER_MATCH_THRESHOLD=0.85` 写死；不同分辨率/UI 风格设备可能需要不同阈值。

### 5.2 接口设计
- **DeviceSession 新增实例属性** `marker_threshold: float = 0.85`（每设备独立，重启还原——与模板本身行为一致，UI 提示）。
- 新增端点 `POST /api/marker_threshold`：
  - 请求 `{ threshold: float, serial?: str }`；校验 `0.5 <= threshold <= 0.99`，非法 → 400。
  - 设置 `ds.marker_threshold`，**同时失效 `ds._marker_matcher` 缓存**（matcher 的 threshold 参数需重建）。
  - 返回 `{ ok, threshold }`。

### 5.3 实现映射
- `MARKER_MATCH_THRESHOLD` 引用点（6 处）逐一改为 `ds.marker_threshold`：
  - `DeviceSession.ensure_marker_matcher` 构造参数（608）
  - `check_marker` 返回 threshold（1507）
  - `check_auto` above 判断（2046）+ 返回 threshold（2070）
  - `marker_watch_reset` 返回 threshold（2108）
  - `cold_start` 返回 marker_threshold（2257）
- 模块常量保留（默认值来源，`DeviceSession.__init__` 用它初始化）。
- 跳过模板阈值 `SKIP_MATCH_THRESHOLD` 本次不动（范围收窄，保持常量）。

### 5.4 前端
- 模板设置区加阈值输入框（默认 0.85）——⚠ **布局零影响**：并入现有模板状态行（如 markerStatus 行右侧 inline），不新增行；调 `POST /api/marker_threshold`。
- 展示处（check_auto 返回的 threshold）跟随。

### 5.5 验收
- 改阈值后 check_auto 返回新 threshold；`above` 判定同步生效。
- 非法值（0.2 / 1.5）→ 400。
- 每设备独立：设备 A 改 0.9 不影响设备 B 的 0.85。

## 6. F. P1-4 分辨率变化检测

### 6.1 需求描述
模板与分辨率绑定（AGENTS §2.5），换分辨率后无提示，用户可能带旧模板测出全错数据。

### 6.2 实现
- **DeviceSession 新增字段** `_marker_res: Optional[tuple[int,int]] = None`：
  - `set_marker_template` 成功时记录 `ds._marker_res = (w_px, h_px)`（截图原分辨率）。
  - `_apply_project_to_session` 加载项目模板时：meta 无分辨率字段 → `ds._marker_res = None`（未知，跳过检查，不误报）。
- `check_auto` / `check_marker`：若 `ds._marker_res` 非 None 且 `ds.device._last_size` 非 None 且两者不一致 → 返回字段 `"res_mismatch": true`（仅提示，不阻塞停表）；一致或未知 → `false`。
- 前端 `waitForLaunchDone`：`j.res_mismatch` 为 true 时 autoLog 警告一次「⚠ 分辨率已变化，建议重设启动模板」。

### 6.3 验收
- 模拟改分辨率（`adb shell wm size` 改后改回）：警告出现且不阻塞停表。
- `_last_size` 未预热（None）时不误报。

## 7. C. P1-1 iOS 首冷 -1s 可配置

### 7.1 需求描述
TestFlight 弹窗扣 1 秒写死，iOS 升级后行为可能变化。改为可配置，默认 -1s，0 关闭。

### 7.2 实现（纯前端）
- 新增配置 `iosFirstAdjustSec`（默认 1.0），存 localStorage（CONFIG 对象 + 设置区输入框，小数步进 0.5，允许 0）。
- 5 处使用点全部改为读取配置（grep 已定位）：
  - `computeStatsGrouped.firstAdjustMs`（1753）
  - `renderStats` meta 显示（1790，读 gs.first.adjustment，自动跟随）
  - 复制文本「平台调整」（1960）
  - CSV 导出「首次_平台调整」（2044）
  - `renderAutoReport.firstAdjust`（2893）+ 平台调整行（2940）
- 配置为 0 → 完全不做调整（adjustment=0，各路径自然关闭）。
- 行为默认不变（默认 1.0 秒 = 原 -1000ms）。

### 7.3 验收
- 切 platform=iOS：改 0 / 1.5，报告/CSV/复制文本数字相应变化；改回 1.0 恢复默认。
- Android 平台不受影响（调整恒为 0）。

## 8. D. P1-2 样本不足标记

### 8.1 需求描述
n<3 全量平均、n=3 截尾后仅剩 1 值，统计意义不足。加显式标记（纯展示，不改计算，AGENTS §2.3 已知行为保持）。

### 8.2 实现（纯前端展示层）
- 标记规则：`n < 3` → 「⚠ 样本不足（n=2）」；`3 <= n < 5` → 「样本较少（n=3）」；`n >= 5` → 正常。
- 落点：
  - `computeStatsGrouped.trimMean`：n<3 的 warning 文案增强（现有 warning 机制复用）；3≤n<5 时加 warning「样本较少」。
  - `renderStats` 的 firstMeta/secondMeta：warning 已展示，跟随。
  - `quickTrim` 调用处（SUMMARY 日志）：按 firsts.length/seconds.length 在日志追加标记。
  - `renderAutoReport`：trimMean 同样加 warning 展示。

### 8.3 验收
- 分别用 2 / 3 / 5 条记录验证标记出现与否。

## 9. H. P2 温度/降频记录（留档，本期不做）

环境控制证据。实现成本高（dumpsys thermalservice / /sys 节点，ROM 差异大）。留档：若做，每次记录样本附带 thermal_state，报告标记降频期间样本。

## 10. 契约检查清单（前后端字段逐一核对）

| 端点 | 新增/变更字段 | 前端消费点 |
|---|---|---|
| POST /api/sys_baseline | 全部新增 | sys_baseline 结果区 |
| POST /api/reinstall | +apk_info | runAutoLoop → records.apk → 报告分组 |
| POST /api/marker_threshold | 全部新增 | 阈值输入框 |
| GET /api/check_auto | +res_mismatch | waitForLaunchDone 警告 |
| GET /api/check_marker | +res_mismatch | （轮询展示，可选） |
| check_auto/check_marker/marker_watch_reset/cold_start | threshold 改读实例值（字段名不变） | 现有读取点不变 |

⚠ 老字段一律不动；新增字段全部向后兼容。

## 11. 验证矩阵（全部做完必跑）

- [ ] `python -c "import ast; ast.parse(open('server.py', encoding='utf-8').read())"` 通过
- [ ] 后端 uvicorn 启动，/api/health 200
- [ ] A：/api/sys_baseline 真机 N 轮样本 + UI 并排；无 Activity 报 400；解析失败进 errors
- [ ] B：reinstall 含 apk_info（sha256 12 位）；旧记录加载不报错；报告按 APK 分组；仅二次模式不崩
- [ ] C：iOS 调整 0 / 1.5 生效，报告/CSV/复制文本同步；Android 恒 0
- [ ] D：n=2/3/5 标记正确
- [ ] E：阈值改动后 check_auto 返回新值且 above 生效；0.2/1.5 被拒；每设备独立
- [ ] F：分辨率变化警告出现且不阻塞；_last_size 未预热不误报
- [ ] G：后端停止时前端 15s 内报错；check_auto 超时警告续跑
- [ ] 自动测速完整跑一轮（全选/仅首次/仅二次 各一次），首次/二次记录正常写入
- [ ] 前端读取字段与后端返回字段逐一核对（§10）
- [ ] 45+ 单测全绿（.venv/bin/python -m pytest tests/）
- [ ] 布局零影响：截屏对比自动测速面板前后
