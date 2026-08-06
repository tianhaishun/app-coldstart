# scrcpy 视频帧精度 POC

本分支只验证 scrcpy 视频帧链路，不接入现有自动测速状态机。

## 目标

验证 Windows/macOS 能否从 scrcpy 视频 socket 持续得到带 PTS 的 H.264 帧，并统计：

- 实际解码帧数 / FPS；
- scrcpy PTS 帧间隔 P50/P95/max；
- 主机收到帧的间隔；
- H.264 解码 + BGR 转换耗时；
- 每帧 OpenCV ROI 模板匹配耗时（提供模板时）；
- 命中帧数量、最高置信度和全屏回退次数；
- 连续运行时是否断流。

## 当前实现

`scripts/scrcpy_frame_probe.py`：

1. 用 `adb reverse` 建立本机 TCP 端口到设备 `localabstract:scrcpy_<scid>` 的通道；
2. 推送并启动与客户端版本匹配的项目内 `scrcpy-server`（当前 Windows 工作副本为 4.0）；
3. 读取 scrcpy 4.x 的 codec 头、session 分辨率头和 12 字节媒体 packet header；
4. 解析 PTS、packet size 和关键帧标志；
5. PyAV 解码并转换 BGR；
6. 如果传入 `--template`，对每个解码 BGR 帧执行 OpenCV ROI `TM_CCOEFF_NORMED` 匹配，并输出命中帧；
7. 输出 JSON 统计。

示例（模板中心坐标为归一化坐标）：

```text
.venv\\Scripts\\python.exe scripts\\scrcpy_frame_probe.py \\
  --serial 设备序列号 \\
  --template projects\\项目\\marker.png \\
  --template-cx 0.5 --template-cy 0.5 \\
  --template-padding 20 --template-threshold 0.85 \\
  --stop-on-hit
```

POC 的 OpenCV matcher 与生产 `_match_template_in_scene()` 保持相同原则：固定模板中心、局部 ROI、`TM_CCOEFF_NORMED`、0.85 阈值和纯色模板拒绝；上升沿/连续确认尚未接入 POC。

POC 不修改 `server.py`，不修改 `check_auto`，不写历史数据。

## 安装 POC 依赖

在现有 Python venv 中执行：

```text
.venv\\Scripts\\python.exe -m pip install -r scripts/requirements-scrcpy-frame.txt
```

Mac：

```text
.venv/bin/python -m pip install -r scripts/requirements-scrcpy-frame.txt
```

## 运行

Windows：

```text
.venv\\Scripts\\python.exe scripts\\scrcpy_frame_probe.py --serial 设备序列号 --duration 10 --max-fps 60 --scrcpy-version 4.0
```

Mac：

```text
.venv/bin/python scripts/scrcpy_frame_probe.py --serial 设备序列号 --duration 10 --max-fps 60 --scrcpy-version 对应版本
```

默认读取：

```text
scrcpy/scrcpy-server
```

也可以显式传入：

```text
--server path/to/scrcpy-server
```

## 当前 Windows 实机结果

设备：Pixel 6a，scrcpy 4.0，目标 `--max-fps 60`，持续 10 秒。

```text
decoded_frames: 582
PTS FPS: 57.941
主机接收 FPS: 58.199
PTS 帧间隔 P50: 16.689ms
PTS 帧间隔 P95: 20.512ms
PTS 帧间隔 max: 34.324ms
接收间隔 P50: 16.843ms
接收间隔 P95: 25.740ms
解码 + BGR P50: 4.399ms
解码 + BGR P95: 9.202ms
解码 + BGR max: 72.876ms
```

该结果证明当前 Windows + Pixel 6a 已达到接近 60fps 的视频帧采样，但这次运行没有提供模板，因此 `marker_checked_frames=0`。这不是最终停表精度结论。

追加实测：Samsung SM-A145M（1080×2408，目标 60fps，5 秒，使用临时静态模板）：

```text
decoded_frames: 143
PTS FPS: 28.046
主机接收 FPS: 28.511
PTS 帧间隔 P50: 32.613ms
PTS 帧间隔 P95: 56.490ms
解码 + BGR P50: 6.416ms
解码 + BGR P95: 8.685ms
OpenCV ROI 匹配帧数: 143
OpenCV match_ms P50: 1.615ms
OpenCV match_ms P95: 2.167ms
最高置信度: 0.1988（本次画面未命中临时模板）
```

这说明视频帧率受设备/编码链路影响：Pixel 6a 本次接近 58fps，Samsung 本次约 28fps；OpenCV ROI 匹配仍保持约 1-2ms 级别。该结果不是最终停表精度结论。

## 通过标准

POC 阶段目标，不代表最终测量精度承诺：

- 实际 FPS ≥ 30；目标 60 FPS；
- PTS 帧间隔 P95 ≤ 50ms；
- 丢帧率可被检测并记录；
- 解码 + BGR 转换 P95 < 10ms；
- 连续 10 分钟运行无明显内存持续增长；
- Windows 和 macOS 各完成一次实机验证；
- 失败时不影响现有 ADB 截图测速。

## 已知边界

- PTS 是设备视频编码时间戳，不等于屏幕物理扫描输出时间；
- 高帧率视频采样不自动等于绝对 ±1ms 精度；
- POC 通过 adb reverse 连接 scrcpy 内部视频 socket，依赖 scrcpy 客户端与 server 的版本匹配；默认版本是当前 Windows 工作副本的 4.0，可用 `--scrcpy-version` 覆盖；
- 该协议不是当前生产 API，升级 scrcpy 版本时必须重新验证；
- POC 通过 `av` 解码 H.264，生产接入前需评估 Python/FFmpeg 的分发和资源消耗。

## 当前结论

当前视频帧 POC 已经证明：

```text
ADB 截图约 3Hz
scrcpy 视频帧约 58Hz
```

下一步仍需用真实启动成功模板运行 OpenCV 每帧匹配，并对比 ADB 模式和外部高帧率视频结果；目前不接入正式自动测速。
