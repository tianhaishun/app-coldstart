# AdbHelper 设计与使用说明

## 目标

将 Android 设备相关操作集中到 `adb_helper.py`，避免业务层分散拼接 adb 命令，并统一 Windows/macOS 的可执行文件路径、超时、错误和高精度计时行为。

## 模块

### `AdbHelper`

负责：

- 自动解析 `adb` 路径：显式参数 → `ADB` 环境变量 → 项目目录 `adb/adb.exe` 或 `adb/adb` → PATH；
- 设备状态、设备列表、屏幕尺寸；
- raw gzip / PNG 截图；
- tap、keyevent、force-stop、kill-all、包名启动；
- APK install/uninstall/reinstall；
- 使用 `aapt`/`aapt2` 解析 APK package、版本号和显示名称；
- 所有子进程使用参数数组，不经过 shell，并设置 timeout。

### `TemplateMatcher`

负责：

- 固定归一化坐标 ROI；
- `TM_CCOEFF_NORMED` 匹配；
- 纯色模板拒绝；
- 模板大于画面时返回未命中，不抛 OpenCV 裸异常；
- 返回置信度、ROI 尺寸和匹配耗时。

### `ScreenPoller`

负责按固定间隔执行：

```text
perf_counter 记录开始
→ AdbHelper.screenshot_bgr()
→ TemplateMatcher.match()
→ 生成 ScreenSample
→ 回调 on_sample
→ 等待到下一个 perf_counter tick
```

特点：

- `interval_ms=N` 表示目标轮询周期；
- 使用单线程串行执行，避免多个截图请求同时抢 adb；
- 截图耗时超过周期时不堆积请求，下一轮从当前时间重新对齐；
- `ScreenSample.started_at/captured_at/capture_ms` 均使用 `time.perf_counter()` 口径；
- `PerfTimer` 也只使用 `time.perf_counter()`，不使用系统墙钟做耗时计算。

## 示例

```python
from adb_helper import AdbHelper, ScreenPoller, TemplateMatcher

adb = AdbHelper(serial="设备序列号")
matcher = TemplateMatcher.from_file(
    "marker.png", 0.5, 0.5, padding=20, threshold=0.85
)

poller = ScreenPoller(
    adb,
    interval_ms=100,
    matcher=matcher,
    on_sample=lambda sample: print(
        sample.sequence,
        sample.captured_at,
        sample.match.confidence if sample.match else None,
    ),
)
try:
    poller.start()
    # 业务线程执行冷启动；命中由 on_sample 或外部状态机消费。
finally:
    poller.stop()
```

## APK 解析

`AdbHelper.parse_apk()` 需要系统存在 `aapt` 或 `aapt2`。查找顺序包括：

- `AAPT` / `AAPT2` 环境变量；
- `ANDROID_HOME/build-tools/*/aapt[2]`；
- Windows PATH；
- macOS PATH；
- 项目 `build-tools/` 目录。

服务端提供：

```text
POST /api/parse_apk
{"apk_path": ".../app.apk"}
```

该接口只解析元数据，不安装 APK。

## 接入规则

- 现有 `server.py` 的 `AdbDevice` 旧路径暂时保留，避免一次性大范围替换稳定测速逻辑；
- `AdbHelper` 先作为独立、可测试的公共组件使用；
- 新功能优先使用 `AdbHelper`；后续再将 `AdbDevice` 改为兼容适配器；
- iOS 不使用 `AdbHelper`，继续使用 `IosDevice`；
- `ScreenPoller` 是截图轮询模式，不替代 scrcpy 高帧率视频流；视频流路径继续由独立的 `ScrcpyStream` 管理。

## 验收标准

- Windows 和 macOS 路径解析不依赖硬编码 `.exe`；
- adb 子进程超时会转为 `AdbHelperError`；
- APK 元数据解析失败给出可理解错误；
- N 毫秒轮询不产生并发截图堆积；
- 所有耗时使用 `time.perf_counter()`；
- 单元测试覆盖命令拼接、超时、aapt 输出、模板匹配和轮询回调。
