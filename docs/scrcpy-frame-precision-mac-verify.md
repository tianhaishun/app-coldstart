# scrcpy 帧精度 POC — macOS 实机验证清单

> 目标：完成 POC 通过标准中唯一未验证项（macOS 实机），并将结果回填到
> `docs/scrcpy-frame-precision-progress.md`。
>
> 对照基线：Windows + Pixel 6a 已完成（见进度文档），Mac 结果应与基线同一量级。

## 0. 前置条件

- [ ] Mac 上已运行过一次 `bash start-mac.sh`（或手动完成等价安装：brew scrcpy、brew android-platform-tools、`.venv` 及 requirements）
- [ ] 手机通过 USB 连接，`adb devices` 能看到设备
- [ ] 已在仓库根目录执行 POC 依赖安装：
      ```text
      .venv/bin/python -m pip install -r scripts/requirements-scrcpy-frame.txt
      ```
- [ ] 核对 scrcpy 版本（**第一步必做，决定 --scrcpy-version 取值**）：
      ```text
      scrcpy --version
      ```
      - brew 当前装的可能是 3.x 或 4.0，POC 默认值 `4.0` 不一定匹配
      - 同时确认 `scrcpy/scrcpy-server` 符号链接存在：`ls -l scrcpy/scrcpy-server`
      - 如果 brew 是 3.x：POC 的 12 字节头协议标志位布局与 4.0 不同（CONFIG/KEY 位错位一位），
        当前 POC 只实现 4.0 协议 → **建议先 `brew upgrade scrcpy` 到 4.0**，或记录为已知问题
- [ ] 记录：Mac 型号 / macOS 版本 / scrcpy 版本 / 设备型号，填入结果回填节

## 1. 链路连通（静止画面）

```text
.venv/bin/python scripts/scrcpy_frame_probe.py --serial 设备序列号 \
  --server scrcpy/scrcpy-server --duration 10 --max-fps 60 --max-size 720 \
  --scrcpy-version 实际版本
```

- 预期：输出 `device: ... stream=...`，duration 10 秒左右结束（不应长时间挂起）
- 通过判定：
  - `duration_s` 在 10~11 之间（修复后的读取超时生效）
  - 静止画面帧率可能是 10fps（Android 合成降帧，设备正常行为，不是丢帧）
  - 无 `[FAIL]`、无断流错误

## 2. 动画帧率

```text
.venv/bin/python scripts/scrcpy_frame_probe.py --serial 设备序列号 \
  --server scrcpy/scrcpy-server --duration 15 --max-fps 60 --max-size 720 \
  --scrcpy-version 实际版本 &
# 运行期间执行 2~3 次滑动制造动画：
adb -s 设备序列号 shell input swipe 540 1800 540 600 300
```

- 通过判定（对照 Windows 基线：PTS 间隔 P50 ≈17ms、PTS FPS 15~20 混合）：
  - 动画期间 PTS 间隔 P50 ≤ 20ms（≈50~60fps）
  - 解码 + BGR P95 < 10ms
  - `pts_fps` / `receive_fps` 接近（无接收瓶颈）

## 3. 模板匹配（真实画面）

```text
adb -s 设备序列号 exec-out screencap -p > ~/screen.png
# 用 Python 裁剪画面中一个稳定、纹理丰富的区域（非纯色，std>15）：
.venv/bin/python - <<'EOF'
import cv2
img = cv2.imread("~/screen.png")  # 路径按实际调整
roi = img[y1:y2, x1:x2]           # 选一个稳定区域，如应用标题
cv2.imwrite("~/template.png", roi)
print(roi.shape, "std=%.1f" % float(roi.std()))
EOF
```

```text
.venv/bin/python scripts/scrcpy_frame_probe.py --serial 设备序列号 \
  --server scrcpy/scrcpy-server --duration 30 --max-fps 60 --max-size 0 \
  --scrcpy-version 实际版本 \
  --template ~/template.png --template-cx 0.5 --template-cy 0.5 \
  --template-padding 20 --template-threshold 0.85
```

- 通过判定：
  - `marker_checked_frames > 0`，静止画面时 `marker_hit_frames > 0`
  - 静止画面 `marker_best_confidence ≥ 0.95`
  - `marker_full_frame_fallbacks` 说明：1080 模板配 720 流会全屏回退并得到 0 置信（正常防护），
    模板必须与视频流同分辨率（用 `--max-size 0`）

## 4. 端到端冷启动（核心语义验证）

```text
# 1) 打开目标应用，等主界面稳定，截图并裁剪模板（同第 3 步）
adb -s 设备序列号 shell am force-stop 包名
# 2) 后台启动 probe（带模板、--stop-on-hit），数秒后启动应用：
.venv/bin/python scripts/scrcpy_frame_probe.py --serial 设备序列号 \
  --server scrcpy/scrcpy-server --duration 60 --max-fps 60 --max-size 0 \
  --scrcpy-version 实际版本 --template ~/template.png \
  --template-cx 0.5 --template-cy 0.5 --stop-on-hit &
sleep 4
adb -s 设备序列号 shell monkey -p 包名 -c android.intent.category.LAUNCHER 1
```

- 通过判定：
  - 启动画面期间不输出 `marker_hit`
  - 主界面出现后输出 `marker_hit`（confidence ≥ 0.85）并以退出码 0 结束
  - 对照 Windows：Brick Blast 214 帧启动画面未命中 → 第 215 帧命中

## 5. 10 分钟长跑 + 内存

```text
.venv/bin/python scripts/scrcpy_frame_probe.py --serial 设备序列号 \
  --server scrcpy/scrcpy-server --duration 600 --max-fps 60 --max-size 720 \
  --scrcpy-version 实际版本 > ~/longrun.json 2>&1 &
# 每 60 秒采样一次 probe 进程内存（Mac 用 ps，注意取 python 解释器进程）：
ps -o pid,rss,etime -p <probe_pid>
```

- 通过判定：
  - `duration_s ≈ 600`，无断流（probe_exit=0）
  - 全程不显著丢帧（PTS FPS 与动画场景匹配）
  - 内存：启动与结束差 < 30MB（Windows 基线 +3MB）
- 注意：前台画面要有内容变化（游戏/动画），静止画面帧率低不代表链路问题

## 6. 回填

- [ ] 把每步实测数值填入 `docs/scrcpy-frame-precision-progress.md` 的
      "当前状态 / 通过标准对照"：macOS 实机从"待做"改为"通过"
- [ ] 提交并推送：
      ```text
      git add -A && git commit -m "docs: macOS 实机验证结果回填"
      git push 7k7k scrcpy-frame-precision
      ```

## 常见问题

| 现象 | 处理 |
|---|---|
| `--scrcpy-version` 不匹配导致错流/垃圾统计 | 核对 `scrcpy --version`，POC 当前只支持 4.0 协议；3.x 需先升级或记入已知问题 |
| PyAV 安装失败 | `brew install ffmpeg` 后再装 `av`；或确认 `--only-binary :all:` 拉预编译包 |
| `adb` 找不到 | `brew install android-platform-tools`，或 export PATH |
| 中文输出乱码 | POC 已重配置 UTF-8；若终端显示异常用 `LANG=en_US.UTF-8` 运行 |
| 屏幕长时间静止帧率低 | 设备正常行为（10fps），不是丢帧；判定以动画场景为准 |
