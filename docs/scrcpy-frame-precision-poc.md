# scrcpy 视频帧精度 POC

本分支只验证 scrcpy 视频帧链路，不接入现有自动测速状态机。

## 目标

验证 Windows/macOS 能否从 scrcpy 3.3 视频 socket 持续得到带 PTS 的 H.264 帧，并统计：

- 实际解码帧数 / FPS
- scrcpy PTS 帧间隔 P50/P95/max
- 主机收到帧的间隔
- H.264 解码 + BGR 转换耗时
- 连续运行时是否断流

## 当前实现

`scripts/scrcpy_frame_probe.py`：

1. 用 `adb reverse` 建立本机 TCP 端口到设备 `localabstract:scrcpy_<scid>` 的通道；
2. 推送并启动项目内的 `scrcpy-server` 3.3；
3. 读取 scrcpy 3.3 的 dummy byte、设备信息、H.264 codec/分辨率头；
4. 按 scrcpy 12 字节 packet header 读取 PTS、packet size 和关键帧标志；
5. PyAV 解码并转换 BGR；
6. 输出 JSON 统计。

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
.venv\\Scripts\\python.exe scripts\\scrcpy_frame_probe.py --serial 设备序列号 --duration 10 --max-fps 60
```

Mac：

```text
.venv/bin/python scripts/scrcpy_frame_probe.py --serial 设备序列号 --duration 10 --max-fps 60
```

默认读取：

```text
scrcpy/scrcpy-server
```

也可以显式传入：

```text
--server path/to/scrcpy-server
```

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
- POC 通过 adb reverse 连接 scrcpy 内部视频 socket，依赖 scrcpy 3.3 协议；
- 该协议不是当前生产 API，升级 scrcpy 版本时必须重新验证；
- POC 通过 `av` 解码 H.264，生产接入前需评估 Python/FFmpeg 的分发和资源消耗。
