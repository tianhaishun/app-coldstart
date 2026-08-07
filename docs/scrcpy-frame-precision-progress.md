# scrcpy 帧精度 POC 进度跟踪

更新时间：2026-08-06（本轮：修复阻塞项并完成实机复测）

## 当前状态

- 分支：`scrcpy-frame-precision`
- POC 仍为独立验证工具，尚未接入正式测速状态机、`server.py` 或 `check_auto`。
- 已连接并识别一台 Android 真机：Pixel 6a（序列号 27231JEGR06AE5，1080×2400）。
- 验证环境：项目内 scrcpy 4.0、PyAV 18.0.0、OpenCV 5.0.0。
- 测试：45 项全部通过（`.venv` 下运行；系统 Python 无 cv2 时 matcher 测试会跳过）。

## 本轮修复（2026-08-06）

针对上次实机复测暴露的问题，已修改 `scripts/scrcpy_frame_probe.py`：

1. **socket 读取超时（原阻塞项 1）**：新增 `BufferedStream` 可恢复读取器，部分收到的字节保留在内部缓冲区，socket.timeout 发生在数据包中间也不会丢同步；读取超时按轮询间隔（0.25s）设置，主循环在超时后重新检查 duration 到期。流头/会话头读取阶段使用 5s 超时并转为 ProbeError。
2. **无效 PTS 统计（原阻塞项 2）**：`summarize` 只使用 `pts_us >= 0` 的样本计算 PTS 时长和 first/last_pts，配置帧（pts=-1）不再污染 FPS 统计。
3. **模板大于帧时不崩溃（审查 M3）**：ROI 仍小于模板时直接返回零置信度结果，不再让 `cv2.matchTemplate` 抛裸异常。
4. **--stop-on-hit 提前退出时不再误报**：命中后样本不足 2 帧时直接退出（原实现会因 summarize 需要 ≥2 帧而 [FAIL]）。
5. **Windows 控制台 UTF-8 输出**：stdout/stderr 重配置为 UTF-8，中文错误信息不再乱码。

新增/更新测试：BufferedStream 跨超时恢复（read_exact 与 read_packet 两条路径）、模板大于帧的零置信返回、首帧 pts=-1 的统计过滤。

## 修复后实机验证结果

### 静止画面（桌面，无动画）

- 720 高度：duration 10s 严格生效（实际 10.25s），静止时 PTS 间隔恒为 100ms（10fps），PTS FPS 10.0。
- 1080 原生 + 模板 + `--stop-on-hit`：第一帧即命中（置信度 0.9988），立即干净退出（EXIT=0）。

### 动画场景（两次 adb swipe，720 高度，15s）

```text
duration_s: 15.085
decoded_frames: 103
PTS FPS: 15.657
PTS 间隔 P50: 17.123ms（动画期间 ≈58fps）
PTS 间隔 P95: 100ms（静止期 10fps）
解码 + BGR P50: 3.777ms
解码 + BGR P95: 5.967ms
```

### 动画场景（1080 原生 + 真实模板，15s）

```text
duration_s: 15.106
decoded_frames: 106
PTS FPS: 15.97
PTS 间隔 P50: 17.241ms
PTS 间隔 P95: 100ms
解码 + BGR P50: 5.846ms
解码 + BGR P95: 12.028ms
marker_checked_frames: 106
marker_hit_frames: 58
marker_match_ms P50: 10.144ms
marker_match_ms P95: 11.27ms
marker_best_confidence: 0.9997
marker_full_frame_fallbacks: 0
```

模板来源：手机桌面截图的中间图标行区域（1080 截图裁剪 680×370），模板中心归一化坐标 (0.5, 0.5146)。

## 结果解释与结论

- **duration 严格生效**：15s 运行实际 15.1s 结束，读取超时阻塞问题已解决。
- **静止画面帧率低是正常行为**：屏幕完全静止时 Android 合成降到 10fps（PTS 间隔恒定 100ms）；动画期间升到约 58fps（PTS 间隔 P50 ≈17ms）。链路本身没有丢帧，帧率由设备合成节奏决定。历史记录的"58fps"是动画/内容变化场景下的结果。
- **真实模板匹配可用**：1080 原生帧上命中 58/106 帧（swipe 动画期间模板区域随图标移动，置信度跌破 0.85 后回升），最佳置信度 0.9997；`--stop-on-hit` 在第一帧命中后干净退出。
- **1080 原生下匹配耗时约 10ms/帧**（720 流时因 ROI 不足走 early-return 显示 0.009ms，不是真实匹配耗时）：10ms 匹配 + ~6ms 解码 ≈ 16ms/帧，在 60fps（16.7ms/帧）下接近满载。生产接入需评估：缩小模板/ROI、降低匹配频率、或使用 720 流并按比例缩放模板。

## 端到端冷启动验证（2026-08-06）

目标应用：Brick Blast（com.easybrain.brick.balls.blast，Unity 游戏，启动画面→主菜单链路完整）。

流程：截图主菜单 → 裁剪游戏名区域（680×350，y 700-1050）做模板 → force-stop → probe 运行期间 monkey 启动应用。

结果：

```text
app launched 后：前 214 帧全部未命中（启动画面/加载过程）
第 215 帧命中：confidence 0.8991（主界面游戏名区域）
--stop-on-hit 生效，probe exit=0
采样期间 PTS 间隔 P50: 16.928ms（约 59fps，加载动画期间）
解码 + BGR P50: 3.793ms / P95: 7.272ms
marker_match_ms P50: 9.414ms
```

结论：**冷启动链路端到端可用**——启动画面期间模板不命中，主界面出现后立即命中并停表。这就是生产"启动成功停表"的核心语义，已用真实应用验证。

## OpenCV 匹配耗时基准（2026-08-06，离线）

1080×2400 帧、Brick Blast 标题模板（680×350），TM_CCOEFF_NORMED：

| 匹配方式 | 耗时 P50 | 备注 |
|---|---|---|
| 全帧匹配（原模板） | 123.6ms | 不可行 |
| 全帧匹配（模板缩 0.5x） | 97.6ms | 不可行 |
| ROI 720×430（原模板完整放入） | 10.1-10.6ms | 与实机一致；模板缩放会改变置信度分布 |
| ROI 500×300（模板随之缩放） | 4.8ms | 置信度分布改变，阈值需重标定 |
| ROI 360×200 | 2.1ms | 同上 |

结论：全帧匹配不可行，必须 ROI；原模板 + 720×430 ROI ≈10ms/帧，加解码 ~6ms 共 ~16ms/帧，60fps（16.7ms/帧）下单核接近满载。生产可选：A) 原模板 + 固定 ROI（10ms，阈值 0.85 语义不变）；B) 模板缩放 + 小 ROI（2-5ms，需重新标定阈值）；C) 降低匹配频率或仅在关键阶段匹配。**不推荐全帧匹配。**

## 10 分钟长跑结果（2026-08-06）

参数：`--duration 600 --max-fps 60 --max-size 720`（无模板），手机前台为 Brick Blast 主菜单（有常驻动画，全程约 58fps），每 90 秒 adb swipe 一次。

```text
duration_s: 600.008（严格 10 分钟）
decoded_frames: 34771
PTS FPS: 57.946
接收 FPS: 57.951
PTS 间隔 P50: 16.758ms
PTS 间隔 P95: 20.408ms
PTS 间隔 max: 38.788ms
接收间隔 P95: 24.011ms
解码 + BGR P50: 2.61ms
解码 + BGR P95: 4.706ms
```

内存（WMI 采样 probe 主进程 WorkingSet）：56.5MB → 59.1MB → 59.5MB（跨度 10 分钟，+3MB 属正常浮动，无泄漏趋势）。

**重要说明**：内存采样只在启动、中途、结束三个时间点成功（初始采样脚本因进程 ID 问题产生空值，后改用 WMI 按命令行过滤）；结论基于三点采样，粒度有限但趋势平稳。

## POC 通过标准对照（2026-08-06 实机汇总）

| 标准 | 要求 | 实测 | 状态 |
|---|---|---|---|
| 实际 FPS | ≥30，目标 60 | 57.9（长跑全程） | 通过 |
| PTS 间隔 P95 | ≤50ms | 20.4ms | 通过 |
| 丢帧可检测 | 可检测并记录 | pts/receive 双统计可用 | 通过 |
| 解码+BGR P95 | <10ms | 4.7ms（720）/ 12.0ms（1080 含 IDR） | 720 通过，1080 接近 |
| 10 分钟无内存增长 | 无明显增长 | +3MB 波动 | 通过 |
| Windows 实机 | 完成一次 | Pixel 6a 多次 | 通过 |
| macOS 实机 | 完成一次 | 未做 | 待做 |
| 不影响现有测速 | 失败不影响 | POC 独立，未改 server.py | 通过 |

补充说明：1080 原生流的 P95 12ms 主要由首帧 IDR 解码（13ms）贡献；静态画面时 Android 合成降到 10fps 属设备正常行为，非丢帧。静止画面 + 60fps 采样场景下 PTS 间隔 P95 会因混合静止期（100ms）超出 50ms 阈值——判定时应以动画/内容变化场景为准。

## 生产匹配策略定案（2026-08-06，对标校准实验）

校准实验（Brick Blast 标题模板，命中画面 vs 桌面非命中画面）：

**缩放模板（1080 帧上）不可行**：

| 模板缩放 | 命中置信度 | 非命中置信度 | 耗时 |
|---|---|---|---|
| 1.0x（680×350） | 1.0000 | 0.0520 | 10.2ms |
| 0.75x | 0.7867 | 0.0399 | 5.8ms |
| 0.5x | 0.5776 | -0.0712 | 2.7ms |
| 0.3x | 0.3259 | 0.0143 | 1.1ms |

TM_CCOEFF_NORMED 对缩放模板极度敏感：模板模糊化后与清晰帧内容的相关性崩坏，0.75x 就跌破 0.85 阈值。**不能缩放模板，模板必须与视频流同分辨率截取。**

**720 流 + 同分辨率模板（定案）**：

| padding | ROI | 命中置信度 | 非命中置信度 | 耗时 P50/P95 |
|---|---|---|---|---|
| 20 | 244×144 | 1.0000 | 0.0680 | 1.17 / 1.59ms |
| 10 | 224×124 | 1.0000 | 0.0564 | 0.96 / 1.43ms |
| 0 | 204×104 | 1.0000 | 0.0457 | 0.68 / 0.87ms |

结论：**生产匹配策略 = 720 流（--max-size 720）+ 同分辨率模板 + padding 20 + 阈值 0.85 语义不变**。每帧成本：解码 ~3ms + 匹配 ~1.2ms ≈ 4.2ms/帧，60fps 下单核占用约 25%，余量充足。模板获取流程（未来生产）：目标应用主界面稳定后，用与测速流相同的 max-size 截图并裁剪模板；**禁止从高分辨率模板直接缩放**。

## 接入决策意见（2026-08-06，已给意见待拍板）

接入方案讨论稿见 `docs/scrcpy-frame-precision-integration.md`，4 个决策点意见：

1. **方案**：采纳方案 B（事件驱动）——方案 A（轮询读最新帧）的停表精度受前端轮询频率钳制，58Hz 帧流发挥不出来；B 让后台线程持续解码匹配，check_auto 只读状态。落地分两阶段：先验证正确性（前端轮询不变），再调轮询频率与前端展示。
2. **匹配频率**：每帧匹配不抽样——720 流每帧 4.2ms，58fps 单核 ~25% 余量充足；抽样破坏连续确认/上升沿序列完整性，且错过瞬态画面。
3. **停表时间**：主机 `received_at` 为主、`pts` 辅助——与现有截图模式语义一致；pts 是设备编码时间戳，与主机差一个传输延迟且静止低帧率时跳变。未来可标定 `received_at - pts` 偏置，本期不做。
4. **Mac 打包资源缺口（H3）**：本期不修——接入后 Mac 打包产物缺 scrcpy-server 会走截图回退，功能不坏；且 mac 验证用开发模式与打包无关。H3 后置为独立小项。

**实施建议**：第一步先做纯重构（POC 协议解析 + BufferedStream + matcher 提取为共享模块 `scrcpy_stream.py`，45 项测试保持通过），零风险，可与 macOS 验证并行；第二步 ScrcpyStream（后台线程 + 生命周期 + 截图回退）；第三步 check_auto 双通道 + 回归 29 项测试。

## 第一阶段代码抽取（2026-08-06）

已完成纯重构，未接入 `server.py` / `check_auto`，不改变 POC 行为：

- 新增共享模块 `scripts/scrcpy_stream.py`：集中协议常量、`BufferedStream`、scrcpy 4.x 流头/会话头/媒体 packet 解析、`MarkerMatcher`、PyAV packet 解码和 BGR 帧处理。
- `scripts/scrcpy_frame_probe.py` 保留设备发现、adb reverse、server 子进程生命周期、duration 循环和统计逻辑；改为调用共享模块。
- 测试改为直接覆盖共享协议/matcher 实现，同时保留 `scrcpy_frame_probe.read_packet` 的兼容导入面。
- 系统 Python 测试：40 passed, 5 skipped；项目 `.venv`（含 OpenCV）测试：45 passed；两个模块 `py_compile` 通过。
- 已连接 Pixel 6a 实机冒烟：5.004s、288 帧、PTS FPS 56.926、PTS P50 16.875ms、解码+BGR P95 3.813ms，清理流程正常。

## 第二阶段 ScrcpyStream 运行时（2026-08-06）

已完成独立运行时骨架，仍未接入 `server.py` / `check_auto`：

- 新增 `scripts/scrcpy_runtime.py`：`ScrcpyStreamConfig` + `ScrcpyStream`，负责 adb reverse、推送/启动 scrcpy-server、socket 握手、后台 H.264 解码线程、最新帧缓存、回调、状态、断流错误、stop/reconnect 和资源清理。
- `scripts/scrcpy_stream.py` 新增 `DecodedFrame` 与 `decode_payload_frames`，POC 与运行时共享同一套解码和协议处理；补强 `BufferedStream` 在 packet payload 中途 timeout 后继续读取，避免丢失数据包同步。
- 新增 `tests/test_scrcpy_runtime.py`：fake socket/process/decoder 验证启动、握手、最新帧、状态和 stop 清理；另有缺失 server 快速失败测试。
- 测试：系统 Python `49 passed, 6 skipped`；项目 `.venv` `55 passed`。
- 实机运行时冒烟（Pixel 6a / scrcpy 4.0 / 720）：5 秒收到 287 帧，最新帧 324×720，单帧解码约 3.7ms，stop 后 adb reverse / 临时 server 清理正常。
- 修复运行时与 POC 的无效 `send_codec_meta=true` 参数；scrcpy 4.0 不再输出 `Unknown server option` 警告。

## 第三阶段接入（2026-08-06，第一轮已完成）

已完成第一轮最小接入，尚未作为正式测速结论发布：

- `Session.select()` 在 Android 设备切换时创建/停止 `ScrcpyStream`；iOS 不创建视频流；后端 lifespan 退出前先清理视频流，再 kill adb。
- `check_auto` 优先读取新鲜且与模板源分辨率一致的视频帧；不满足时完整回退原 ADB 截图路径。返回字段保持兼容，视频路径 `shot_via="video"`、`shot_ms` 使用帧龄。
- 启动模板/跳过模板优先从视频帧采集，并记录 `src_w/src_h`；旧项目缺少源分辨率时自动回退截图，不运行时缩放模板。
- 增加 `/api/stream_status` 诊断接口；requirements 加入 PyAV；Windows embed 构建不再删除 `av`/`av.libs`；Electron extraResources 携带两个 stream 脚本。
- 补充 Session 分辨率/新鲜度/设备切换/模板越界守卫测试；当前系统 Python `49 passed, 6 skipped`，项目 `.venv` `55 passed`。
- 已在 Pixel 6a 实机验证 `Session` 视频通道：scrcpy 4.0 / 720 流成功建立，`check_auto` 返回 `shot_via="video"`，匹配耗时约 1.3ms；该次人为构造模板仅用于通道验证，不是正式模板命中结论。

**Android 实机端到端验证已完成（2026-08-06）**：使用 Brick Blast 当前主界面采集同分辨率 324×720 模板，执行 `cold_start(pkg)` 后轮询 `check_auto(false)`；全程 `shot_via=["video"]`，主界面重新出现时命中 `confidence=0.9814`、`shot_ms=16.0ms`（约一帧龄）、`match_ms=1.6ms`。这证明正式 `server.py` 视频优先路径和冷启动状态机能够协同工作。该模板为本次实机临时模板，不代表项目模板已迁移完成。

**ADB 回退实测（2026-08-06）**：停止视频流后继续调用 `check_auto(false)`，返回 `shot_via="raw_gzip"`、`shot_ms=298.8ms`、`match_ms=2.3ms`、无 error；视频通道关闭没有破坏原截图路径。

**当前限制**：第三阶段代码已提交到 `f278d4b`；客户端错误修复和 Excel/报告 UI 改动随后提交为 `a9215ab`、`5c7174c`，均已推送。UX/UI 需求稿见 `docs/coldstart-client-ux-requirements.md`；当前已开始 Sprint 1（状态条、Electron 项目输入、跑测控件/快捷键锁定），基础版本已提交到 c8ffa3e，快捷键/菜单/清空记录锁定修复待提交。仍需执行打包后 `import av` 验证；Mac 实机仍待回家验证。

## 尚未完成（下一步候选）

1. 用真实项目模板跑 Android 视频路径冷启动，并与 ADB 回退路径对照。
2. 断流/拔线模拟：确认 `available=false` 后 `check_auto` 不报错并走截图；重新选择设备可重建流。
3. 检查并构建 Windows embed，确认 PyAV/FFmpeg DLL 未被清理；再提交第三阶段改动。
4. macOS 实机验证一次（通过标准未满足项）——清单见 `docs/scrcpy-frame-precision-mac-verify.md`。

## 已知历史结果

文档中的 Windows + Pixel 6a 约 58 FPS 结果是此前一次运行的历史记录；本次复测确认该帧率出现在内容变化（动画）期间，静止画面为 10fps。
