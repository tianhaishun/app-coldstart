"""App 冷启测速 —— FastAPI 后端。

参考 GameAuto（D:\\work\\GameAuto）的设计哲学：
  - 自动测速热路径截图：`adb exec-out sh -c 'screencap | gzip -1 -c'`（raw+gzip，
    Pixel 6a 实测 ~350ms，优于 `screencap -p` 的 ~580ms）；失败回退 PNG
  - 落盘/模板/直播仍可用 `screencap -p` PNG
  - OCR 用 RapidOCR（ONNX，跨平台，归一化坐标输出）
  - 所有 adb 调用集中在 adb_helper.AdbHelper（跨平台封装），便于复用 + 加锁
  - 实时画面走截图轮询（不做 scrcpy 流，保持简单可靠）

v1 → v2 的关键修复：
  - **计时精度**：新增 `/api/cold_start`，服务端编排 force_stop + tap/launch 一气呵成；
    计时用 v1 验证过的纯前端 `performance.now()` 方案（响应回来后直接打点），
    单一时钟不校准 —— 简单到一眼能看懂，不会有混时钟的 bug（见 AGENTS.md §6）。
    cold_start 仍返回 `start_wall` 字段供诊断/将来用，但前端计时**不消费它**。
  - **错误处理**：所有 adb 异常都被映射为 HTTP 400/500，前端不会 fetch 挂起。
  - **并发**：所有设备 I/O 走 threading.Lock 串行化，避免 adb server 抢占。
"""

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager, contextmanager
import gzip
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Literal, Any
from urllib.parse import quote

# uvicorn 加载本模块时把根目录加进 sys.path，便于 from server import ...
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 内置 adb（同目录 adb\\adb.exe），找不到再回退 PATH
try:
    from adb_helper import AdbHelper, AdbHelperError, TemplateMatcher
except ImportError:
    AdbHelper = None
    AdbHelperError = RuntimeError
    TemplateMatcher = None

# Windows 打包内置 adb/adb.exe；macOS/Linux 仓库里只有 Windows 二进制（PE 格式
# 在 mac 上会 PermissionError），必须走 AdbHelper 解析（项目 adb/adb → PATH，
# 即 brew android-platform-tools）。
_BUNDLED_ADB = ROOT / "adb" / "adb.exe"
if os.name == "nt" and _BUNDLED_ADB.exists():
    ADB_EXE = str(_BUNDLED_ADB)
else:
    try:
        ADB_EXE = AdbHelper.resolve_adb_path(project_root=ROOT) if AdbHelper else "adb"
    except (AdbHelperError, OSError):
        ADB_EXE = "adb"

# 内置 iOS 工具链（同目录 ios\\idevice_id.exe；打包后 ROOT 落在 resources/backend，
# ios/ 在 extraResources 里）。Windows 打包内置；macOS/Linux 依赖 brew 的
# libimobiledevice（idevice_id 在 PATH 里）——不按平台解析会导致 Mac 上
# IosDevice.devices() 因 IDEVICE_ID_EXE=None 恒返回空列表（iOS 设备检测不到）。
_BUNDLED_IDEVICE_ID = ROOT / "ios" / "idevice_id.exe"
if _BUNDLED_IDEVICE_ID.exists():
    IDEVICE_ID_EXE = str(_BUNDLED_IDEVICE_ID)
else:
    IDEVICE_ID_EXE = shutil.which("idevice_id")

# APK 上传目录（每次上传保留原始文件名，不再覆盖式存储）。
# 复数 _cst_uploads 与老的单数 _cst_upload.apk 区分；启动时整目录清空重建。
APK_UPLOAD_DIR = Path(tempfile.gettempdir()) / "_cst_uploads"

# 启动成功模板图（cv2.matchTemplate 区域比对用）。多设备（v4）起按设备隔离存
# tempdir/_cst_marker_<safe_serial>.png（见 DeviceSession.marker_path），不再用单文件。
# 用户在画面上点"启动成功"元素位置 → 后端截小区域存为模板 → 运行时只搜小区域。
# 比 OCR 文字匹配快约 20-40 倍（毫秒级 vs 秒级），冷启动计时精度大幅提升。
MARKER_MATCH_THRESHOLD = 0.85   # cv2.matchTemplate 命中阈值（TM_CCOEFF_NORMED，0~1）
MARKER_SEARCH_PADDING = 20      # 模板坐标周围搜索范围（像素，容忍 UI 轻微位移）
MARKER_DEFAULT_W = 240          # 默认模板宽（用户没传 box_w 时用）
MARKER_DEFAULT_H = 120          # 默认模板高
# 停表可信：连续 N 帧过阈才算命中（抗动画闪一下）；需先见过低于阈值（上升沿，抗桌面残留）
# 2026-07：改为 1——每帧截图 ~0.3–0.6s，2 帧确认会白白多等一轮；上升沿仍保留防误停
# 上升沿例外：cold_start 在 force_stop 之后 reset_marker_watch(after_force_stop=True)，
# 直接种 _marker_seen_below=True（刚杀过进程，不可能还停在启动成功页）。
# 否则二次冷启动无 SKIP 可种 below，首帧就 100% 时会永远卡在「等上升沿」。
MARKER_CONFIRM_FRAMES = 1
MARKER_REQUIRE_RISING_EDGE = True

# 跳过弹窗模板（通知权限「允许/不允许」等）：命中后自动点击，不停表
SKIP_TEMPLATE_MAX = 3
SKIP_MATCH_THRESHOLD = 0.85
SKIP_SEARCH_PADDING = 40        # 弹窗按钮位移通常比启动元素大一点
SKIP_DEFAULT_W = 200            # 按钮区域默认比启动元素略窄
SKIP_DEFAULT_H = 80
SKIP_TAP_COOLDOWN_S = 1.5       # 同一跳过模板点击冷却，防连点

# 项目持久化（启动模板 / 跳过模板 / 包名等）。不存 APK 本体——每次测试自行上传。
# 目录：<仓库>/projects/<id>/meta.json + marker.png + skip_*.png
# 打包后 ROOT 落在 asar 只读区，mkdir 会抛 ReadOnlyError；Electron 启动时通过
# 环境变量 CST_PROJECTS_DIR 注入可写路径（userData/projects）。开发模式不设则回退仓库根，零回归。
_projects_env = os.environ.get("CST_PROJECTS_DIR")
PROJECTS_DIR = Path(_projects_env) if _projects_env else (ROOT / "projects")
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

# 设备模板持久化目录（真机实测痛点修复）：模板是内存态，后端重启即丢，
# 用户反复重设体验差。设模板时同步落盘，DeviceSession 创建时自动恢复。
# 注意：启动清理（_cleanup_stale_temp_files）只清 tempdir，不动这里。
DEVICE_TEMPLATES_DIR = PROJECTS_DIR / "_device_templates"
DEVICE_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)


def _safe_apk_filename(original: str) -> str:
    """把用户上传的原始文件名过滤成安全的磁盘文件名。

    防御点（可信度要求高，路径攻击必须拦）：
      - 只取 basename：防 ``../../evil.apk`` 路径穿越写到任意目录
      - 反斜杠统一转正斜杠再取 basename：Windows 风格 ``..\\..\\evil.apk`` 在
        macOS/Linux 上 os.path.basename 不认反斜杠，必须先归一化（跨平台一致）
      - 非 [A-Za-z0-9_\\-.] 字符（含中文/空格/特殊符号）替换为 ``_``：避免文件系统/adb 命令解析问题
      - 强制 .apk 后缀
      - 过滤后为空或过短，用 ``apk_<short>`` 兜底
      - 与目录内已有文件同名时追加短 hash 后缀（不覆盖，保历史）
    返回值仅为文件名（不含路径），调用方拼到 APK_UPLOAD_DIR 下。
    """
    name = os.path.basename((original or "").replace("\\", "/")).strip()
    if not name:
        name = "apk_upload.apk"
    # 强制 .apk 后缀（无论原名后缀是什么）
    stem = name
    if stem.lower().endswith(".apk"):
        stem = stem[:-4]
    # 非 ASCII 字母数字/下划线/连字符/点 → 下划线
    safe_stem = re.sub(r"[^A-Za-z0-9_\-.]", "_", stem)
    # 去掉开头的点（防隐藏文件 / 多余分隔）
    safe_stem = safe_stem.lstrip(".")
    # 空或全是下划线 → 兜底名
    if not safe_stem or set(safe_stem) == {"_"}:
        safe_stem = "apk_upload"
    candidate = f"{safe_stem}.apk"
    # 同名冲突 → 追加 6 位短 hash
    if (APK_UPLOAD_DIR / candidate).exists():
        short = hashlib.md5(f"{candidate}{time.time()}".encode()).hexdigest()[:6]
        candidate = f"{safe_stem}_{short}.apk"
    return candidate


# ── OCR ────────────────────────────────────────────────────────────────


@dataclass
class OcrItem:
    """单条 OCR 命中。cx/cy/w/h 都是归一化（0~1，原点左上）。"""

    text: str
    cx: float
    cy: float
    w: float
    h: float
    confidence: float = 1.0


class OcrEngine:
    """RapidOCR 懒加载封装。首次识别加载 ONNX 模型约 2 秒，之后复用。"""

    def __init__(self) -> None:
        self._engine = None
        self._lock = threading.Lock()

    def _get(self):
        if self._engine is not None:
            return self._engine
        with self._lock:
            if self._engine is not None:
                return self._engine
            try:
                from rapidocr_onnxruntime import RapidOCR
            except ModuleNotFoundError as e:
                raise RuntimeError(
                    f"RapidOCR 未安装：{e}。请运行 pip install -r requirements.txt"
                ) from e
            print("[ocr] 首次初始化 RapidOCR（加载 ONNX 模型，约 2 秒）...", file=sys.stderr, flush=True)
            self._engine = RapidOCR()
            return self._engine

    def recognize(self, image_path: Path) -> list[OcrItem]:
        import cv2

        engine = self._get()
        # 多设备并发 OCR：RapidOCR 单引擎推理不保证线程安全，识别整体加锁串行
        with self._lock:
            return self._recognize_locked(engine, image_path)

    def _recognize_locked(self, engine, image_path: Path) -> list[OcrItem]:
        import cv2

        img = cv2.imread(str(image_path))
        if img is None:
            raise RuntimeError(f"cv2 读图失败：{image_path}")
        h_px, w_px = img.shape[:2]
        if h_px == 0 or w_px == 0:
            return []

        result, _elapse = engine(img)
        if not result:
            return []

        items: list[OcrItem] = []
        for entry in result:
            try:
                box, text, conf = entry[0], entry[1], entry[2]
            except (IndexError, ValueError):
                continue
            if not text:
                continue
            try:
                conf_f = float(conf)
            except (TypeError, ValueError):
                conf_f = 1.0
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            x_min, x_max = min(xs), max(xs)
            y_min, y_max = min(ys), max(ys)
            bw_px = x_max - x_min
            bh_px = y_max - y_min
            items.append(
                OcrItem(
                    text=str(text),
                    cx=(x_min + bw_px / 2) / w_px,
                    cy=(y_min + bh_px / 2) / h_px,
                    w=bw_px / w_px,
                    h=bh_px / h_px,
                    confidence=conf_f,
                )
            )
        return items


def _check_amds() -> dict:
    """检测 Apple Mobile Device Service 状态。

    Windows 上 iOS USB 通信依赖 AMDS（本质是 usbmuxd）。服务缺失或未运行时
    idevice_id / pymobiledevice3 都无法发现设备。返回诊断信息供前端展示。
    """
    try:
        cp = subprocess.run(
            ["sc", "query", "Apple Mobile Device Service"],
            capture_output=True, timeout=3, text=True,
        )
        output = cp.stdout + cp.stderr
        if "RUNNING" in output:
            return {"installed": True, "running": True}
        if "SERVICE_NAME" in output:
            return {"installed": True, "running": False,
                    "hint": "AMDS 已安装但未运行，请在「服务」管理器中启动 Apple Mobile Device Service"}
        return {"installed": False, "running": False,
                "hint": "未检测到 AMDS。请安装 iTunes 或 AppleMobileDeviceSupport64.msi"}
    except Exception:
        return {"installed": False, "running": False, "hint": "AMDS 检测失败"}


# ── iOS 设备（pymobiledevice3）─────────────────────────────────────────


def _ios_async(coro):
    """在同步端点中运行 pymobiledevice3 异步调用。

    FastAPI 的 sync 端点跑在线程池里（不在 event loop 中），
    可以安全地用 asyncio.run() 创建临时 loop。
    每次调用创建新连接（略低效但简单可靠）。
    """
    return asyncio.run(coro)


# userspace tunnel 是进程级单例（PyTCP 栈全局，一个进程只能一个活跃隧道，
# 实测报错 "a userspace tunnel is already active in this process"）——
# 全局只维护一个活跃隧道，换设备（serial 不同）时关旧建新。
_ios_tunnel_state: dict = {"rsd": None, "tunnel": None, "serial": None}
_ios_tunnel_lock = threading.Lock()


class IosLoop:
    """iOS 操作常驻 event loop（单例，后台线程）。

    tunnel 复用（真机实测优化）的前提：asyncio 对象（套接字/连接）绑定创建它的
    event loop——asyncio.run 每次新建 loop，跨调用复用会失败。常驻 loop 让
    tunnel 在多次截图/启动调用间存活，省掉每次重建的开销（实测基线 ~400ms/帧）。
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._loop = asyncio.new_event_loop()
                cls._instance._thread = threading.Thread(
                    target=cls._instance._run, name="ios-loop", daemon=True,
                )
                cls._instance._thread.start()
            return cls._instance

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro, timeout: float = 120.0):
        """把协程提交到常驻 loop 并阻塞等待结果（线程安全）。"""
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)


class IosDevice:
    """iOS 设备操作（通过 pymobiledevice3 / lockdown 协议）。

    与 AdbHelper 平行，但底层不用 ADB，而是通过 usbmuxd → lockdown。
    非越狱设备限制：
      - 无模拟点击（tap/swipe 不可用，调用时抛 AdbError 提示）
      - 无程序化杀进程（force_stop 是 no-op + 警告日志）
      - 截图、安装/卸载、设备检测、App 启动均可用
    """

    def __init__(self, udid: str) -> None:
        self.udid = udid
        self._last_size: Optional[tuple[int, int]] = None
        # 注：tunnel 是进程级单例（_ios_tunnel_state），不再挂在实例上

    async def _get_lockdown(self):
        """创建到设备的 lockdown 连接。"""
        from pymobiledevice3.lockdown import create_using_usbmux
        return await create_using_usbmux(serial=self.udid)

    # ── 设备列表（idevice_id -l CLI，同步简单可靠）──
    @staticmethod
    def devices() -> list[dict]:
        """列出通过 USB 连接的 iOS 设备。

        直接调 idevice_id -l（同步 CLI），不依赖 pymobiledevice3 异步 API。
        UDID 列表拿到后，尝试用 pymobiledevice3 取设备名（失败则显示 UDID）。
        """
        if not IDEVICE_ID_EXE:
            return []
        try:
            cp = subprocess.run(
                [IDEVICE_ID_EXE, "-l"],
                capture_output=True, timeout=3,
            )
            # UDID 正则：^[0-9a-fA-F-]{8,}$
            udids = [
                line.strip()
                for line in cp.stdout.decode("utf-8", "replace").splitlines()
                if re.match(r"^[0-9a-fA-F-]{8,}$", line.strip())
            ]
            result = []
            for udid in udids:
                model = "iOS 设备"
                # 尝试用 pymobiledevice3 取设备名（可选，失败不致命）
                try:
                    async def _get_name(u=udid):
                        from pymobiledevice3.lockdown import create_using_usbmux
                        ld = await create_using_usbmux(serial=u)
                        name = ld.all_values.get('DeviceName', '') or \
                               ld.all_values.get('ProductType', '')
                        return name
                    model = _ios_async(_get_name()) or model
                except Exception:
                    pass
                result.append({
                    "serial": udid,
                    "state": "device",
                    "model": f"{model} (iOS)" if model != "iOS 设备" else model,
                    "platform": "ios",
                })
            return result
        except Exception:
            return []

    # ── 截图 ──
    # iOS 26 起 lockdown screenshotr 服务被移除/禁用（InvalidService），截图必须走
    # DVT（instruments）服务 + RSD tunnel（userspace 免 root，iOS 17+ 开发者服务标配）。
    # tunnel 复用（真机实测基线 ~400ms/帧）：隧道在常驻 loop 内建立并缓存，
    # 多次调用复用；连接失效（设备重连）时自动重建一次。实测对比见 git 提交说明。

    def _ensure_ios_rsd(self):
        """返回进程级单例 RSD（tunnel 复用）；换设备/失效时关旧建新。

        userspace tunnel 是进程级单例（PyTCP 栈全局），不能每会话一个——
        实测 "a userspace tunnel is already active in this process"。加锁保护
        多设备并发切换。须在常驻 loop 内调用。
        """
        with _ios_tunnel_lock:
            st = _ios_tunnel_state
            if st["rsd"] is not None and st["serial"] == self.udid:
                return st["rsd"]
            # 换设备或首次：关闭旧隧道（async with 语义 = aclose），建新
            if st["tunnel"] is not None:
                try:
                    IosLoop().run(st["tunnel"].aclose())
                except Exception:
                    pass
                st["rsd"] = st["tunnel"] = st["serial"] = None
            from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel
            tunnel = UserspaceRsdTunnel(serial=self.udid)
            st["rsd"] = IosLoop().run(tunnel.aopen())
            st["tunnel"] = tunnel
            st["serial"] = self.udid
            return st["rsd"]

    def _invalidate_ios_tunnel(self) -> None:
        """tunnel 失效（连接失败/设备重连）：先关闭旧隧道再清缓存，下次调用重建。

        多设备并行实测抓到的竞态：只清缓存不 aclose 时，PyTCP 进程级单例栈
        仍持有旧隧道对象——后续新建隧道必然报 "a userspace tunnel is already
        active in this process"（直播链 in-flight 截图失败 → invalidate →
        循环 launch 撞单例）。
        """
        with _ios_tunnel_lock:
            st = _ios_tunnel_state
            if st["tunnel"] is not None:
                try:
                    IosLoop().run(st["tunnel"].aclose())
                except Exception:
                    pass
            st["rsd"] = st["tunnel"] = st["serial"] = None

    def _dvt_shot(self) -> bytes:
        """DVT 截图（tunnel 复用；连接失败清缓存重建一次）。"""
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        from pymobiledevice3.services.dvt.instruments.screenshot import Screenshot

        async def _inner(rsd):
            async with DvtProvider(rsd) as dvt:
                async with Screenshot(dvt) as shot:
                    return await shot.get_screenshot()

        def _do():
            rsd = self._ensure_ios_rsd()
            return IosLoop().run(_inner(rsd))

        try:
            return _do()
        except Exception:
            # 隧道可能已失效（设备重连/系统睡眠）：重建一次，仍失败则如实抛错
            self._invalidate_ios_tunnel()
            return _do()

    def screenshot(self, target: Optional[Path] = None) -> Path:
        """截图（PNG），保存到 target 或临时文件。"""
        if target is None:
            fd, name = tempfile.mkstemp(prefix="_cst_ios_", suffix=".png")
            os.close(fd)
            target = Path(name)

        data = self._dvt_shot()
        if not data or len(data) < 1024:
            raise AdbError("iOS 截图失败：返回数据为空")
        target.write_bytes(data)
        return target

    def screenshot_bgr(self) -> tuple[Any, str]:
        """截图 → BGR ndarray（自动测速热路径，模板比对用）。

        iOS 截图返回 PNG（DVT 路径为 16-bit RGB），cv2.imdecode 后统一转 8-bit，
        保证与 Android 路径的模板/匹配/预览链路一致（imencode JPG 只支持 8-bit）。
        """
        import cv2
        import numpy as np

        data = self._dvt_shot()
        if not data or len(data) < 1024:
            raise AdbError("iOS 截图失败：返回数据为空")
        arr = np.frombuffer(data, dtype=np.uint8)
        bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if bgr is None:
            raise AdbError("iOS 截图 PNG 解码失败")
        if bgr.dtype == np.uint16:
            bgr = (bgr // 256).astype(np.uint8)  # 16-bit → 8-bit（模板匹配链路一致）
        self._last_size = (bgr.shape[1], bgr.shape[0])  # (w, h)
        return bgr, "dvt"

    def screen_size(self) -> tuple[int, int]:
        """获取屏幕分辨率。"""
        if self._last_size:
            return self._last_size

        async def _size():
            ld = await self._get_lockdown()
            w = int(ld.all_values.get('ScreenWidth', 0))
            h = int(ld.all_values.get('ScreenHeight', 0))
            scale = float(ld.all_values.get('ScreenScaleFactor', 1))
            # lockdown 给的是逻辑分辨率，乘以 scale 得物理像素
            if w > 0 and h > 0:
                return int(w * scale), int(h * scale)
            # 兜底：截图后从图像尺寸获取
            return None

        result = _ios_async(_size())
        if result:
            self._last_size = result
            return result
        # 最终兜底：截一张图取尺寸
        bgr, _ = self.screenshot_bgr()
        h, w = bgr.shape[:2]
        self._last_size = (w, h)
        return (w, h)

    # ── 输入（非越狱不可用）──
    def tap_pixel(self, x: int, y: int) -> None:
        raise AdbError("iOS 非越狱设备不支持模拟点击。请手动操作或用包名启动。")

    def tap_norm(self, cx: float, cy: float) -> None:
        raise AdbError("iOS 非越狱设备不支持模拟点击。请手动操作或用包名启动。")

    def swipe(self, x1: int, y1: int, x2: int, y2: int, dur_ms: int = 200) -> None:
        raise AdbError("iOS 非越狱设备不支持模拟滑动。")

    def keyevent(self, code: int) -> None:
        raise AdbError("iOS 不支持 keyevent（Android 按键码）。")

    # ── App 生命周期 ──
    def launch_package(self, bundle_id: str) -> None:
        """与 AdbHelper.launch_package 对齐的接口名（冷启动端点统一调用，真机验证发现）。"""
        self.launch_pkg(bundle_id)

    def launch_pkg(self, bundle_id: str) -> None:
        """通过 CoreDevice 框架启动 App（iOS 26 真机实测的正确路径）。

        严格性（对齐教训七）：启动是关键操作，失败必须抛错，不能静默吞。

        实测对比（iPhone 17,2 / iOS 26.6，2026-08-09）：
          - DVT ProcessControl.launch：进程启动但 window 未激活 → 画面黑屏 ✗
          - devicectl process launch：同样黑屏 ✗
          - CoreDevice launch_application（Xcode 同款框架）：画面正常渲染 ✓
        因此启动走 CoreDevice：AppServiceService.launch_application(bundle_id,
        kill_existing=True)。terminateExisting 先杀旧进程再启动——非越狱 iOS
        无法 force_stop，这是唯一保证「进程不存在时启动」（真冷启动语义）的途径，
        且画面渲染正常。
        """
        from pymobiledevice3.remote.core_device.app_service import AppServiceService

        async def _launch(rsd):
            async with AppServiceService(rsd) as apps:
                result = await apps.launch_application(bundle_id, kill_existing=True)
                pid = int(result.get("processToken", {}).get("processIdentifier") or 0)
                if pid <= 0:
                    raise AdbError(f"iOS 启动失败：设备返回无效 PID（{pid}）。bundle_id={bundle_id}")

        def _do():
            rsd = self._ensure_ios_rsd()
            IosLoop().run(_launch(rsd))

        try:
            _do()
        except AdbError:
            raise
        except Exception as e:
            # 隧道可能已失效：重建一次，仍失败则如实报错
            self._invalidate_ios_tunnel()
            try:
                _do()
            except Exception as e2:
                raise AdbError(
                    f"iOS 启动失败：{e2}。可能原因："
                    f"1) 未开启开发者模式（设置→隐私与安全→开发者模式）"
                    f"2) App 未安装或 bundle id 错误"
                    f"3) 设备未信任此电脑"
                ) from e2

    def force_stop(self, pkg: str) -> None:
        """iOS 非越狱无法程序化杀进程——记日志，用户需手动上滑关闭。"""
        print(f"[ios] force_stop 不支持（非越狱）· 请手动在 App 切换器中上滑关闭 {pkg}",
              file=sys.stderr, flush=True)

    def kill_all(self) -> str:
        return "(iOS 不支持 kill-all)"

    def list_packages(self) -> list[str]:
        """列出已安装 App 的 bundle id。

        10.x 的 get_apps() 返回 dict（bundle_id → info），迭代 dict 得到 key 字符串——
        旧写法 a.get('CFBundleIdentifier') 对 str 调用会 AttributeError（真机验证发现）。
        """
        async def _list():
            ld = await self._get_lockdown()
            from pymobiledevice3.services.installation_proxy import InstallationProxyService
            ip = InstallationProxyService(lockdown=ld)
            apps = await ip.get_apps()  # dict keyed by bundle id
            return sorted(bid for bid in apps)
        try:
            return _ios_async(_list())
        except Exception:
            return []

    def reinstall(self, pkg: str, ipa_path: str) -> list[str]:
        """卸载重装 IPA（通过 ideviceinstaller 或 pymobiledevice3）。"""
        if not Path(ipa_path).exists():
            raise AdbError(f"IPA 文件不存在：{ipa_path}")
        log: list[str] = []

        async def _do_reinstall():
            ld = await self._get_lockdown()
            from pymobiledevice3.services.installation_proxy import InstallationProxyService
            ip = InstallationProxyService(lockdown=ld)
            # 卸载
            try:
                await ip.uninstall(pkg)
                log.append(f"uninstall: {pkg}")
            except Exception as e:
                log.append(f"uninstall: skipped ({e})")
            # 安装（10.x API：install_from_local；旧的 ip.install 已移除，会 AttributeError）
            await ip.install_from_local(Path(ipa_path))
            log.append(f"install: {ipa_path}")

        try:
            _ios_async(_do_reinstall())
            return log
        except Exception as e:
            raise AdbError(f"iOS 安装失败：{e}") from e





class AdbError(RuntimeError):
    """adb 调用失败的统一异常（iOS 设备限制也抛这个），message 直接返回给前端。"""


# adb 调用失败统一捕获：Android 走 AdbHelper（AdbHelperError），
# iOS 仍抛 AdbError（非越狱限制），端点 catch 用这个联合
AdbOpError = (AdbError, AdbHelperError)


def _safe_serial(serial: str) -> str:
    """serial 转安全文件名片段（serial 可能含 ``:`` ``.`` 等路径危险字符）。"""
    return re.sub(r"[^A-Za-z0-9_-]", "_", serial or "")


def _device_template_files(serial: str) -> tuple[Path, Path]:
    """设备模板持久化文件（marker 图 + 元信息）。重启后端自动恢复用。"""
    safe = _safe_serial(serial)
    return DEVICE_TEMPLATES_DIR / f"marker_{safe}.png", DEVICE_TEMPLATES_DIR / f"meta_{safe}.json"


class DeviceSession:
    """单台设备的独立会话：设备句柄 + 独立锁 + 模板 + 观察状态 + 截图缓存。

    多设备并行（v4）：adb 命令只在本设备的锁下串行（adb server 对同一设备
    不擅长并发，AGENTS §2.4）；不同设备的命令经 adb server 多路复用可并行、
    互不阻塞——这是多台设备同时识别、单台延迟保持 ≤400ms 的关键。
    """

    def __init__(self, serial: str, platform: str = "android") -> None:
        self.serial = serial
        self.platform = platform
        self.lock = threading.RLock()
        # Android 统一走 AdbHelper（跨平台封装；server.py 不再维护平行 AdbDevice）
        self.device = (
            IosDevice(serial) if platform == "ios"
            else AdbHelper(serial, adb_path=ADB_EXE, project_root=ROOT)
        )
        # 模板/截图临时文件按 serial 隔离（多设备同时用互不覆盖）
        safe = _safe_serial(serial)
        self.marker_path = Path(tempfile.gettempdir()) / f"_cst_marker_{safe}.png"
        self.skip_dir = Path(tempfile.gettempdir()) / f"_cst_skips_{safe}"

        self._last_shot: Optional[Path] = None
        self._last_shot_at: float = 0.0
        # 截图诊断统计（让前端能看到后端工作是否正常，不再是黑盒）
        self.shot_total = 0       # 总截图次数（不含缓存命中）
        self.shot_cache_hits = 0  # 缓存命中次数
        self.shot_errors = 0      # 失败次数
        self.shot_last_ms = 0.0   # 上次截图耗时
        self.shot_avg_ms = 0.0    # 滑动平均耗时
        # 启动成功模板（cv2.matchTemplate 用，详见 set_marker_template / check_marker）
        self._marker_template: Optional[Path] = None  # 模板图路径（None=未设）
        self._marker_img = None                        # 内存缓存（BGR ndarray），避免每次 imread
        self._marker_matcher = None                    # TemplateMatcher 缓存（图片元素识别封装，见 adb_helper）
        # 模板匹配阈值（审核 E-P1-3：可调，每设备独立；重启还原默认值——与模板本身一致）
        self.marker_threshold: float = MARKER_MATCH_THRESHOLD
        # 设模板时的屏幕分辨率（审核 F-P1-4：分辨率变化检测用；None=未知，跳过检查）
        self._marker_res: Optional[tuple] = None
        self._fallback_from: Optional[str] = None       # 回退共用模板的来源 serial（诊断用）
        self._marker_w: int = 0                        # 模板像素宽
        self._marker_h: int = 0                        # 模板像素高
        self._marker_cx: float = 0.5                   # 模板中心归一化坐标（运行时搜索用）
        self._marker_cy: float = 0.5
        # 停表观察状态（每次 cold_start / marker_watch_reset 清零）
        self._marker_hit_streak: int = 0               # 连续过阈帧数
        self._marker_seen_below: bool = False          # 是否已见过低于阈值（上升沿）
        # 本轮启动已点过的跳过模板 id。点过后本轮不再命中，避免弹窗关掉后
        # 跳过区仍高置信 → 每 1.5s 又 return skipped，启动模板永远攒不满连续帧
        self._skip_fired_ids: set = set()
        self.marker_check_total = 0      # check_marker 累计调用次数（诊断用）
        self.marker_check_last_ms = 0.0  # 上次 check 耗时
        self.marker_check_last_conf = 0.0  # 上次置信度
        # 跳过弹窗模板（最多 SKIP_TEMPLATE_MAX 个）。每项：
        #   id/path/cx/cy/w/h/img/preview；last_tap_at 用于冷却防连点
        self._skip_templates: list[dict] = []
        # 从持久化目录恢复上次的启动模板（后端重启自动恢复，免反复重设）
        self._restore_marker()

    def reset_marker_watch(self, *, after_force_stop: bool = False) -> None:
        """新一次测速开始前清零连续确认 / 本轮已点跳过。

        after_force_stop=True（cold_start 刚杀过进程）：种子 ``_marker_seen_below=True``。
        含义见模块常量旁注释 / AGENTS——杀进程后不可能还停在「启动成功」页，
        若仍从 False 起算，二次冷启动首帧就已过阈时会永远等不到「先低于再升高」。
        """
        with self.lock:
            self._marker_hit_streak = 0
            # 上升沿：必须先见过低于阈值的帧，再过阈才停表（防桌面残留一上来就误停）。
            # force_stop 之后视为已经「离开成功态」，等价于见过 below。
            self._marker_seen_below = bool(after_force_stop)
            self._skip_fired_ids.clear()

    def set_marker_image(self, img) -> None:
        """写入启动模板内存缓存（拷贝，避免后续被改）。img 为 None 则清空。

        模板图变化时同时失效 matcher 缓存（下次匹配惰性重建）。
        """
        self._marker_matcher = None
        if img is None:
            self._marker_img = None
        else:
            self._marker_img = img.copy()

    def _load_fallback_marker(self):
        """回退共用模板：本设备无自己的模板时，从其它同分辨率设备持久化模板加载运行时副本。

        多台设备并行跑同一 App 时，各设备首页 UI 一致、分辨率不一致（Pixel 6a 1080x2400、
        moto 1080x2400 等情况）时，模板可以共用——不要求每台设备都设模板。

        规则：
          1. 遍历其它设备的持久化模板（marker_<serial>.png + meta_<serial>.json）
          2. 优先选与本设备屏幕分辨率（_last_size）一致的；未知时选分辨率最接近的
          3. 只做运行时加载（写入 _marker_img / _marker_template 生效），不覆盖持久化配置；
             设模板/清模板时回退自然失效（本设备有模板后优先用本设备的）

        返回 bool：是否成功加载到回退模板。
        """
        import json as _json
        # 本设备已有模板则不回退
        if self._marker_template is not None and Path(self._marker_template).exists():
            return False
        if not DEVICE_TEMPLATES_DIR.is_dir():
            return False
        target = getattr(self.device, "_last_size", None)
        target = tuple(target) if target else None
        best = None  # (score, marker_file, meta, serial)
        try:
            for meta_f in DEVICE_TEMPLATES_DIR.glob("meta_*.json"):
                safe = meta_f.name[len("meta_"):-len(".json")]
                if safe == _safe_serial(self.serial):
                    continue  # 跳过自己
                marker_f = DEVICE_TEMPLATES_DIR / f"marker_{safe}.png"
                if not marker_f.exists():
                    continue
                try:
                    meta = _json.loads(meta_f.read_text(encoding="utf-8"))
                except Exception:
                    continue
                # 评分：分辨率完全一致 = 最佳；否则按分辨率差值惩罚。
                # 本设备分辨率未知（尚无截图）时无法判断模板是否适用，跳过回退
                # （此前 target 为空给 0 分，会随机采纳第一份模板，实测踩坑）。
                if not target:
                    continue
                mres = tuple(meta.get("res") or ())
                if mres:
                    res_score = 0 if tuple(mres) == target else abs(tuple(mres)[0] - target[0]) + abs(tuple(mres)[1] - target[1])
                else:
                    res_score = 10 ** 6
                cand = (res_score, marker_f, meta, safe)
                if best is None or cand[0] < best[0]:
                    best = cand
        except Exception:
            return False
        if best is None:
            return False
        res_score, marker_f, meta, src_serial = best
        import cv2
        im = cv2.imread(str(marker_f))
        if im is None:
            return False
        # 运行时套用回退模板（不写持久化文件，避免污染本设备配置）
        self._marker_template = marker_f
        self._marker_img = im
        self._marker_matcher = None
        self._marker_w = int(meta.get("w") or 0)
        self._marker_h = int(meta.get("h") or 0)
        self._marker_cx = float(meta.get("cx", 0.5) if meta.get("cx") is not None else 0.5)
        self._marker_cy = float(meta.get("cy", 0.5) if meta.get("cy") is not None else 0.5)
        if meta.get("threshold"):
            self.marker_threshold = float(meta["threshold"])
        self._fallback_from = src_serial  # 诊断：回退来源
        print(f"[marker:{self.serial}] 回退共用模板 ← {src_serial}（{self._marker_w}×{self._marker_h}，res_score={res_score}）", flush=True)
        return True

    def ensure_marker_image(self):
        """返回缓存的模板图；缓存空则从磁盘读并填入。

        本设备无模板时自动回退到其它设备同分辨率模板（设备首次未设模板也能跑）。
        返回 DataFrame/ndarray。
        """
        import cv2
        if self._marker_img is not None:
            return self._marker_img
        if self._marker_template is None or not Path(self._marker_template).is_file():
            # 本设备没设模板 → 尝试回退其它设备的共用模板
            if not self._load_fallback_marker():
                return None
        im = cv2.imread(str(self._marker_template))
        if im is not None:
            self._marker_img = im
        return self._marker_img

    def ensure_marker_matcher(self):
        """返回 TemplateMatcher（图片元素识别封装，见 adb_helper）。

        惰性构造：模板图/坐标变更时由 set_marker_image / 模板设置处清缓存重建。
        matchTemplate + 纯色拒绝 + 帧太小兜底都在 TemplateMatcher 内部，
        端点不再裸用 cv2（同一套逻辑被 tests/test_adb_helper.py 覆盖）。
        """
        if self._marker_matcher is not None:
            return self._marker_matcher
        img = self.ensure_marker_image()
        if img is None:
            return None
        self._marker_matcher = TemplateMatcher(
            img,
            self._marker_cx,
            self._marker_cy,
            padding=MARKER_SEARCH_PADDING,
            threshold=self.marker_threshold,
        )
        return self._marker_matcher

    def _restore_marker(self) -> None:
        """从持久化目录恢复上次的模板（后端重启自动恢复，免用户反复重设）。

        真机实测痛点修复：模板是内存态，每次后端重启就丢。设模板时同步落盘
        （set_marker_template 里调用 _save_marker_persistent），创建会话时恢复。
        恢复失败（文件缺失/损坏）静默视为未设，不阻塞。
        """
        try:
            import json as _json
            marker_file, meta_file = _device_template_files(self.serial)
            if not marker_file.is_file() or not meta_file.is_file():
                return
            meta = _json.loads(meta_file.read_text(encoding="utf-8"))
            self._marker_template = marker_file
            self._marker_w = int(meta.get("w") or 0)
            self._marker_h = int(meta.get("h") or 0)
            self._marker_cx = float(meta.get("cx") if meta.get("cx") is not None else 0.5)
            self._marker_cy = float(meta.get("cy") if meta.get("cy") is not None else 0.5)
            self.marker_threshold = float(meta.get("threshold") or MARKER_MATCH_THRESHOLD)
            if meta.get("res"):
                self._marker_res = tuple(meta["res"])
            self._marker_img = None  # ensure_marker_image 惰性读盘
            self._marker_matcher = None
            print(f"[marker:{self.serial}] 已从磁盘恢复模板（{self._marker_w}×{self._marker_h}）", flush=True)
        except Exception as e:
            print(f"[marker:{self.serial}] 模板恢复失败（忽略，视为未设）：{e}", file=sys.stderr, flush=True)

    def screenshot_bytes(self, *, use_cache: bool = True) -> tuple[bytes, dict]:
        """截图并返回 (PNG bytes, 诊断元信息)。

        缓存策略：100ms 内的截图复用（防止 OCR + 直播轮询同时打 adb，
        adb server 单线程，并发请求会互相阻塞）。`use_cache=False` 强制新截图，
        供「手动截图」按钮使用。

        元信息让前端能看见后端到底发生了什么：
          - ms：本次耗时
          - bytes：返回字节数
          - cache：是否命中缓存
          - shot_at：截图时刻（perf_counter 秒）
        """
        with self.lock:
            now = time.perf_counter()
            cache_age = now - self._last_shot_at
            if (
                use_cache
                and self._last_shot is not None
                and self._last_shot.exists()
                and cache_age < 0.1  # 100ms（原 300ms 太长，让画面"滞后"）
            ):
                self.shot_cache_hits += 1
                data = self._last_shot.read_bytes()
                return data, {
                    "ms": 0, "bytes": len(data), "cache": True,
                    "shot_at": self._last_shot_at, "cache_age_ms": round(cache_age * 1000, 1),
                }

            target = Path(tempfile.gettempdir()) / (
                f"_cst_live_{os.getpid()}_{_safe_serial(self.serial)}.png"
            )
            t0 = time.perf_counter()
            try:
                self.device.screenshot(target)
            except Exception as e:
                self.shot_errors += 1
                raise
            elapsed = (time.perf_counter() - t0) * 1000
            data = target.read_bytes()
            self._last_shot = target
            self._last_shot_at = time.perf_counter()

            # 滑动平均耗时（窗口 20）
            self.shot_total += 1
            self.shot_last_ms = elapsed
            w = min(self.shot_total, 20)
            self.shot_avg_ms = self.shot_avg_ms * (1 - 1 / w) + elapsed / w

            # 后端日志（让用户在黑窗口能看到工作状态）
            print(
                f"[shot:{self.serial}] #{self.shot_total} {len(data)//1024}KB {elapsed:.0f}ms"
                f" (avg {self.shot_avg_ms:.0f}ms, cache_hits={self.shot_cache_hits})",
                flush=True,
            )

            return data, {
                "ms": round(elapsed, 1),
                "bytes": len(data),
                "cache": False,
                "shot_at": self._last_shot_at,
            }

    def ocr(self, engine: OcrEngine) -> dict:
        with self.lock:
            shot = Path(tempfile.gettempdir()) / (
                f"_cst_ocr_{os.getpid()}_{_safe_serial(self.serial)}.png"
            )
            self.device.screenshot(shot)
            try:
                items = engine.recognize(shot)
            finally:
                try:
                    shot.unlink()
                except OSError:
                    pass
            # 同步把屏幕尺寸更新到 _last_size，后续 tap_norm 用
            try:
                w, h = self.device.screen_size()
            except AdbOpError:
                # wm size 失败时用图像尺寸兜底
                import cv2
                img = cv2.imread(str(self._last_shot)) if self._last_shot else None
                if img is not None:
                    h, w = img.shape[:2]
                else:
                    w, h = 1080, 1920
            confidences = [i.confidence for i in items]
            return {
                "width": w,
                "height": h,
                "items": [
                    {
                        "text": it.text,
                        "cx": round(it.cx, 5),
                        "cy": round(it.cy, 5),
                        "w": round(it.w, 5),
                        "h": round(it.h, 5),
                        "confidence": round(it.confidence, 4),
                    }
                    for it in items
                ],
                "stats": {
                    "count": len(confidences),
                    "mean": round(sum(confidences) / len(confidences), 4) if confidences else 0.0,
                    "ge_0_8": sum(1 for c in confidences if c >= 0.8),
                    "ge_0_6_lt_0_8": sum(1 for c in confidences if 0.6 <= c < 0.8),
                    "lt_0_6": sum(1 for c in confidences if c < 0.6),
                },
            }


class Session:
    """全局会话：设备注册表 + 当前选中指针（多设备状态各自在 DeviceSession 里）。

    兼容层：老端点代码里 ``SESSION._lock`` / ``SESSION.device`` / ``SESSION.shot_total``
    等访问会被委托到「当前选中的 DeviceSession」，单设备流程语义与 v3 完全一致；
    带 serial 的请求则直接路由到对应 DeviceSession（不抢全局选中，并行安全）。
    """

    def __init__(self) -> None:
        # 仅保护注册表 dict（不持锁做 adb I/O）。用 RLock：select() 持锁内还会
        # 调 session_for()（同样取这把锁），普通 Lock 会死锁（实测踩过）。
        self._registry_lock = threading.RLock()
        self._devices: dict[str, DeviceSession] = {}
        self._serial: Optional[str] = None
        self._platform = "android"
        self._ocr = OcrEngine()
        self._empty_lock = threading.RLock()     # 未选设备时的空锁（兼容老语义）

    # ── 注册表 ──
    def session_for(self, serial: str, platform: Optional[str] = None) -> DeviceSession:
        """按 serial 取设备会话，不存在则创建（惰性）。platform 只在创建时生效。"""
        with self._registry_lock:
            ds = self._devices.get(serial)
            if ds is None:
                ds = DeviceSession(serial, platform or "android")
                self._devices[serial] = ds
            return ds

    def select(self, serial: Optional[str], platform: str = "android") -> dict:
        """切换「当前选中」设备（UI 语义：直播/手动操作作用于选中设备）。"""
        with self._registry_lock:
            self._serial = serial
            self._platform = platform
            if serial:
                self.session_for(serial, platform)
        return {"serial": serial, "ready": serial is not None, "platform": platform}

    def current(self) -> dict:
        return {
            "serial": self._serial,
            "ready": self.current_session is not None,
            "platform": self._platform,
        }

    # ── 当前设备委托（老端点代码兼容）──
    @property
    def current_session(self) -> Optional[DeviceSession]:
        return self._devices.get(self._serial) if self._serial else None

    @property
    def _lock(self):
        """当前选中设备的锁；未选设备时返回空锁（老代码 with SESSION._lock: 语义不变）。"""
        ds = self.current_session
        return ds.lock if ds else self._empty_lock

    @property
    def device(self):
        ds = self.current_session
        if ds is None:
            raise AdbError("未选择设备，请先在左上角连接设备")
        return ds.device

    @contextmanager
    def device_op(self):
        """设备操作统一入口：在当前选中设备的锁下执行 ADB I/O（adb server 不擅长并发）。

        所有会触发 adb 命令的端点都应用 ``with SESSION.device_op() as dev:`` 包住，
        替代裸 ``SESSION.device`` 访问，避免直播截图/OCR/自动测速/卸装操作并发竞争。
        """
        with self._lock:
            yield self.device

    def __getattr__(self, name):
        """老端点代码里 SESSION.shot_total / SESSION._marker_* 等字段 → 委托当前设备会话。"""
        ds = self.current_session
        if ds is None:
            raise AttributeError(f"SESSION.{name}：未选择设备")
        return getattr(ds, name)

SESSION = Session()


# ── FastAPI ────────────────────────────────────────────────────────────

from fastapi import FastAPI, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field


def _cleanup_stale_temp_files() -> None:
    """启动时清理 tempdir 下的旧 _cst_* 文件（避免长时间运行后堆积）。

    72 小时连续运行 + 多次重启会累积一堆 _cst_live_{oldpid}.png 和 _cst_upload.apk
    （232MB）。这里在每次启动时清理掉所有 _cst_* 文件——本进程的会在使用中，
    但启动瞬间还没创建，所以清的是历史残留。

    v2 新增：清空 _cst_uploads/ 整个目录（用户上传的 APK 现在按原始名保留，
    不再覆盖式，会累积。启动时整目录清空重建空目录，避免堆积 + 跨设备冲突）。

    v3 新增：清理 _cst_marker.png（启动成功模板）。Session._marker_template 是
    内存变量，重启后必然丢失，文件留着也用不上（check_marker 会返回"未设模板"），
    所以清掉保持一致。

    v4 新增：模板按设备隔离（_cst_marker_<serial>.png / _cst_skips_<serial>/），
    一并清理；老的单文件 glob 保留兼容历史残留。
    """
    import glob
    import shutil
    tempdir = tempfile.gettempdir()
    # 老的单数文件（兼容历史）
    for pattern in ("_cst_live_*.png", "_cst_ocr_*.png", "_cst_upload.apk",
                    "_cst_marker.png", "_cst_marker_src_*.png", "_cst_marker_chk_*.png",
                    "_cst_skip_*.png",
                    # v4：按设备隔离的模板/截图
                    "_cst_marker_*.png", "_cst_marker_src_*.png",
                    "_cst_marker_chk_*.png", "_cst_skip_src_*.png"):
        for path in glob.glob(str(Path(tempdir) / pattern)):
            try:
                Path(path).unlink()
            except OSError:
                pass  # 文件可能正被占用
    # 跳过模板目录（通知权限等）：老单目录 + v4 按设备隔离的 _cst_skips_* 目录
    for skip_dir in glob.glob(str(Path(tempdir) / "_cst_skips*")):
        shutil.rmtree(skip_dir, ignore_errors=True)
    # 新的 APK 上传目录：只确保目录存在，**不在启动时清空**。
    # 清空会导致前端 localStorage 里的 apkPath 失效，自动循环卡在「卸装重装」
    # 或秒失败「APK 不存在」——测速会话跨重启必须还能用同一份 APK。
    APK_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    print("[startup] 清理了 tempdir 下的旧 _cst_* 截图/模板；保留 _cst_uploads/ 里的 APK", flush=True)


def _kill_adb_server() -> None:
    """退出前关闭 adb daemon。

    adb daemon 是常驻进程，后端退出后 adb.exe 继续运行，导致：
      - 升级安装时 adb.exe 文件被锁 → NSIS 安装器报「应用仍在运行」
      - 多次重启后残留多个 adb server 实例
    Electron before-quit 也会兜底调一次（后端被 taskkill /F 时 lifespan 不执行）。
    """
    try:
        subprocess.run(
            [ADB_EXE, "kill-server"],
            timeout=5, capture_output=True,
        )
    except Exception:
        pass  # 退出路径不能抛异常


# FastAPI lifespan（替代已弃用的 on_event，官方推荐写法）
@asynccontextmanager
async def lifespan(app: FastAPI):
    _cleanup_stale_temp_files()
    yield
    _kill_adb_server()


app = FastAPI(title="App Cold Start Profiler", version="2.0", lifespan=lifespan)


class DeviceSelectReq(BaseModel):
    serial: Optional[str] = None
    platform: str = "android"  # "android" | "ios"


class SetMarkerReq(BaseModel):
    """设定启动成功模板：以当前屏 (cx, cy) 为中心截小区域存为模板。

    cx/cy 是归一化坐标（0~1），来自前端点画面或点 OCR 框。
    box_w/box_h 可选：若来自 OCR 框就用框的归一化尺寸换算像素；否则用默认。
    serial 可选：指定设备（默认当前选中），多设备各设各的模板。
    """
    cx: float = Field(ge=0.0, le=1.0)
    cy: float = Field(ge=0.0, le=1.0)
    box_w: Optional[float] = Field(default=None, ge=0.0, le=1.0)  # 归一化宽（0~1）
    box_h: Optional[float] = Field(default=None, ge=0.0, le=1.0)  # 归一化高（0~1）
    serial: Optional[str] = None


class ClearSkipReq(BaseModel):
    """清除跳过模板。id 为 None 时清空全部；指定 id 只删一条。"""
    id: Optional[int] = None
    serial: Optional[str] = None


class ProjectCreateReq(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class ProjectRenameReq(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class ProjectSaveReq(BaseModel):
    """把当前 Session 模板 + 表单配置写入指定项目（手动保存）。不存 APK。"""
    name: Optional[str] = None
    package: str = ""
    launch_mode: Literal["tap", "pkg"] = "pkg"
    tap_x: Optional[float] = None
    tap_y: Optional[float] = None
    platform: str = "gp"
    apk_hint: str = ""  # 仅提示用文件名，不复制 APK


def _safe_project_id(raw: str) -> str:
    """项目 id：只允许 [A-Za-z0-9_-]，防路径穿越。"""
    s = re.sub(r"[^A-Za-z0-9_-]", "", (raw or "").strip())
    if not s or s in (".", ".."):
        raise AdbError("非法项目 id")
    return s


def _project_dir(pid: str) -> Path:
    d = (PROJECTS_DIR / _safe_project_id(pid)).resolve()
    try:
        d.relative_to(PROJECTS_DIR.resolve())
    except ValueError as e:
        raise AdbError("项目路径越界") from e
    return d


def _read_project_meta(pid: str) -> dict:
    meta_path = _project_dir(pid) / "meta.json"
    if not meta_path.is_file():
        raise AdbError(f"项目不存在：{pid}")
    import json
    return json.loads(meta_path.read_text(encoding="utf-8"))


def _write_project_meta(pid: str, meta: dict) -> None:
    import json
    d = _project_dir(pid)
    d.mkdir(parents=True, exist_ok=True)
    meta["id"] = pid
    meta["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    (_project_dir(pid) / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _preview_b64_from_path(path: Path) -> tuple[str, str]:
    import cv2
    img = cv2.imread(str(path))
    if img is None:
        return "", "image/jpeg"
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ok:
        return "", "image/jpeg"
    return base64.b64encode(buf).decode("ascii"), "image/jpeg"


def _apply_project_to_session(pid: str) -> dict:
    """从磁盘项目加载模板到当前选中设备的会话（运行时内存），返回给前端的摘要。

    多设备：每次「加载项目」只作用于当前选中设备（每台设备模板独立，见
    DeviceSession.marker_path/skip_dir）；多设备并行时每台设备各自加载一次即可。
    项目磁盘格式暂不变（marker.png/skip_<id>.png 存当前设备一份）。
    """
    import shutil

    meta = _read_project_meta(pid)
    pdir = _project_dir(pid)
    ds = SESSION.current_session
    if ds is None:
        raise AdbError("未选择设备，请先在左上角连接设备")

    with ds.lock:
        # 清当前设备跳过模板
        for t in ds._skip_templates:
            try:
                Path(t["path"]).unlink(missing_ok=True)
            except OSError:
                pass
        ds._skip_templates.clear()
        ds.skip_dir.mkdir(parents=True, exist_ok=True)

        marker_src = pdir / "marker.png"
        if marker_src.is_file():
            shutil.copy2(marker_src, ds.marker_path)
            ds._marker_template = ds.marker_path
            ds._marker_res = None  # 项目 meta 无分辨率字段 → 未知，跳过分辨率检查（不误报）
            m = meta.get("marker") or {}
            ds._marker_w = int(m.get("w") or 0)
            ds._marker_h = int(m.get("h") or 0)
            ds._marker_cx = float(m.get("cx") if m.get("cx") is not None else 0.5)
            ds._marker_cy = float(m.get("cy") if m.get("cy") is not None else 0.5)
            import cv2
            im = cv2.imread(str(ds.marker_path))
            if im is not None:
                ds.set_marker_image(im)
                if ds._marker_w <= 0 or ds._marker_h <= 0:
                    ds._marker_h, ds._marker_w = im.shape[:2]
            else:
                ds.set_marker_image(None)
            ds.reset_marker_watch()
            marker_preview, marker_mime = _preview_b64_from_path(ds.marker_path)
        else:
            ds._marker_template = None
            ds.set_marker_image(None)
            ds._marker_w = ds._marker_h = 0
            ds.reset_marker_watch()
            marker_preview, marker_mime = "", "image/jpeg"

        skip_out = []
        for s in meta.get("skips") or []:
            sid = int(s["id"])
            src = pdir / f"skip_{sid}.png"
            if not src.is_file():
                continue
            dst = ds.skip_dir / f"skip_{sid}.png"
            shutil.copy2(src, dst)
            prev, mime = _preview_b64_from_path(dst)
            import cv2
            skip_img = cv2.imread(str(dst))
            entry = {
                "id": sid,
                "path": dst,
                "cx": float(s["cx"]),
                "cy": float(s["cy"]),
                "w": int(s.get("w") or 0),
                "h": int(s.get("h") or 0),
                "img": skip_img,  # 内存缓存，check_auto 免重复 imread
                "matcher": None,  # TemplateMatcher 惰性构造（图片元素识别封装）
                "last_tap_at": 0.0,
                "preview_base64": prev,
                "preview_mime": mime,
            }
            ds._skip_templates.append(entry)
            skip_out.append({
                "id": sid,
                "width": entry["w"],
                "height": entry["h"],
                "center_x": round(entry["cx"], 5),
                "center_y": round(entry["cy"], 5),
                "preview_base64": prev,
                "preview_mime": mime,
            })

    return {
        "ok": True,
        "id": pid,
        "name": meta.get("name") or pid,
        "package": meta.get("package") or "",
        "launch_mode": meta.get("launch_mode") or "pkg",
        "tap_x": meta.get("tap_x"),
        "tap_y": meta.get("tap_y"),
        "platform": meta.get("platform") or "gp",
        "apk_hint": meta.get("apk_hint") or "",
        "marker_ready": ds._marker_template is not None,
        "marker_width": ds._marker_w,
        "marker_height": ds._marker_h,
        "marker_center_x": round(ds._marker_cx, 5) if ds._marker_template else None,
        "marker_center_y": round(ds._marker_cy, 5) if ds._marker_template else None,
        "marker_preview_base64": marker_preview,
        "marker_preview_mime": marker_mime,
        "skips": skip_out,
        "updated_at": meta.get("updated_at") or "",
    }


class TapReq(BaseModel):
    x: float
    y: float
    norm: bool = True
    serial: Optional[str] = None


class KeyReq(BaseModel):
    code: int
    serial: Optional[str] = None


class SwipeReq(BaseModel):
    x1: float
    y1: float
    x2: float
    y2: float
    norm: bool = True
    dur_ms: int = 200
    serial: Optional[str] = None


class LaunchPkgReq(BaseModel):
    package: str
    serial: Optional[str] = None


class ForceStopReq(BaseModel):
    package: str
    serial: Optional[str] = None


class KillAllReq(BaseModel):
    serial: Optional[str] = None


class ReinstallReq(BaseModel):
    package: str
    apk_path: str
    serial: Optional[str] = None


class ApkParseReq(BaseModel):
    apk_path: str


class MarkerThresholdReq(BaseModel):
    """模板匹配阈值设置（审核 E-P1-3）。校验 0.5~0.99，每设备独立，重启还原默认。"""
    threshold: float = Field(ge=0.5, le=0.99)
    serial: Optional[str] = None


class VerifyLaunchReq(BaseModel):
    """启动测试：启动 App 并校验前台包名与期望一致（防包名填错/装错包）。"""
    package: str = Field(min_length=1, max_length=200)
    serial: Optional[str] = None


class SysBaselineReq(BaseModel):
    """系统对照模式（am start -W 交叉验证）。package 必填，rounds/cooldown_s 可选。"""
    package: str = Field(min_length=1, max_length=200)
    rounds: int = Field(default=5, ge=1, le=10)
    cooldown_s: float = Field(default=3.0, ge=0.0, le=10.0)
    serial: Optional[str] = None


class ColdStartReq(BaseModel):
    """冷启动请求：服务端做 force_stop → tap/launch，响应返回后前端开始计时。

    `mode` = "tap"：按归一化坐标点图标（含 Launcher 响应，真实冷启动）
    `mode` = "pkg"：用 monkey 包名启动（绕过触摸，三星等禁用 tap 的设备用）

    计时语义（v1 逻辑，已验证 correct，详见 AGENTS.md §2.1）：
      - 前端 Space 启动 → 网络往返到服务端 → force_stop 准备 → tap/launch 命令发出
        → 网络返回前端 → 前端 startTs = performance.now() 开始计时
      - 计时起点 = 响应回来后（漏掉了 tap 执行时间 ~200ms，每次都漏，横向对比有效）
      - 计时终点 = 用户按 Space 停止（人工按键，含反应时间 ~250-400ms 系统性正偏）
      - 服务端返回的 start_wall 仅供诊断/将来用，前端计时**不消费它**

    注意：cold_start 不会自动回主页。用户需确保启动前已在桌面（或 App 已被 force_stop
    后系统自动回桌面）。独立的"回主页"能力保留在前端按钮 + /api/key 端点。
    """
    mode: Literal["tap", "pkg"] = "tap"
    x: Optional[float] = Field(default=None, ge=0.0, le=1.0)  # 归一化坐标（mode=tap 时）
    y: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    package: Optional[str] = None
    serial: Optional[str] = None


def _err(status: int, msg: str) -> HTTPException:
    return HTTPException(status_code=status, detail=msg)


def _target_session(serial: Optional[str]) -> DeviceSession:
    """带 serial 的请求路由到对应设备会话；未传 serial 用当前选中。

    多设备并行：不调用 SESSION.select()（那会抢全局选中，并行时互相干扰），
    直接按 serial 从注册表取/建会话。已有会话优先（保留其平台类型，如 iOS）。
    """
    if serial:
        return SESSION.session_for(serial)
    ds = SESSION.current_session
    if ds is None:
        raise AdbError("未选择设备，请先在左上角连接设备")
    return ds


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "adb": ADB_EXE, "version": "2.0"}


@app.get("/api/device_info")
def device_info(serial: Optional[str] = None) -> dict:
    """读取一台设备的硬件规格（供 Word 报告「测试设备」表格填充）。

    Android：adb getprop + wm size + /proc/meminfo；iOS：pymobiledevice3 取值。
    单条 prop 取失败不影响整体（返回对应字段为空串），整设备不可达才抛 AdbError→400。
    与其他 adb 端点同样持目标设备锁（对齐 §2.4）。
    """
    try:
        ds = _target_session(serial)
        with ds.lock:
            if isinstance(ds.device, IosDevice):
                # iOS：pymobiledevice3 能取到的有限，尽力而为
                return {"ok": True, "platform": "ios",
                        "brand": "Apple", "model": ds.device.model or "",
                        "osVersion": "", "cpu": "", "ram": "",
                        "resolution": "", "hardware": ""}
            # Android：一条 shell 批量取多个 getprop（减少 adb 往返）
            props_script = (
                "for p in ro.product.manufacturer ro.product.brand ro.product.model "
                "ro.build.version.release ro.product.cpu.abi ro.board.platform ro.hardware; "
                "do echo \"$p=${p}\"; getprop \"$p\"; done"
            )
            raw = ds.device.run(["shell", props_script], timeout=10.0, check=False)
            props: dict[str, str] = {}
            lines = [l.strip() for l in (raw or "").splitlines() if l.strip()]
            i = 0
            while i + 1 < len(lines):
                key = lines[i]
                val = lines[i + 1]
                if key.startswith("ro.") and "=" in key:
                    props[key.split("=")[0]] = val
                i += 2
            # 内存：MemTotal（kB）→ GB
            ram = ""
            try:
                mem_raw = ds.device.run(["shell", "cat", "/proc/meminfo"], timeout=10.0, check=False)
                m = re.search(r"MemTotal:\s*(\d+)", mem_raw or "")
                if m:
                    ram = f"{round(int(m.group(1)) / 1024 / 1024)}GB"
            except Exception:
                pass
            # 分辨率：wm size 输出形如 "Physical size: 1080x2400"
            resolution = ""
            try:
                size_raw = ds.device.run(["shell", "wm", "size"], timeout=10.0, check=False)
                m = re.search(r"(\d+x\d+)", size_raw or "")
                if m:
                    resolution = m.group(1)
            except Exception:
                pass
            # 厂商优先 manufacturer，空则回退 brand
            brand = props.get("ro.product.manufacturer") or props.get("ro.product.brand") or ""
            return {
                "ok": True,
                "platform": "android",
                "brand": brand,
                "model": props.get("ro.product.model", ""),
                "osVersion": ("Android " + props["ro.build.version.release"]) if props.get("ro.build.version.release") else "",
                "cpu": props.get("ro.product.cpu.abi") or props.get("ro.board.platform") or "",
                "ram": ram,
                "resolution": resolution,
                "hardware": props.get("ro.hardware") or props.get("ro.board.platform") or "",
            }
    except AdbOpError as e:
        raise _err(400, str(e))


@app.get("/api/devices")
def list_devices() -> dict:
    """列出所有连接的设备（Android + iOS 合并返回）。

    Android 设备有 platform="android"，iOS 设备有 platform="ios"。
    前端选择设备时带上 platform，后端据此创建 AdbHelper 或 IosDevice。
    """
    try:
        devices = [
            {
                "serial": d.serial,
                "state": d.state,
                "model": d.model,
                "platform": "android",
            }
            for d in AdbHelper.devices(adb_path=ADB_EXE, project_root=ROOT)
        ]
    except AdbHelperError as e:
        devices = []
        adb_error = str(e)
    else:
        adb_error = None
    # 合并 iOS 设备
    try:
        ios_devs = IosDevice.devices()
        devices.extend(ios_devs)
    except Exception:
        pass  # pymobiledevice3 不可用或无 iOS 设备，静默跳过
    result = {"devices": devices}
    if adb_error:
        result["error"] = adb_error
    return result


@app.post("/api/device/select")
def select_device(req: DeviceSelectReq) -> dict:
    return SESSION.select(req.serial, req.platform)


@app.get("/api/apps")
def list_apps(serial: Optional[str] = None) -> dict:
    """列出指定设备（默认当前选中）上的第三方包名，供前端下拉选择。"""
    try:
        ds = _target_session(serial)
        with ds.lock:
            pkgs = ds.device.list_packages()
        return {"apps": pkgs}
    except AdbOpError as e:
        return {"apps": [], "error": str(e)}


@app.post("/api/upload_apk")
async def upload_apk(file: UploadFile) -> dict:
    """接收前端拖拽/选择上传的 APK，**保留原始文件名**存到 _cst_uploads/。

    设计（v2 改进）：
      - 浏览器 http:// 下拿不到本地完整路径（安全限制），仍走"上传到后端"方案
      - 但不再覆盖式存到固定 _cst_upload.apk —— 那样前端显示的路径毫无可信度，
        看不出是哪个 APK。现在按原始文件名（经安全过滤）存到 _cst_uploads/
      - 前端 apkPath 输入框显示后端真实路径（含原始名），一眼能识别是哪个 APK
      - 同名冲突不覆盖，追加短 hash 后缀，保留历史（启动时会清空）

    返回字段（**只新增不删除**，契约向前兼容）：
      - ok / path / size_mb：老字段，保留
      - original_name：用户原始文件名（未过滤，含中文/空格）
      - saved_name：实际存盘文件名（已过滤）
      - size_bytes：精确字节数
    """
    if not file.filename or not file.filename.lower().endswith(".apk"):
        raise _err(400, "请上传 .apk 文件")
    APK_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    saved_name = _safe_apk_filename(file.filename)
    target = APK_UPLOAD_DIR / saved_name
    with target.open("wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)
    size_bytes = target.stat().st_size
    return {
        "ok": True,
        "path": str(target),
        "size_mb": round(size_bytes / 1048576, 1),
        # 新增字段（可信度增强）
        "original_name": file.filename,
        "saved_name": saved_name,
        "size_bytes": size_bytes,
    }


@app.get("/api/device/current")
def current_device() -> dict:
    return SESSION.current()


@app.post("/api/parse_apk")
def parse_apk(req: ApkParseReq) -> dict:
    """解析 APK 的 package/version 元数据，不执行安装。"""
    if AdbHelper is None:
        raise _err(500, "ADB helper 未加载")
    try:
        info = AdbHelper(
            SESSION._serial,
            adb_path=ADB_EXE,
            project_root=ROOT,
        ).parse_apk(req.apk_path)
        return {
            "ok": True,
            "path": info.path,
            "package": info.package,
            "version_name": info.version_name,
            "version_code": info.version_code,
            "label": info.label,
        }
    except (AdbHelperError, OSError) as exc:
        raise _err(400, str(exc)) from exc


@app.get("/api/screenshot")
def screenshot(manual: int = 0, serial: Optional[str] = None) -> Response:
    """截屏 PNG。

    `?manual=1` 跳过缓存（用于「手动截图」按钮，确保拿到最新画面）。
    响应头带诊断字段，前端据此显示耗时/缓存命中/失败原因：
      - X-Shot-Ms: 本次截图耗时（毫秒）
      - X-Shot-Cache: 1=命中缓存 0=新截图
      - X-Shot-Bytes: 字节数
      - X-Shot-Total: 该设备累计截图次数
    """
    ds = None
    try:
        ds = _target_session(serial)
        data, meta = ds.screenshot_bytes(use_cache=manual == 0)
    except AdbOpError as e:
        if ds is not None:
            ds.shot_errors += 1
        raise _err(400, str(e))
    headers = {
        "X-Shot-Ms": str(meta["ms"]),
        "X-Shot-Cache": "1" if meta["cache"] else "0",
        "X-Shot-Bytes": str(meta["bytes"]),
        "X-Shot-Total": str(ds.shot_total),
        "X-Shot-Avg-Ms": str(round(ds.shot_avg_ms, 1)),
        # 用 no-store 防止浏览器/中间代理缓存这个响应（很重要！否则永远同一张图）
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    return Response(content=data, media_type="image/png", headers=headers)


@app.get("/api/shot_stats")
def shot_stats(serial: Optional[str] = None) -> dict:
    """截图累计统计（指定设备或当前选中），让前端诊断"轮询到底有没有在工作"。"""
    ds = _target_session(serial) if serial else SESSION.current_session
    if ds is None:
        return {
            "total": 0, "cache_hits": 0, "errors": 0,
            "last_ms": 0.0, "avg_ms": 0.0,
            "device": SESSION._serial, "ready": False,
        }
    return {
        "total": ds.shot_total,
        "cache_hits": ds.shot_cache_hits,
        "errors": ds.shot_errors,
        "last_ms": round(ds.shot_last_ms, 1),
        "avg_ms": round(ds.shot_avg_ms, 1),
        "device": ds.serial,
        "ready": True,
    }


@app.get("/api/ocr")
def ocr(serial: Optional[str] = None) -> dict:
    try:
        ds = _target_session(serial)
        return ds.ocr(SESSION._ocr)
    except AdbOpError as e:
        raise _err(400, str(e))


@app.post("/api/set_marker_template")
def set_marker_template(req: SetMarkerReq) -> dict:
    """以当前屏幕 (cx, cy) 为中心截小区域，存为启动成功模板。

    用途：用户在画面上点"启动成功"元素位置（或点 OCR 框），后端以该坐标为中心
    截一个小区域存为模板（_cst_marker.png）。之后 /api/check_marker 用这个模板
    做 cv2.matchTemplate 区域比对，毫秒级判定启动是否成功。

    比 OCR 文字匹配快 ~20-40 倍：OCR 全图推理 800-2000ms，模板比对 20-50ms。
    冷启动计时停表精度从 ±1-2s 提升到 ±50ms。

    返回 preview_base64 让前端显示缩略图，用户能确认截对了哪块。
    """
    if not (0.0 <= req.cx <= 1.0 and 0.0 <= req.cy <= 1.0):
        raise _err(400, f"cx/cy 必须在 0~1 之间，收到 cx={req.cx} cy={req.cy}")
    try:
        import cv2

        ds = _target_session(req.serial)
        with ds.lock:
            # 1) 截当前屏（不复用缓存，确保是用户当前看到的画面）
            shot = Path(tempfile.gettempdir()) / f"_cst_marker_src_{os.getpid()}_{_safe_serial(ds.serial)}.png"
            try:
                ds.device.screenshot(shot)
                img = cv2.imread(str(shot))
            finally:
                try:
                    shot.unlink()
                except OSError:
                    pass
            if img is None:
                raise AdbError("截图失败或 cv2 读图失败")

            h_px, w_px = img.shape[:2]
            cx_px = int(req.cx * w_px)
            cy_px = int(req.cy * h_px)

            # 2) 算模板尺寸：优先用传入的 box_w/box_h 换算，否则用默认
            if req.box_w and req.box_h and 0 < req.box_w <= 1 and 0 < req.box_h <= 1:
                tw = max(40, int(req.box_w * w_px))
                th = max(40, int(req.box_h * h_px))
            else:
                tw = MARKER_DEFAULT_W
                th = MARKER_DEFAULT_H

            # 3) 算截取区域（中心对齐 cx/cy，裁剪到画面范围内）
            x1 = cx_px - tw // 2
            y1 = cy_px - th // 2
            x2 = x1 + tw
            y2 = y1 + th
            # 越界则整体平移（保持模板尺寸不变，避免 matchTemplate 尺寸不匹配）
            if x1 < 0:
                x1, x2 = 0, tw
            elif x2 > w_px:
                x2, x1 = w_px, w_px - tw
            if y1 < 0:
                y1, y2 = 0, th
            elif y2 > h_px:
                y2, y1 = h_px, h_px - th

            template = img[y1:y2, x1:x2]
            if template.size == 0:
                raise AdbError(f"模板截取为空：img {w_px}x{h_px}, 区域 ({x1},{y1})-({x2},{y2})")

            # 3.5) 拒绝低方差/纯色模板：TM_CCOEFF_NORMED 对纯色模板会返回 1.0 满置信度
            # （实测确认），导致启动瞬间立即误命中停表。要求模板有足够纹理（标准差 ≥ 15）。
            gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
            std = float(gray.std())
            if std < 15.0:
                raise AdbError(
                    f"选定区域几乎是纯色（标准差 {std:.1f} < 15），无法可靠匹配。"
                    f"请点画面上有文字/图标/边缘的区域。"
                )

            # 4) 存模板（覆盖式，按设备隔离的单文件）
            cv2.imwrite(str(ds.marker_path), template)
            ds._fallback_from = None  # 本设备已有自己的模板，清除回退来源

            # 4.3) 持久化到磁盘（重启后端自动恢复，免用户反复重设——真机实测痛点）
            try:
                import json as _json
                marker_file, meta_file = _device_template_files(ds.serial)
                ok_buf, buf = cv2.imencode(".png", template)
                if ok_buf:
                    tw2, th2 = template.shape[1], template.shape[0]
                    marker_file.write_bytes(buf.tobytes())
                    meta_file.write_text(_json.dumps({
                        "w": tw2,
                        "h": th2,
                        "cx": (x1 + tw2 / 2) / w_px,
                        "cy": (y1 + th2 / 2) / h_px,
                        "threshold": ds.marker_threshold,
                        "res": [w_px, h_px],
                    }, ensure_ascii=False), encoding="utf-8")
            except OSError as e:
                print(f"[marker:{ds.serial}] 模板持久化失败（不阻塞）：{e}", file=sys.stderr, flush=True)

            # 4.5) 记录设模板时的屏幕分辨率（审核 F-P1-4：后续分辨率变化检测基准）
            ds._marker_res = (w_px, h_px)

            # 5) 记录到设备会话（运行时 check_marker 用）
            actual_h, actual_w = template.shape[:2]
            ds._marker_template = ds.marker_path
            ds._marker_w = actual_w
            ds._marker_h = actual_h
            # 中心归一化坐标（实际截取后的中心，可能与请求的 cx/cy 略有偏差——因越界裁剪）
            ds._marker_cx = (x1 + actual_w / 2) / w_px
            ds._marker_cy = (y1 + actual_h / 2) / h_px
            ds.set_marker_image(template)
            ds.reset_marker_watch()

            # 6) 返回预览（base64 JPG，体积小，前端 <img> 直接显示）
            ok, buf = cv2.imencode(".jpg", template, [cv2.IMWRITE_JPEG_QUALITY, 80])
            preview_b64 = base64.b64encode(buf).decode("ascii") if ok else ""

            return {
                "ok": True,
                "width": actual_w,
                "height": actual_h,
                "center_x": round(ds._marker_cx, 5),
                "center_y": round(ds._marker_cy, 5),
                "preview_base64": preview_b64,
                "preview_mime": "image/jpeg",
            }
    except AdbOpError as e:
        raise _err(400, str(e))


@app.get("/api/check_marker")
def check_marker(serial: Optional[str] = None) -> dict:
    """截当前屏 + 在模板坐标周围搜索模板，返回是否命中 + 置信度。

    高频轮询用（前端 50-100ms 调一次）。设计要点：
      - 每次重新截图（不复用缓存，避免错过关键帧）
      - 只搜模板坐标 ± MARKER_SEARCH_PADDING 范围（小区域，所以快）
      - cv2.TM_CCOEFF_NORMED 归一化相关系数，最鲁棒（容忍亮度/对比度变化）
      - 置信度 ≥ MARKER_MATCH_THRESHOLD（默认 0.85）才算命中
      - 返回 ms 让前端诊断实际耗时

    未设模板时返回 hit=false + error，不抛异常（前端按未命中处理，会继续等）。
    """
    t0 = time.perf_counter()
    try:
        import cv2

        ds = _target_session(serial)
        with ds.lock:
            # 分辨率变化检测（审核 F-P1-4）：未知或未预热时跳过，仅提示不阻塞
            res_mismatch = False
            if ds._marker_res is not None and getattr(ds.device, "_last_size", None) is not None:
                res_mismatch = tuple(ds.device._last_size) != tuple(ds._marker_res)
            if ds._marker_template is None or not ds._marker_template.exists():
                return {"hit": False, "confidence": 0.0, "ms": 0.0, "error": "未设模板"}

            # 1) 截当前屏
            shot = Path(tempfile.gettempdir()) / f"_cst_marker_chk_{os.getpid()}_{_safe_serial(ds.serial)}.png"
            try:
                ds.device.screenshot(shot)
                scene = cv2.imread(str(shot))
            finally:
                try:
                    shot.unlink()
                except OSError:
                    pass
            if scene is None:
                return {"hit": False, "confidence": 0.0, "ms": 0.0, "error": "截图失败"}

            # 2) 图片元素识别（TemplateMatcher 封装：ROI matchTemplate + 纯色拒绝 + 帧太小兜底）
            matcher = ds.ensure_marker_matcher()
            if matcher is None:
                return {"hit": False, "confidence": 0.0, "ms": 0.0, "error": "模板读失败"}
            mr = matcher.match(scene)

            elapsed_ms = (time.perf_counter() - t0) * 1000
            ds.marker_check_total += 1
            ds.marker_check_last_ms = elapsed_ms
            ds.marker_check_last_conf = mr.confidence

            return {
                "hit": bool(mr.hit),
                "confidence": round(mr.confidence, 4),
                "threshold": ds.marker_threshold,
                "ms": round(elapsed_ms, 1),
                "res_mismatch": res_mismatch,
            }
    except AdbOpError as e:
        return {"hit": False, "confidence": 0.0, "ms": 0.0, "error": str(e)}
    except Exception as e:
        # cv2/numpy 出错不抛 500，让前端能继续轮询（按未命中处理）
        return {"hit": False, "confidence": 0.0, "ms": 0.0, "error": f"{type(e).__name__}: {e}"}


@app.get("/api/marker_status")
def marker_status(serial: Optional[str] = None) -> dict:
    """查询指定设备（默认当前选中）启动成功模板是否已设。"""
    try:
        ds = _target_session(serial)
    except AdbOpError as e:
        raise _err(400, str(e))
    # 本设备模板，或（未设时）可回退到其它设备同分辨率模板，都视为可用
    self_ready = (
        ds._marker_template is not None
        and Path(ds._marker_template).exists()
    )
    if not self_ready:
        ds.ensure_marker_image()  # 触发回退加载（成功则设置 _marker_template/_fallback_from）
    ready = (
        ds._marker_template is not None
        and Path(ds._marker_template).exists()
    )
    return {
        "ready": ready,
        "width": ds._marker_w if ready else 0,
        "height": ds._marker_h if ready else 0,
        "center_x": round(ds._marker_cx, 5) if ready else None,
        "center_y": round(ds._marker_cy, 5) if ready else None,
        "threshold": ds.marker_threshold,           # 审核 E-P1-3：可调阈值
        "threshold_default": MARKER_MATCH_THRESHOLD,
        "fallback_from": ds._fallback_from,          # 非空 = 用的其它设备共用模板
    }


@app.post("/api/preflight_auto")
def preflight_auto(req: ReinstallReq) -> dict:
    """自动循环开跑前自检（目标设备、APK 文件、包名非空）。不占设备锁、不做 adb 卸装。

    多设备：前端逐台调用，每台各自检查模板是否已设（serial 指定设备）。
    """
    errors: list[str] = []
    ds = None
    try:
        ds = _target_session(req.serial)
    except AdbError as e:
        errors.append(str(e))
    if not (req.package or "").strip():
        errors.append("包名为空")
    # iOS 模式用已装 App/IPA，不要求 APK（真机验证发现误拦自动循环）
    apk = None
    if ds is not None and ds.platform != "ios":
        apk = Path(req.apk_path or "")
        if not apk.is_file():
            errors.append(f"APK 不存在：{req.apk_path}（请重新上传）")
    marker_ok = False
    if ds is not None:
        marker_ok = (
            ds._marker_template is not None
            and Path(ds._marker_template).exists()
        )
        if not marker_ok:
            # 允许回退共用模板：本设备没设，但其它设备有同分辨率可用模板时放行
            # （多台跑同一 App、首页 UI 一致时，不必每台都设模板）
            if ds.ensure_marker_image() is not None:
                marker_ok = True
            else:
                errors.append("未设启动元素模板，且无其它设备的共用模板可回退")
    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "apk_name": apk.name if apk and apk.is_file() else "",
        "apk_size_mb": round(apk.stat().st_size / 1048576, 1) if apk and apk.is_file() else 0,
        "marker_ready": marker_ok,
        "device": ds.serial if ds else None,
    }


# ── 项目持久化（手动保存；不复制 APK）──

def _new_project_id(name: str) -> str:
    """从显示名生成安全 id：ascii 前缀 + 短 hash，避免中文路径。"""
    base = re.sub(r"[^A-Za-z0-9_-]", "", (name or "").strip())[:20] or "proj"
    suffix = hashlib.md5(f"{name}-{time.time()}".encode("utf-8")).hexdigest()[:6]
    pid = f"{base}_{suffix}"
    return _safe_project_id(pid)


@app.get("/api/projects")
def list_projects() -> dict:
    """列出本机 projects/ 下全部项目（不含模板预览，列表要轻）。"""
    import json

    items = []
    if not PROJECTS_DIR.is_dir():
        return {"ok": True, "items": []}
    for d in sorted(PROJECTS_DIR.iterdir(), key=lambda p: p.name.lower()):
        if not d.is_dir():
            continue
        meta_path = d / "meta.json"
        if not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        items.append({
            "id": d.name,
            "name": meta.get("name") or d.name,
            "package": meta.get("package") or "",
            "platform": meta.get("platform") or "gp",
            "launch_mode": meta.get("launch_mode") or "pkg",
            "apk_hint": meta.get("apk_hint") or "",
            "updated_at": meta.get("updated_at") or "",
            "has_marker": (d / "marker.png").is_file(),
            "skip_count": len(meta.get("skips") or []),
        })
    return {"ok": True, "items": items}


@app.post("/api/projects")
def create_project(req: ProjectCreateReq) -> dict:
    """新建空项目（只写 meta.json，模板等用户设好后手动保存）。"""
    name = (req.name or "").strip()
    if not name:
        raise _err(400, "项目名不能为空")
    pid = _new_project_id(name)
    pdir = _project_dir(pid)
    if pdir.exists():
        raise _err(400, f"项目 id 冲突：{pid}")
    pdir.mkdir(parents=True, exist_ok=True)
    _write_project_meta(pid, {
        "name": name,
        "package": "",
        "launch_mode": "pkg",
        "tap_x": None,
        "tap_y": None,
        "platform": "gp",
        "apk_hint": "",
        "marker": None,
        "skips": [],
    })
    return {"ok": True, "id": pid, "name": name}


@app.get("/api/projects/{pid}")
def load_project(pid: str) -> dict:
    """加载项目到 Session（模板写回内存/临时文件），并返回表单字段给前端。"""
    try:
        return _apply_project_to_session(pid)
    except AdbError as e:
        raise _err(400, str(e))


@app.put("/api/projects/{pid}")
def save_project(pid: str, req: ProjectSaveReq) -> dict:
    """手动保存：把当前选中设备的模板 + 请求里的表单配置写入 projects/<id>/。

    不复制 APK——只记 apk_hint 文件名提醒用户下次再传。
    多设备：每台设备各自「加载项目 + 设模板 + 保存」时，保存的是当前选中设备
    的那份模板（每台设备模板独立，项目磁盘格式保持单份，后续如需多设备并存再扩展）。
    """
    import shutil

    try:
        pid = _safe_project_id(pid)
        pdir = _project_dir(pid)
        if not (pdir / "meta.json").is_file():
            raise AdbError(f"项目不存在：{pid}，请先新建")
        pdir.mkdir(parents=True, exist_ok=True)

        old = _read_project_meta(pid)
        display_name = (req.name or "").strip() or old.get("name") or pid
        ds = SESSION.current_session
        if ds is None:
            raise AdbError("未选择设备，请先在左上角连接设备")

        with ds.lock:
            marker_meta = None
            marker_dst = pdir / "marker.png"
            if (
                ds._marker_template is not None
                and Path(ds._marker_template).is_file()
            ):
                shutil.copy2(ds._marker_template, marker_dst)
                marker_meta = {
                    "cx": ds._marker_cx,
                    "cy": ds._marker_cy,
                    "w": ds._marker_w,
                    "h": ds._marker_h,
                }
            else:
                marker_dst.unlink(missing_ok=True)

            for old_skip in pdir.glob("skip_*.png"):
                try:
                    old_skip.unlink()
                except OSError:
                    pass
            skips_meta = []
            for t in ds._skip_templates:
                src = Path(t["path"])
                if not src.is_file():
                    continue
                sid = int(t["id"])
                shutil.copy2(src, pdir / f"skip_{sid}.png")
                skips_meta.append({
                    "id": sid,
                    "cx": float(t["cx"]),
                    "cy": float(t["cy"]),
                    "w": int(t.get("w") or 0),
                    "h": int(t.get("h") or 0),
                })

        apk_hint = (req.apk_hint or "").strip()
        if apk_hint:
            apk_hint = Path(apk_hint).name  # 只留文件名，防路径泄漏

        _write_project_meta(pid, {
            "name": display_name,
            "package": (req.package or "").strip(),
            "launch_mode": req.launch_mode,
            "tap_x": req.tap_x,
            "tap_y": req.tap_y,
            "platform": (req.platform or "gp").strip() or "gp",
            "apk_hint": apk_hint,
            "marker": marker_meta,
            "skips": skips_meta,
        })
        return {
            "ok": True,
            "id": pid,
            "name": display_name,
            "has_marker": marker_meta is not None,
            "skip_count": len(skips_meta),
            "apk_hint": apk_hint,
        }
    except AdbError as e:
        raise _err(400, str(e))


@app.patch("/api/projects/{pid}")
def rename_project(pid: str, req: ProjectRenameReq) -> dict:
    """只改显示名，目录 id 不变。"""
    try:
        meta = _read_project_meta(pid)
        name = (req.name or "").strip()
        if not name:
            raise AdbError("项目名不能为空")
        meta["name"] = name
        _write_project_meta(pid, meta)
        return {"ok": True, "id": _safe_project_id(pid), "name": name}
    except AdbError as e:
        raise _err(400, str(e))


@app.delete("/api/projects/{pid}")
def delete_project(pid: str) -> dict:
    """删除项目目录（含模板图）。测速历史在浏览器 localStorage，需前端一并清。"""
    import shutil

    try:
        pdir = _project_dir(pid)
        if not pdir.is_dir():
            raise AdbError(f"项目不存在：{pid}")
        shutil.rmtree(pdir, ignore_errors=False)
        return {"ok": True, "id": _safe_project_id(pid)}
    except AdbError as e:
        raise _err(400, str(e))


@app.post("/api/set_skip_template")
def set_skip_template(req: SetMarkerReq) -> dict:
    """添加一条跳过弹窗模板（通知权限按钮等）。最多 SKIP_TEMPLATE_MAX 条。

    与启动模板相同：点按钮中心 → 截小区域存盘。自动测速轮询命中后对该中心 tap，
    **不停表**，只为关掉挡在启动元素前面的系统/应用弹窗。
    """
    if not (0.0 <= req.cx <= 1.0 and 0.0 <= req.cy <= 1.0):
        raise _err(400, f"cx/cy 必须在 0~1 之间，收到 cx={req.cx} cy={req.cy}")
    try:
        import cv2

        ds = _target_session(req.serial)
        with ds.lock:
            if len(ds._skip_templates) >= SKIP_TEMPLATE_MAX:
                raise AdbError(f"跳过模板已满（最多 {SKIP_TEMPLATE_MAX} 个），请先清除再添加")

            shot = Path(tempfile.gettempdir()) / f"_cst_skip_src_{os.getpid()}_{_safe_serial(ds.serial)}.png"
            try:
                ds.device.screenshot(shot)
                img = cv2.imread(str(shot))
            finally:
                try:
                    shot.unlink()
                except OSError:
                    pass
            if img is None:
                raise AdbError("截图失败或 cv2 读图失败")

            h_px, w_px = img.shape[:2]
            cx_px = int(req.cx * w_px)
            cy_px = int(req.cy * h_px)

            if req.box_w and req.box_h and 0 < req.box_w <= 1 and 0 < req.box_h <= 1:
                tw = max(40, int(req.box_w * w_px))
                th = max(40, int(req.box_h * h_px))
            else:
                tw = SKIP_DEFAULT_W
                th = SKIP_DEFAULT_H

            x1 = cx_px - tw // 2
            y1 = cy_px - th // 2
            x2 = x1 + tw
            y2 = y1 + th
            if x1 < 0:
                x1, x2 = 0, tw
            elif x2 > w_px:
                x2, x1 = w_px, w_px - tw
            if y1 < 0:
                y1, y2 = 0, th
            elif y2 > h_px:
                y2, y1 = h_px, h_px - th

            template = img[y1:y2, x1:x2]
            if template.size == 0:
                raise AdbError(f"跳过模板截取为空：区域 ({x1},{y1})-({x2},{y2})")

            gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
            std = float(gray.std())
            if std < 15.0:
                raise AdbError(
                    f"选定区域几乎是纯色（标准差 {std:.1f} < 15），无法可靠匹配。"
                    f"请点「允许/不允许/跳过」等按钮文字区域。"
                )

            ds.skip_dir.mkdir(parents=True, exist_ok=True)
            # id 用递增：已有 max+1，避免删中间后撞名
            next_id = (max((t["id"] for t in ds._skip_templates), default=0) + 1)
            path = ds.skip_dir / f"skip_{next_id}.png"
            cv2.imwrite(str(path), template)
            actual_h, actual_w = template.shape[:2]
            cx_n = (x1 + actual_w / 2) / w_px
            cy_n = (y1 + actual_h / 2) / h_px

            ok, buf = cv2.imencode(".jpg", template, [cv2.IMWRITE_JPEG_QUALITY, 80])
            preview_b64 = base64.b64encode(buf).decode("ascii") if ok else ""

            entry = {
                "id": next_id,
                "path": path,
                "cx": cx_n,
                "cy": cy_n,
                "w": actual_w,
                "h": actual_h,
                "img": template.copy(),  # 内存缓存
                "matcher": None,          # TemplateMatcher 惰性构造（图片元素识别封装）
                "last_tap_at": 0.0,
                "preview_base64": preview_b64,
                "preview_mime": "image/jpeg",
            }
            ds._skip_templates.append(entry)
            print(f"[skip:{ds.serial}] 添加模板 #{next_id} {actual_w}x{actual_h} @({cx_n:.3f},{cy_n:.3f})", flush=True)

            return {
                "ok": True,
                "id": next_id,
                "width": actual_w,
                "height": actual_h,
                "center_x": round(cx_n, 5),
                "center_y": round(cy_n, 5),
                "count": len(ds._skip_templates),
                "preview_base64": preview_b64,
                "preview_mime": "image/jpeg",
            }
    except AdbOpError as e:
        raise _err(400, str(e))


@app.get("/api/skip_templates")
def list_skip_templates(serial: Optional[str] = None) -> dict:
    """列出指定设备（默认当前选中）的跳过模板（含预览），供前端刷新列表。"""
    ds = _target_session(serial)
    items = []
    for t in ds._skip_templates:
        items.append({
            "id": t["id"],
            "width": t["w"],
            "height": t["h"],
            "center_x": round(t["cx"], 5),
            "center_y": round(t["cy"], 5),
            "preview_base64": t.get("preview_base64", ""),
            "preview_mime": t.get("preview_mime", "image/jpeg"),
        })
    return {"ok": True, "items": items, "max": SKIP_TEMPLATE_MAX}


@app.post("/api/clear_skip_templates")
def clear_skip_templates(req: ClearSkipReq) -> dict:
    """清除跳过模板：指定 id 删一条，否则清空全部。"""
    try:
        ds = _target_session(req.serial)
    except AdbOpError as e:
        raise _err(400, str(e))
    with ds.lock:
        if req.id is None:
            for t in ds._skip_templates:
                try:
                    Path(t["path"]).unlink(missing_ok=True)
                except OSError:
                    pass
            ds._skip_templates.clear()
            return {"ok": True, "count": 0}
        kept = []
        removed = False
        for t in ds._skip_templates:
            if t["id"] == req.id:
                try:
                    Path(t["path"]).unlink(missing_ok=True)
                except OSError:
                    pass
                removed = True
            else:
                kept.append(t)
        ds._skip_templates = kept
        if not removed:
            raise _err(400, f"找不到跳过模板 id={req.id}")
        return {"ok": True, "count": len(kept)}


@app.get("/api/check_auto")
def check_auto(
    check_skips: bool = Query(
        True,
        description="是否匹配跳过弹窗模板。二次冷启动应传 false（首次装后不会再弹允许类弹窗）",
    ),
    serial: Optional[str] = Query(
        None,
        description="目标设备 serial；不传用当前选中。多设备并行时各自带 serial 轮询",
    ),
) -> dict:
    """自动测速轮询（一次截图）：可选先跳过弹窗，再判定启动成功。

    返回字段：
      - skipped: 是否刚点击了跳过按钮（命中跳过模板并 tap）
      - skip_id / skip_confidence: 若 skipped
      - check_skips: 本次是否启用了跳过匹配（回显）
      - hit: 启动成功是否已确认（连续 MARKER_CONFIRM_FRAMES 帧过阈，且满足上升沿）
      - confidence / threshold / streak / confirm_need / rising_ready
      - shot_ms / match_ms / ms：耗时拆分（证明瓶颈在截图）
    设计：一次截图兼顾两者（热路径 raw|gzip）；模板走内存缓存；停表用连续确认+上升沿抗抖。
    二次冷启动请 check_skips=false，只扫启动成功模板，避免空扫跳过模板。
    """
    t0 = time.perf_counter()
    shot_via = "—"
    try:
        import cv2

        ds = _target_session(serial)
        with ds.lock:
            # 0) 分辨率变化检测（审核 F-P1-4）：模板按设模板时分辨率绑定；
            #    仅提示不阻塞停表；_marker_res 未知（项目加载）或 _last_size 未预热时跳过
            res_mismatch = False
            if ds._marker_res is not None and getattr(ds.device, "_last_size", None) is not None:
                res_mismatch = tuple(ds.device._last_size) != tuple(ds._marker_res)

            # 1) 截当前屏（热路径：raw|gzip → BGR，免落盘）
            t_shot0 = time.perf_counter()
            try:
                scene, shot_via = ds.device.screenshot_bgr()
            except AdbOpError as e:
                shot_ms = (time.perf_counter() - t_shot0) * 1000
                return {
                    "skipped": False, "hit": False, "confidence": 0.0,
                    "check_skips": bool(check_skips),
                    "ms": round(shot_ms, 1), "shot_ms": round(shot_ms, 1),
                    "match_ms": 0.0, "shot_via": shot_via, "error": str(e),
                }
            shot_ms = (time.perf_counter() - t_shot0) * 1000
            if scene is None:
                return {
                    "skipped": False, "hit": False, "confidence": 0.0,
                    "check_skips": bool(check_skips),
                    "ms": round(shot_ms, 1), "shot_ms": round(shot_ms, 1),
                    "match_ms": 0.0, "shot_via": shot_via, "error": "截图失败",
                }

            now = time.time()
            t_match0 = time.perf_counter()

            # 2) 跳过模板（仅 check_skips=true，一般只开在首次冷启动）
            #    命中且过冷却 → tap；打断 marker 连续帧
            #    本轮已点过的 skip id 跳过，避免反复 return skipped 堵死启动模板识别
            if check_skips:
                for t in ds._skip_templates:
                    if t["id"] in ds._skip_fired_ids:
                        continue
                    matcher = t.get("matcher")
                    if matcher is None:
                        # 惰性构造 TemplateMatcher（图片元素识别封装，见 adb_helper）
                        template = t.get("img")
                        if template is None:
                            path = Path(t["path"])
                            if path.exists():
                                template = cv2.imread(str(path))
                                t["img"] = template
                        if template is None:
                            continue
                        matcher = TemplateMatcher(
                            template, t["cx"], t["cy"],
                            padding=SKIP_SEARCH_PADDING, threshold=SKIP_MATCH_THRESHOLD,
                        )
                        t["matcher"] = matcher
                    conf = matcher.match(scene).confidence
                    if conf < SKIP_MATCH_THRESHOLD:
                        continue
                    if now - float(t.get("last_tap_at") or 0) < SKIP_TAP_COOLDOWN_S:
                        continue
                    ds.device.tap_norm(t["cx"], t["cy"])
                    t["last_tap_at"] = now
                    ds._skip_fired_ids.add(t["id"])
                    ds._marker_hit_streak = 0
                    # 弹窗屏 ≠ 启动成功态：视为已见过「低于成功阈值」，避免点完后
                    # 首页已就绪时上升沿永远充不上能（R5 死锁：conf 一直 100% 等上升沿）
                    ds._marker_seen_below = True
                    match_ms = (time.perf_counter() - t_match0) * 1000
                    elapsed_ms = (time.perf_counter() - t0) * 1000
                    print(
                        f"[skip] 自动点击 #{t['id']} conf={conf:.3f} @({t['cx']:.3f},{t['cy']:.3f})"
                        f" shot={shot_ms:.0f}ms via={shot_via} match={match_ms:.0f}ms",
                        flush=True,
                    )
                    return {
                        "skipped": True,
                        "skip_id": t["id"],
                        "skip_confidence": round(conf, 4),
                        "skip_cx": round(float(t["cx"]), 5),
                        "skip_cy": round(float(t["cy"]), 5),
                        "check_skips": True,
                        "hit": False,
                        "confidence": 0.0,
                        "ms": round(elapsed_ms, 1),
                        "shot_ms": round(shot_ms, 1),
                        "match_ms": round(match_ms, 1),
                        "shot_via": shot_via,
                    }

            # 3) 启动成功模板（TemplateMatcher 封装 + 连续确认 + 上升沿）
            matcher = ds.ensure_marker_matcher()
            if matcher is None:
                match_ms = (time.perf_counter() - t_match0) * 1000
                elapsed_ms = (time.perf_counter() - t0) * 1000
                return {
                    "skipped": False, "hit": False, "confidence": 0.0,
                    "check_skips": bool(check_skips),
                    "ms": round(elapsed_ms, 1), "shot_ms": round(shot_ms, 1),
                    "match_ms": round(match_ms, 1), "shot_via": shot_via,
                    "error": "未设模板",
                }

            conf = matcher.match(scene).confidence
            match_ms = (time.perf_counter() - t_match0) * 1000
            elapsed_ms = (time.perf_counter() - t0) * 1000

            above = conf >= ds.marker_threshold
            if not above:
                ds._marker_seen_below = True
                ds._marker_hit_streak = 0
            else:
                if MARKER_REQUIRE_RISING_EDGE and not ds._marker_seen_below:
                    # 开跑时桌面/残留已过高：等掉下去再上来，避免误停
                    ds._marker_hit_streak = 0
                else:
                    ds._marker_hit_streak += 1

            hit = (
                above
                and ds._marker_hit_streak >= MARKER_CONFIRM_FRAMES
                and (not MARKER_REQUIRE_RISING_EDGE or ds._marker_seen_below)
            )

            ds.marker_check_total += 1
            ds.marker_check_last_ms = elapsed_ms
            ds.marker_check_last_conf = conf
            return {
                "skipped": False,
                "hit": bool(hit),
                "confidence": round(conf, 4),
                "threshold": ds.marker_threshold,
                "above": bool(above),
                "streak": ds._marker_hit_streak,
                "confirm_need": MARKER_CONFIRM_FRAMES,
                "rising_ready": bool(ds._marker_seen_below) or (not MARKER_REQUIRE_RISING_EDGE),
                "check_skips": bool(check_skips),
                "ms": round(elapsed_ms, 1),
                "shot_ms": round(shot_ms, 1),
                "match_ms": round(match_ms, 1),
                "shot_via": shot_via,
                "res_mismatch": res_mismatch,
            }
    except AdbOpError as e:
        return {
            "skipped": False, "hit": False, "confidence": 0.0,
            "check_skips": bool(check_skips), "shot_via": shot_via,
            "ms": 0.0, "error": str(e),
        }
    except Exception as e:
        return {
            "skipped": False, "hit": False, "confidence": 0.0,
            "check_skips": bool(check_skips), "shot_via": shot_via,
            "ms": 0.0, "error": f"{type(e).__name__}: {e}",
        }


@app.post("/api/marker_watch_reset")
def marker_watch_reset(req: Optional[dict] = None) -> dict:
    """测速开始前清零连续确认/上升沿（cold_start 成功时也会自动清）。

    body 可选：{"serial": "..."} 指定设备，默认当前选中。
    """
    serial = (req or {}).get("serial") if isinstance(req, dict) else None
    try:
        ds = _target_session(serial)
    except AdbOpError as e:
        raise _err(400, str(e))
    ds.reset_marker_watch()
    return {
        "ok": True,
        "confirm_need": MARKER_CONFIRM_FRAMES,
        "rising_edge": MARKER_REQUIRE_RISING_EDGE,
        "threshold": ds.marker_threshold,
    }


@app.post("/api/tap")
def tap(req: TapReq) -> dict:
    try:
        ds = _target_session(req.serial)
        with ds.lock:
            if req.norm:
                ds.device.tap_norm(req.x, req.y)
            else:
                ds.device.tap_pixel(int(req.x), int(req.y))
    except AdbOpError as e:
        raise _err(400, str(e))
    return {"ok": True}


@app.post("/api/key")
def key(req: KeyReq) -> dict:
    try:
        ds = _target_session(req.serial)
        with ds.lock:
            ds.device.keyevent(req.code)
    except AdbOpError as e:
        raise _err(400, str(e))
    return {"ok": True}


@app.post("/api/swipe")
def swipe(req: SwipeReq) -> dict:
    try:
        ds = _target_session(req.serial)
        with ds.lock:
            if req.norm:
                w, h = ds.device.screen_size()
                x1, y1 = int(req.x1 * w), int(req.y1 * h)
                x2, y2 = int(req.x2 * w), int(req.y2 * h)
            else:
                x1, y1, x2, y2 = int(req.x1), int(req.y1), int(req.x2), int(req.y2)
            ds.device.swipe(x1, y1, x2, y2, req.dur_ms)
    except AdbOpError as e:
        raise _err(400, str(e))
    return {"ok": True}


@app.post("/api/launch_pkg")
def launch_pkg(req: LaunchPkgReq) -> dict:
    try:
        ds = _target_session(req.serial)
        with ds.lock:
            ds.device.launch_package(req.package)
    except AdbOpError as e:
        raise _err(400, str(e))
    return {"ok": True}


@app.post("/api/force_stop")
def force_stop(req: ForceStopReq) -> dict:
    try:
        ds = _target_session(req.serial)
        with ds.lock:
            ds.device.force_stop(req.package)
    except AdbOpError as e:
        raise _err(400, str(e))
    return {"ok": True}


@app.post("/api/kill_all")
def kill_all(req: Optional[KillAllReq] = None) -> dict:
    """测速间隔清后台：am kill-all（方案 A，温和）。"""
    body = req or KillAllReq()
    try:
        ds = _target_session(body.serial)
        with ds.lock:
            out = ds.device.kill_all()
    except AdbOpError as e:
        raise _err(400, str(e))
    return {"ok": True, "log": out or "(ok)"}


def _apk_info(path: Path) -> Optional[dict]:
    """APK 文件指纹（审核 B-P0-2：首冷样本跨版本可比性字段）。

    只用于报告按 APK 版本分组；不叠加时间戳/指纹等「可信度增强」（教训三）。
    计算失败返回 None（不阻塞安装流程）。sha256 前 12 位 hex，流式读防大文件内存峰值。
    """
    try:
        size = path.stat().st_size
        h = hashlib.sha256()
        with path.open("rb") as f:
            while chunk := f.read(1024 * 1024):
                h.update(chunk)
        return {
            "name": path.name,
            "size_bytes": size,
            "sha256_prefix": h.hexdigest()[:12],
        }
    except OSError:
        return None


@app.post("/api/reinstall")
def reinstall(req: ReinstallReq) -> dict:
    try:
        ds = _target_session(req.serial)
    except AdbOpError as e:
        # 前端按 {ok:false} 结构处理（与其它错误路径一致，避免 500）
        return {"ok": False, "error": str(e), "log": [], "apk_info": None}
    # 锁外先查 APK：否则直播截图占着设备锁时，连「文件不存在」都要干等十几秒，
    # 前端只看到「卸装重装」一行日志，像没执行。
    apk = Path(req.apk_path)
    if not apk.is_file():
        return {
            "ok": False,
            "error": f"APK 文件不存在：{req.apk_path}（若刚重启过后端，请重新上传 APK）",
            "log": [],
        }
    # 锁外算 APK 指纹（百 MB 级哈希数百 ms，不占设备锁）
    apk_info = _apk_info(apk)
    print(f"[reinstall:{ds.serial}] 开始 pkg={req.package} apk={apk.name} size={apk.stat().st_size}", flush=True)
    try:
        with ds.lock:
            print("[reinstall] 已拿到设备锁，执行 uninstall…", flush=True)
            log = ds.device.reinstall(req.package, req.apk_path)
    except AdbOpError as e:
        print(f"[reinstall] 失败：{e}", flush=True)
        return {"ok": False, "error": str(e), "log": [], "apk_info": apk_info}
    print("[reinstall] 完成", flush=True)
    return {"ok": True, "log": log, "apk_info": apk_info}


@app.post("/api/verify_launch")
def verify_launch(req: VerifyLaunchReq) -> dict:
    """启动 App 并校验前台包名与期望一致（防包名填错/装错包）。

    Android：launch 后查 ``dumpsys window`` 的 mCurrentFocus/mFocusedApp 解析前台
    包名，与期望对比，返回 match 布尔值——自动校验，不用肉眼看。
    iOS：非越狱无等效前台检测，launch 成功即返回并提示看画面确认。
    """
    ds = _target_session(req.serial)
    try:
        with ds.lock:
            ds.device.launch_package(req.package)
            if ds.platform == "ios":
                return {
                    "ok": True, "launched": True, "match": None,
                    "note": "iOS 无法读取前台包名（非越狱），请确认画面中的 App",
                }
            # Android：等前台 Activity 稳定后解析前台包名
            time.sleep(2.5)
            out = ds.device.run(
                ["shell", "sh", "-c", "dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'"],
                check=False, timeout=10.0,
            )
            fg: Optional[str] = None
            for m in re.finditer(r"(?:mCurrentFocus|mFocusedApp)=.*?([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+)/", out):
                fg = m.group(1)
                break
            if fg is None:
                return {
                    "ok": True, "launched": True, "match": None,
                    "note": "无法解析前台包名（ROM 差异），请确认画面中的 App",
                    "raw": out.strip()[:200],
                }
            return {
                "ok": True, "launched": True,
                "match": fg == req.package,
                "foreground_pkg": fg,
                "expected": req.package,
            }
    except AdbOpError as e:
        raise _err(400, str(e))


@app.post("/api/marker_threshold")
def set_marker_threshold(req: MarkerThresholdReq) -> dict:
    """设置指定设备（默认当前选中）的模板匹配阈值（审核 E-P1-3）。

    每设备独立（不同分辨率/UI 风格设备可不同阈值）；内存变量，重启还原默认 0.85
    （与模板本身行为一致，前端 UI 有提示）。阈值变化时失效 matcher 缓存（重建）。
    """
    try:
        ds = _target_session(req.serial)
    except AdbOpError as e:
        raise _err(400, str(e))
    with ds.lock:
        ds.marker_threshold = req.threshold
        ds._marker_matcher = None  # matcher 带阈值参数，必须重建
    return {"ok": True, "threshold": ds.marker_threshold, "default": MARKER_MATCH_THRESHOLD}


@app.post("/api/sys_baseline")
def sys_baseline(req: SysBaselineReq) -> dict:
    """系统对照模式：am start -W 连跑 N 轮，输出 TotalTime 均值/中位数。

    目的（审核 A-P0-1）：与工具模板停表口径做交叉验证。注意口径差异——
    am start -W 的 TotalTime 终点是系统首帧绘制，模板停表终点是启动成功页
    元素就绪；两者并排看趋势一致性，差值表述为「口径偏差」而非「精度偏差」。
    只读打点：不写 records、不影响任何现有统计。全程持目标设备锁（对齐 §2.4）。
    """
    ds = _target_session(req.serial)
    try:
        with ds.lock:
            # 1) 解析可启动组件（resolve-activity --brief 输出形如 com.pkg/.MainActivity）
            out = ds.device.run(
                ["shell", "cmd", "package", "resolve-activity", "--brief", req.package],
                timeout=10.0,
            )
            lines = [l.strip() for l in (out or "").splitlines() if l.strip()]
            component = lines[-1] if lines else ""
            if not component or "/" not in component:
                raise AdbError(
                    f"解析不到可启动 Activity：pkg={req.package}"
                    + (f"（adb 输出：{out.strip()!r}）" if out.strip() else "（空输出，设备可能离线）")
                )

            # 2) 连跑 N 轮：force-stop → 冷却 → am start -W
            samples: list[dict] = []
            errors: list[str] = []
            for i in range(req.rounds):
                ds.device.force_stop(req.package)
                if req.cooldown_s > 0:
                    time.sleep(req.cooldown_s)
                try:
                    raw = ds.device.run(
                        ["shell", "am", "start", "-W", component],
                        timeout=30.0,
                    )
                except AdbOpError as e:
                    errors.append(f"第 {i + 1} 轮 am start 失败：{e}（已跳过）")
                    continue
                m_this = re.search(r"ThisTime:\s*(\d+)", raw)
                m_total = re.search(r"TotalTime:\s*(\d+)", raw)
                m_wait = re.search(r"WaitTime:\s*(\d+)", raw)
                if not m_total:
                    errors.append(f"第 {i + 1} 轮解析失败（无 TotalTime）：{raw.strip()[:80]}（已跳过）")
                    continue
                samples.append({
                    "idx": i + 1,
                    "this_ms": int(m_this.group(1)) if m_this else 0,
                    "total_ms": int(m_total.group(1)),
                    "wait_ms": int(m_wait.group(1)) if m_wait else 0,
                    "raw": raw.strip(),
                })

            if not samples:
                raise AdbError(f"全部 {req.rounds} 轮均无有效样本：" + ("；".join(errors) or "未知原因"))

            totals = [s["total_ms"] for s in samples]
            n = len(totals)
            ordered = sorted(totals)
            if n % 2:
                median = ordered[n // 2]
            else:
                median = (ordered[n // 2 - 1] + ordered[n // 2]) / 2

            return {
                "ok": True,
                "package": req.package,
                "component": component,
                "rounds": req.rounds,
                "samples": samples,
                "errors": errors,
                "stats": {
                    "total_mean_ms": round(sum(totals) / n, 1),
                    "total_median_ms": round(median, 1),
                    "n": n,
                },
            }
    except AdbOpError as e:
        raise _err(400, str(e))


@app.post("/api/cold_start")
def cold_start(req: ColdStartReq) -> dict:
    """冷启动编排：force_stop → tap/launch，返回 start_wall（供诊断）。

    计时由前端完成（v1 单一 performance.now() 方案，详见 ColdStartReq docstring）。
    本端点不自动回主页 —— 用户需确保启动前已在桌面。独立的回主页能力在
    前端"回主页"按钮 + /api/key 端点，与启动流程解耦。
    全程在目标设备锁下执行（审核高3：force_stop + tap 必须串行，避免并发竞争）。
    """
    try:
        ds = _target_session(req.serial)
        with ds.lock:
            # 1) 先把上一次的同包进程杀掉，确保冷启动
            if req.package:
                ds.device.force_stop(req.package)

            # 2) 预热 screen_size（如果还没缓存），避免它计入 tap_norm 的执行
            if getattr(ds.device, "_last_size", None) is None:
                ds.device.screen_size()

            # 3) 在 tap/monkey 命令实际发出前一刻记录 wall 时间（仅供诊断/将来用）
            start_wall = time.time()

            if req.mode == "tap":
                if req.x is None or req.y is None:
                    raise _err(400, "tap 模式需要 x, y 坐标")
                ds.device.tap_norm(req.x, req.y)
            elif req.mode == "pkg":
                if not req.package:
                    raise _err(400, "pkg 模式需要 package")
                ds.device.launch_package(req.package)
            else:
                raise _err(400, f"未知 mode：{req.mode}")

        # 新一次启动观察：清零 streak / 已点跳过；
        # after_force_stop=True：上面若杀过包（或本就无包可杀），视为已离开成功页，种上升沿
        ds.reset_marker_watch(after_force_stop=True)

        return {
            "ok": True,
            "start_wall": start_wall,   # unix epoch 秒，仅供诊断；前端计时用 performance.now() 不消费此字段
            "marker_confirm_frames": MARKER_CONFIRM_FRAMES,
            "marker_rising_edge": MARKER_REQUIRE_RISING_EDGE,
            "marker_threshold": ds.marker_threshold,
            "marker_rising_seeded": True,  # 诊断：本趟上升沿已因 force_stop 预置
        }
    except AdbOpError as e:
        raise _err(400, str(e))


# ── Word 性能报告导出 ────────────────────────────────────────────────────

class ReportSampleData(BaseModel):
    """单条样本（首次/二次冷启动）。"""
    time: float                        # 毫秒
    date: str = ""
    abnormal: bool = False
    apkVersion: Optional[str] = None


class ReportDeviceData(BaseModel):
    """报告中的单台设备数据。"""
    serial: str = ""
    model: str = ""
    label: str = ""
    brand: str = "/"
    osVersion: str = "/"
    cpu: str = "/"
    ram: str = "/"
    resolution: str = "/"
    hardware: str = ""
    firsts: list[ReportSampleData] = Field(default_factory=list)
    seconds: list[ReportSampleData] = Field(default_factory=list)


class ReportExportReq(BaseModel):
    """导出 Word 性能报告请求。"""
    title: str = "冷启动测速报告"
    testDate: str = ""
    appName: str = ""
    platform: str = "gp"
    platformLabel: str = "GP（安卓）"
    # iOS 首次冷启动均值调整秒数（与前端 iosFirstAdjustSec 对齐，默认 1.0，0 关闭）。
    # report_docx 在 platform=='ios' 时对首次均值减去此值（剔除 TestFlight 弹窗时间）。
    iosFirstAdjustSec: float = 1.0
    launchMode: str = "模拟点击图标"
    launchModeDesc: str = "模拟点击图标"
    plannedRounds: int = 5
    totalSec: float = 0
    success: bool = True
    error: str = ""
    devices: list[ReportDeviceData] = Field(default_factory=list)
    auditLog: list[str] = Field(default_factory=list)


@app.post("/api/export_report_docx")
def export_report_docx(req: ReportExportReq) -> Response:
    """生成 Word 性能报告文档（.docx），按参考模板格式一比一输出。

    前端收集 lastReportCtx + records 数据 POST 过来，后端用 python-docx 生成
    格式化的 Word 文档返回下载。格式参照：
    20260625-GP GOGO！Blast 1期&新4期性能对比测试报告.docx
    """
    try:
        from report_docx import generate_report
        data = req.model_dump()
        docx_bytes = generate_report(data)
        title = str(data.get("title") or "report").strip()
        # Content-Disposition 头会被 starlette 按 latin-1 编码（实测：中文文件名
        # 必抛 UnicodeEncodeError → 500）。filename= 只放 ASCII 回退名，中文名走
        # RFC 5987 filename*=UTF-8''（percent 编码后仍是 ASCII），浏览器优先取它。
        ascii_name = re.sub(r'[^A-Za-z0-9_.\-]', '_', title)[:60] or "report"
        filename = f"{ascii_name}.docx"
        encoded_name = f"{quote(title[:60])}.docx"
        return Response(
            content=docx_bytes,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={
                "Content-Disposition": (
                    f"attachment; filename=\"{filename}\"; filename*=UTF-8''{encoded_name}"
                ),
                **_FRONTEND_NO_CACHE_HEADERS,
            },
        )
    except ImportError as e:
        # 区分两种 ImportError，给出可操作的提示：
        #   1) report_docx 模块自身缺失（安装版打包遗漏 backend/report_docx.py）
        #   2) report_docx 内部 from docx import 失败（python-docx 依赖未装）
        # 靠 ImportError.name（缺失的顶层模块名）判断，无 name 时回退到消息匹配。
        missing = getattr(e, "name", "") or ""
        if missing == "docx" or "docx" in str(e).lower():
            raise _err(500, "python-docx 未安装，无法生成 Word 报告")
        raise _err(500, "report_docx 模块缺失（安装版可能打包遗漏），无法生成 Word 报告")
    except Exception as e:
        raise _err(500, f"生成报告失败：{e}")


# ── 静态资源（前端单文件）─────────────────────────────────────────────

STATIC_DIR = ROOT / "static"

# 前端是本地单文件且更新频繁：Chromium 对无 Cache-Control 的 200 响应会做
# 启发式缓存，导致用户升级后仍看到旧版页面（曾实测：旧 index.html 残留）。
# 所有前端资源一律 no-store，客户端每次启动都拿最新版本。
_FRONTEND_NO_CACHE_HEADERS = {"Cache-Control": "no-store, no-cache, must-revalidate"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers=_FRONTEND_NO_CACHE_HEADERS)


@app.get("/{path:path}")
def static_fallback(path: str) -> FileResponse:
    """其它路径都尝试从 static/ 取，找不到回 index.html（SPA 兜底）。

    安全：resolve 后验证目标仍在 STATIC_DIR 内，防路径穿越
    （如 /..%2Fserver.py 读取项目源码）。越界或不存在都回 index.html。
    """
    candidate = (STATIC_DIR / path).resolve()
    try:
        candidate.relative_to(STATIC_DIR.resolve())
    except ValueError:
        # 越出 STATIC_DIR，拒绝（路径穿越攻击）
        return FileResponse(STATIC_DIR / "index.html", headers=_FRONTEND_NO_CACHE_HEADERS)
    if candidate.is_file():
        return FileResponse(candidate, headers=_FRONTEND_NO_CACHE_HEADERS)
    return FileResponse(STATIC_DIR / "index.html", headers=_FRONTEND_NO_CACHE_HEADERS)
