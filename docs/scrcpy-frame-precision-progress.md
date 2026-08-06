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

## 尚未完成（下一步候选）

1. 评估 OpenCV 匹配耗时策略（约 10ms/帧 在 60fps 下的 CPU 占比），决定生产接入的分辨率与模板尺寸。
2. 连续运行 10 分钟检查内存增长（POC 通过标准之一）。
3. 将 POC 结论对照通过标准逐项确认后，再讨论接入正式测速状态机（不影响现有 ADB 截图测速）。

## 已知历史结果

文档中的 Windows + Pixel 6a 约 58 FPS 结果是此前一次运行的历史记录；本次复测确认该帧率出现在内容变化（动画）期间，静止画面为 10fps。
