"""App 冷启测速 —— FastAPI 后端。

参考 GameAuto（D:\\work\\GameAuto）的设计哲学：
  - 自动测速热路径截图：`adb exec-out sh -c 'screencap | gzip -1 -c'`（raw+gzip，
    Pixel 6a 实测 ~350ms，优于 `screencap -p` 的 ~580ms）；失败回退 PNG
  - 落盘/模板/直播仍可用 `screencap -p` PNG
  - OCR 用 RapidOCR（ONNX，跨平台，归一化坐标输出）
  - 所有 adb 调用集中在 AdbDevice，便于复用 + 加锁
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
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Literal, Any

# uvicorn 加载本模块时把根目录加进 sys.path，便于 from server import ...
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 内置 adb（同目录 adb\\adb.exe），找不到再回退 PATH
try:
    from adb_helper import AdbHelper, AdbHelperError
except ImportError:
    AdbHelper = None
    AdbHelperError = RuntimeError

_BUNDLED_ADB = ROOT / "adb" / "adb.exe"
if _BUNDLED_ADB.exists():
    ADB_EXE = str(_BUNDLED_ADB)
else:
    try:
        ADB_EXE = AdbHelper.resolve_adb_path(project_root=ROOT) if AdbHelper else "adb"
    except (AdbHelperError, OSError):
        ADB_EXE = "adb"

# 内置 iOS 工具链（同目录 ios\\idevice_id.exe）
# 打包后 ROOT 落在 resources/backend，ios/ 在 extraResources 里
_BUNDLED_IDEVICE_ID = ROOT / "ios" / "idevice_id.exe"
IDEVICE_ID_EXE = str(_BUNDLED_IDEVICE_ID) if _BUNDLED_IDEVICE_ID.exists() else None

# APK 上传目录（每次上传保留原始文件名，不再覆盖式存储）。
# 复数 _cst_uploads 与老的单数 _cst_upload.apk 区分；启动时整目录清空重建。
APK_UPLOAD_DIR = Path(tempfile.gettempdir()) / "_cst_uploads"

# 启动成功模板图（cv2.matchTemplate 区域比对用，覆盖式单文件）。
# 用户在画面上点"启动成功"元素位置 → 后端截小区域存为这个模板 → 运行时只搜小区域。
# 比 OCR 文字匹配快约 20-40 倍（毫秒级 vs 秒级），冷启动计时精度大幅提升。
MARKER_TEMPLATE_PATH = Path(tempfile.gettempdir()) / "_cst_marker.png"
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
SKIP_TEMPLATE_DIR = Path(tempfile.gettempdir()) / "_cst_skips"
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


def _safe_apk_filename(original: str) -> str:
    """把用户上传的原始文件名过滤成安全的磁盘文件名。

    防御点（可信度要求高，路径攻击必须拦）：
      - 只取 basename：防 ``../../evil.apk`` 路径穿越写到任意目录
      - 非 [A-Za-z0-9_\\-.] 字符（含中文/空格/特殊符号）替换为 ``_``：避免文件系统/adb 命令解析问题
      - 强制 .apk 后缀
      - 过滤后为空或过短，用 ``apk_<short>`` 兜底
      - 与目录内已有文件同名时追加短 hash 后缀（不覆盖，保历史）
    返回值仅为文件名（不含路径），调用方拼到 APK_UPLOAD_DIR 下。
    """
    name = os.path.basename(original or "").strip()
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


# ── ADB 设备 ───────────────────────────────────────────────────────────


# adb install 失败错误码中文翻译。
# adb 输出形如 "Failure [INSTALL_FAILED_OLDER_SDK]"，匹配后附加中文解释，
# 让用户不用查文档就能知道为什么装不上。
_INSTALL_ERROR_CN: dict[str, str] = {
    "INSTALL_FAILED_ALREADY_EXISTS": "应用已存在（请尝试卸载后重装）",
    "INSTALL_FAILED_INVALID_APK": "APK 文件无效或已损坏",
    "INSTALL_FAILED_INVALID_URI": "APK 路径无效",
    "INSTALL_FAILED_INSUFFICIENT_STORAGE": "设备存储空间不足",
    "INSTALL_FAILED_DUPLICATE_PACKAGE": "包名重复",
    "INSTALL_FAILED_NO_SHARED_USER": "共享用户不存在",
    "INSTALL_FAILED_UPDATE_INCOMPATIBLE": "签名不一致，需先卸载已安装的同名应用",
    "INSTALL_FAILED_SHARED_USER_INCOMPATIBLE": "共享用户签名不兼容",
    "INSTALL_FAILED_MISSING_SHARED_LIBRARY": "缺少依赖的共享库",
    "INSTALL_FAILED_REPLACE_COULDNT_DELETE": "无法删除旧版本（残留数据）",
    "INSTALL_FAILED_DEXOPT": "DEX 优化失败（APK 与系统不兼容）",
    "INSTALL_FAILED_OLDER_SDK": "系统版本过低，不满足 APK 最低要求",
    "INSTALL_FAILED_CONFLICTING_PROVIDER": "ContentProvider 权限冲突",
    "INSTALL_FAILED_NEWER_SDK": "系统版本高于 APK 目标版本",
    "INSTALL_FAILED_TEST_ONLY": "APK 是 test-only 构建，需加 -t 参数安装",
    "INSTALL_FAILED_CPU_ABI_INCOMPATIBLE": "CPU 架构不兼容",
    "INSTALL_FAILED_MISSING_FEATURE": "设备缺少 APK 要求的硬件特性",
    "INSTALL_FAILED_CONTAINER_ERROR": "容器错误（SD 卡问题）",
    "INSTALL_FAILED_INVALID_INSTALL_LOCATION": "安装位置无效",
    "INSTALL_FAILED_MEDIA_UNAVAILABLE": "媒体不可用（SD 卡未挂载）",
    "INSTALL_FAILED_VERIFICATION_TIMEOUT": "验证超时",
    "INSTALL_FAILED_VERIFICATION_FAILURE": "验证失败",
    "INSTALL_FAILED_PACKAGE_CHANGED": "包名发生变化",
    "INSTALL_FAILED_UID_CHANGED": "UID 已变化（需先卸载旧版）",
}


def _translate_install_error(output: str) -> Optional[str]:
    """从 adb install 输出中匹配 INSTALL_FAILED_* 错误码，返回中文解释。"""
    for code, cn in _INSTALL_ERROR_CN.items():
        if code in output:
            return f"{code}：{cn}"
    return None


class AdbError(RuntimeError):
    """adb 调用失败的统一异常，message 直接返回给前端。"""


class AdbDevice:
    """单设备的 adb 操作集合。多设备时由调用方传入不同的 serial。"""

    def __init__(self, serial: Optional[str] = None) -> None:
        self.serial = serial
        self._last_size: Optional[tuple[int, int]] = None

    # ── 基础调用 ──
    def _build_args(self, args: list[str]) -> list[str]:
        if self.serial:
            return [ADB_EXE, "-s", self.serial, *args]
        return [ADB_EXE, *args]

    def run(
        self,
        args: list[str],
        *,
        timeout: float = 30.0,
        check: bool = True,
    ) -> str:
        """同步跑 adb 命令，返回 stdout（已 strip）。失败抛 AdbError。"""
        cmd = self._build_args(args)
        try:
            cp = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise AdbError(f"adb 命令超时（{timeout}s）：{' '.join(args)}") from e
        except FileNotFoundError as e:
            raise AdbError(f"找不到 adb：{ADB_EXE}") from e
        out = cp.stdout.decode("utf-8", "replace")
        err = cp.stderr.decode("utf-8", "replace")
        if check and cp.returncode != 0:
            tail = (err or out).strip().splitlines()
            last = tail[-1] if tail else f"exit={cp.returncode}"
            raise AdbError(last)
        return out.strip()

    def run_bytes(self, args: list[str], *, timeout: float = 30.0) -> bytes:
        """跑 adb 取原始 bytes（用于 screencap -p 直出 PNG）。"""
        cmd = self._build_args(args)
        try:
            cp = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as e:
            raise AdbError(f"adb 命令超时（{timeout}s）：{' '.join(args)}") from e
        if cp.returncode != 0:
            err = cp.stderr.decode("utf-8", "replace").strip().splitlines()
            raise AdbError(err[-1] if err else f"exit={cp.returncode}")
        return cp.stdout

    # ── 设备列表 ──
    @staticmethod
    def devices() -> list[dict]:
        """返回 [{serial, state, model?}]。adb devices 解析。"""
        try:
            out = subprocess.run(
                [ADB_EXE, "devices"],
                capture_output=True,
                timeout=5,
                check=False,
                text=True,
            ).stdout
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            raise AdbError(f"adb devices 失败：{e}") from e
        devs: list[dict] = []
        for line in out.splitlines():
            m = re.match(r"^(\S+)\s+(\S+)\s*$", line)
            if not m:
                continue
            sn, state = m.group(1), m.group(2)
            if sn.startswith("List of") or sn == "*":
                continue
            if state not in ("device", "unauthorized", "offline"):
                continue
            model = ""
            if state == "device":
                try:
                    model = subprocess.run(
                        [ADB_EXE, "-s", sn, "shell", "getprop", "ro.product.model"],
                        capture_output=True, timeout=3, check=False, text=True,
                    ).stdout.strip()
                except Exception:
                    pass
            devs.append({"serial": sn, "state": state, "model": model})
        return devs

    # ── 截图 ──
    def screenshot(self, target: Optional[Path] = None) -> Path:
        """优先 exec-out screencap -p 直出 PNG；不支持则回退 screencap + pull。

        给需要落盘的路径用（设模板 / 直播缓存）。自动测速热路径请用 screenshot_bgr()。
        """
        if target is None:
            fd, name = tempfile.mkstemp(prefix="_cst_", suffix=".png")
            os.close(fd)
            target = Path(name)
        try:
            data = self.run_bytes(["exec-out", "screencap", "-p"], timeout=15.0)
            if len(data) < 1024 or not data.startswith(b"\x89PNG"):
                raise AdbError("exec-out screencap 返回的不是有效 PNG")
            target.write_bytes(data)
            return target
        except AdbError:
            # 回退：手机端落盘 + pull
            device_path = "/sdcard/_cst_shot.png"
            self.run(["shell", "screencap", "-p", device_path], timeout=15.0)
            self.run(["pull", device_path, str(target)], timeout=30.0)
            try:
                self.run(["shell", "rm", "-f", device_path], check=False, timeout=5.0)
            except AdbError:
                pass
            if not target.exists():
                raise AdbError("pull 后截图不存在")
            return target

    def screenshot_bgr(self) -> tuple[Any, str]:
        """截当前屏为 OpenCV BGR ndarray（自动测速热路径）。

        优先：`screencap | gzip -1`（raw，免设备端 PNG 编码；Pixel 6a 实测快于 -p）。
        失败回退：`screencap -p` + cv2.imdecode。

        返回 (bgr, via)，via 为 ``raw_gzip`` / ``png``，供日志诊断。
        """
        import cv2
        import numpy as np

        # 1) raw + gzip（热路径）
        try:
            gz = self.run_bytes(
                ["exec-out", "sh", "-c", "screencap | gzip -1 -c"],
                timeout=15.0,
            )
            if len(gz) < 64:
                raise AdbError("gzip 截图过短")
            raw = gzip.decompress(gz)
            bgr = _raw_screencap_to_bgr(raw)
            return bgr, "raw_gzip"
        except Exception as e:
            # 2) PNG 回退（保持旧路径可用）
            try:
                data = self.run_bytes(["exec-out", "screencap", "-p"], timeout=15.0)
                if len(data) < 1024 or not data.startswith(b"\x89PNG"):
                    raise AdbError("exec-out screencap 返回的不是有效 PNG")
                arr = np.frombuffer(data, dtype=np.uint8)
                bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if bgr is None:
                    raise AdbError("PNG imdecode 失败")
                return bgr, "png"
            except AdbError:
                raise
            except Exception as e2:
                raise AdbError(f"截图失败（gzip: {e}; png: {e2})") from e2

    def screen_size(self) -> tuple[int, int]:
        out = self.run(["shell", "wm", "size"], timeout=5.0)
        m = re.search(r"(\d+)x(\d+)", out)
        if not m:
            raise AdbError(f"解析 wm size 失败：{out!r}")
        self._last_size = (int(m.group(1)), int(m.group(2)))
        return self._last_size

    # ── 输入 ──
    def tap_pixel(self, x: int, y: int) -> None:
        self.run(["shell", "input", "tap", str(x), str(y)], timeout=10.0)

    def tap_norm(self, cx: float, cy: float) -> None:
        w, h = self._last_size or self.screen_size()
        self.tap_pixel(int(cx * w), int(cy * h))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, dur_ms: int = 200) -> None:
        self.run(
            ["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(dur_ms)],
            timeout=15.0,
        )

    def keyevent(self, code: int) -> None:
        self.run(["shell", "input", "keyevent", str(code)], check=False, timeout=5.0)

    def launch_pkg(self, pkg: str) -> None:
        """包名启动（绕过 Launcher 触摸）。

        严格性（对齐教训七）：启动是冷启动测速的关键操作，不能静默失败。
        之前用 check=False 会吞掉 monkey 的非零退出码——错误包名实测返回退出码 252
        且输出 ``No activities found to run, monkey aborted.``，却被当成功，
        导致前端开始计时但应用根本没起来（自动测速多轮无人值守时尤其严重）。

        不用 run 的 check=True：monkey 诊断噪音常走 stderr，check=True 优先取 stderr
        会给用户无意义噪音。这里 check=False 自己解析 stdout：
        成功标志含 ``Events injected``，失败标志含 ``aborted`` / ``No activities found``。
        """
        out = self.run(
            ["shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1"],
            check=False, timeout=10.0,
        )
        low = (out or "").lower()
        # 失败信号优先（即使退出码 0，输出含失败标志也判败，应对 Android 碎片化）
        if "aborted" in low or "no activities found" in low:
            raise AdbError(f"包名启动失败：找不到可启动的 Activity（包名错或无桌面入口）。pkg={pkg}")
        # 成功信号：必须注入了事件。无此标志视为异常（空输出 = 设备离线/adb 断连）
        if "events injected" not in low:
            raise AdbError(f"包名启动失败（monkey 输出异常）：{out.strip() or '(空输出，设备可能离线)'}")

    def force_stop(self, pkg: str) -> None:
        self.run(["shell", "am", "force-stop", pkg], check=False, timeout=5.0)

    def kill_all(self) -> str:
        """清后台：``am kill-all``（不清系统关键前台，比逐包 force-stop 温和）。

        用于自动测速每轮启动前腾内存，减轻启动抖动。失败可接受（部分 ROM 无此命令）。
        """
        out = self.run(["shell", "am", "kill-all"], check=False, timeout=10.0)
        return (out or "").strip()

    def list_packages(self) -> list[str]:
        """列出第三方已装包名。`pm list packages -3` 过滤掉系统 App。

        输出形如 ``package:com.foo\\npackage:com.bar``，去掉前缀返回纯包名列表。
        """
        out = self.run(["shell", "pm", "list", "packages", "-3"], timeout=15.0)
        pkgs = []
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("package:"):
                pkgs.append(line[len("package:"):])
        return sorted(pkgs)

    def reinstall(self, pkg: str, apk_path: str) -> list[str]:
        """卸载重装，返回日志行。失败抛 AdbError。

        保持简单透明：uninstall → 兜底 pm uninstall → install → 判 Success。
        返回的 log 是 adb 原始输出（uninstall: ... / install: ...），不加工。
        前端原样显示，用户能直接看到 adb 真实反馈。

        严格性（审核修复）：adb 输出必须有明确的 Success/Failure 字样。
        空输出（device offline / adb 断连）视为失败，不静默继续——否则
        uninstall 静默失败后 install -r 会覆盖安装，被当"干净重装"，污染首次冷启动。
        """
        if not Path(apk_path).exists():
            raise AdbError(f"APK 文件不存在：{apk_path}")
        log: list[str] = []
        out = self.run(["uninstall", pkg], check=False, timeout=60.0)
        log.append(f"uninstall: {out}")
        # adb uninstall 输出必须有明确结果（Success 或 Failure [xxx]）
        # 空输出 = 设备离线/adb 断连，不能继续（否则 install -r 变覆盖安装）
        if not out:
            raise AdbError("卸载失败：adb 无输出（设备离线或 adb 断连）")
        if "Failure" in out:
            # 兜底：部分设备（如装为系统用户）需要 --user 0 才能卸
            out2 = self.run(["shell", "pm", "uninstall", "--user", "0", pkg], check=False, timeout=30.0)
            log.append(f"pm uninstall --user 0: {out2}")
            if not out2:
                raise AdbError("pm uninstall --user 0 无输出（设备离线或 adb 断连）")
        out3 = self.run(["install", "-r", apk_path], check=False, timeout=180.0)
        log.append(f"install: {out3}")
        if not out3:
            raise AdbError("安装失败：adb 无输出（设备离线或 adb 断连）")
        if "Success" not in out3:
            hint = _translate_install_error(out3)
            raise AdbError(f"安装失败：{out3}" + (f"\n💡 {hint}" if hint else ""))
        return log


def _raw_screencap_to_bgr(raw: bytes):
    """解析 ``adb screencap``（无 -p）原始缓冲 → BGR uint8。

    头：老设备 12 字节 (w,h,fmt)；新设备（含 Pixel）16 字节多一个 colorspace 字段。
    fmt：1=RGBA_8888，2=RGBX_8888，5=BGRA_8888。
    """
    import numpy as np

    if len(raw) < 12:
        raise AdbError("raw screencap 过短")
    w, h, fmt = struct.unpack_from("<III", raw, 0)
    if w <= 0 or h <= 0 or w > 10000 or h > 10000:
        raise AdbError(f"raw screencap 尺寸异常：{w}x{h} fmt={fmt}")
    bpp = 4
    if fmt not in (1, 2, 5):  # RGBA / RGBX / BGRA
        raise AdbError(f"不支持的 raw 像素格式 fmt={fmt}")
    need = w * h * bpp
    if len(raw) - 12 == need:
        off = 12
    elif len(raw) - 16 == need:
        off = 16
    else:
        raise AdbError(
            f"raw screencap 长度不匹配 len={len(raw)} 期望 {need}+12/16 "
            f"({w}x{h} fmt={fmt})"
        )
    rgba = np.frombuffer(raw, dtype=np.uint8, offset=off).reshape(h, w, 4)
    if fmt == 5:  # BGRA
        bgr = np.ascontiguousarray(rgba[:, :, :3])
    else:  # RGBA / RGBX → BGR
        bgr = np.ascontiguousarray(rgba[:, :, 2::-1])
    return bgr


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


class IosDevice:
    """iOS 设备操作（通过 pymobiledevice3 / lockdown 协议）。

    与 AdbDevice 平行，但底层不用 ADB，而是通过 usbmuxd → lockdown。
    非越狱设备限制：
      - 无模拟点击（tap/swipe 不可用，调用时抛 AdbError 提示）
      - 无程序化杀进程（force_stop 是 no-op + 警告日志）
      - 截图、安装/卸载、设备检测、App 启动均可用
    """

    def __init__(self, udid: str) -> None:
        self.udid = udid
        self._last_size: Optional[tuple[int, int]] = None

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
    def screenshot(self, target: Optional[Path] = None) -> Path:
        """截图（PNG），保存到 target 或临时文件。"""
        if target is None:
            fd, name = tempfile.mkstemp(prefix="_cst_ios_", suffix=".png")
            os.close(fd)
            target = Path(name)

        async def _shot():
            from pymobiledevice3.services.screenshot import ScreenshotService
            ld = await self._get_lockdown()
            ss = ScreenshotService(lockdown=ld)
            data = await ss.take_screenshot()  # PNG bytes
            return data

        data = _ios_async(_shot())
        if not data or len(data) < 1024:
            raise AdbError("iOS 截图失败：返回数据为空")
        target.write_bytes(data)
        return target

    def screenshot_bgr(self) -> tuple[Any, str]:
        """截图 → BGR ndarray（自动测速热路径，模板比对用）。

        iOS 截图直接返回 PNG，用 cv2.imdecode 解码。
        比 Android 的 raw|gzip 多一步解码，但 iOS 没有等效的 raw 格式。
        """
        import cv2
        import numpy as np

        async def _shot():
            from pymobiledevice3.services.screenshot import ScreenshotService
            ld = await self._get_lockdown()
            ss = ScreenshotService(lockdown=ld)
            return await ss.take_screenshot()

        data = _ios_async(_shot())
        if not data or len(data) < 1024:
            raise AdbError("iOS 截图失败：返回数据为空")
        arr = np.frombuffer(data, dtype=np.uint8)
        bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if bgr is None:
            raise AdbError("iOS 截图 PNG 解码失败")
        self._last_size = (bgr.shape[1], bgr.shape[0])  # (w, h)
        return bgr, "png"

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
    def launch_pkg(self, bundle_id: str) -> None:
        """通过 lockdown 的 launch service 启动 App。"""
        async def _launch():
            ld = await self._get_lockdown()
            from pymobiledevice3.services.diagnostics import DiagnosticsService
            # 使用 ProcessControl service 启动 App
            # pymobiledevice3 的 launch 接口
            sc = ld.start_service("com.apple.instruments.remoteserver")
            # 简单方案：用 ideviceinstaller 的 launch 功能
            return None
        try:
            _ios_async(_launch())
        except Exception:
            pass  # 启动失败不致命——用户可手动点开

    def force_stop(self, pkg: str) -> None:
        """iOS 非越狱无法程序化杀进程——记日志，用户需手动上滑关闭。"""
        print(f"[ios] force_stop 不支持（非越狱）· 请手动在 App 切换器中上滑关闭 {pkg}",
              file=sys.stderr, flush=True)

    def kill_all(self) -> str:
        return "(iOS 不支持 kill-all)"

    def list_packages(self) -> list[str]:
        """列出已安装 App 的 bundle id。"""
        async def _list():
            ld = await self._get_lockdown()
            from pymobiledevice3.services.installation_proxy import InstallationProxyService
            ip = InstallationProxyService(lockdown=ld)
            apps = await ip.get_apps()  # list of dicts with CFBundleIdentifier
            return [a.get('CFBundleIdentifier', '') for a in apps if a.get('CFBundleIdentifier')]
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
            # 安装
            await ip.install(ipa_path)
            log.append(f"install: {ipa_path}")

        try:
            _ios_async(_do_reinstall())
            return log
        except Exception as e:
            raise AdbError(f"iOS 安装失败：{e}") from e





class Session:
    """全局会话单例。设备操作串行化（adb server 不擅长并发）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._device: Optional[AdbDevice] = None
        self._serial: Optional[str] = None
        self._ocr = OcrEngine()
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

    def reset_marker_watch(self, *, after_force_stop: bool = False) -> None:
        """新一次测速开始前清零连续确认 / 本轮已点跳过。

        after_force_stop=True（cold_start 刚杀过进程）：种子 ``_marker_seen_below=True``。
        含义见模块常量旁注释 / AGENTS——杀进程后不可能还停在「启动成功」页，
        若仍从 False 起算，二次冷启动首帧就已过阈时会永远等不到「先低于再升高」。
        """
        with self._lock:
            self._marker_hit_streak = 0
            # 上升沿：必须先见过低于阈值的帧，再过阈才停表（防桌面残留一上来就误停）。
            # force_stop 之后视为已经「离开成功态」，等价于见过 below。
            self._marker_seen_below = bool(after_force_stop)
            self._skip_fired_ids.clear()

    def set_marker_image(self, img) -> None:
        """写入启动模板内存缓存（拷贝，避免后续被改）。img 为 None 则清空。"""
        if img is None:
            self._marker_img = None
        else:
            self._marker_img = img.copy()

    def ensure_marker_image(self):
        """返回缓存的模板图；缓存空则从磁盘读并填入。"""
        import cv2
        if self._marker_img is not None:
            return self._marker_img
        if self._marker_template is None or not Path(self._marker_template).is_file():
            return None
        im = cv2.imread(str(self._marker_template))
        if im is not None:
            self._marker_img = im
        return self._marker_img

    def select(self, serial: Optional[str], platform: str = "android") -> dict:
        with self._lock:
            self._serial = serial
            self._platform = platform
            if platform == "ios":
                self._device = IosDevice(serial) if serial else None
            else:
                self._device = AdbDevice(serial)
            self._last_shot = None
            return {"serial": serial, "ready": self._device is not None, "platform": platform}

    def current(self) -> dict:
        return {"serial": self._serial, "ready": self._device is not None, "platform": getattr(self, '_platform', 'android')}

    @property
    def device(self):
        if self._device is None:
            raise AdbError("未选择设备，请先在左上角连接设备")
        return self._device

    @contextmanager
    def device_op(self):
        """设备操作统一入口：在同一把锁下执行 ADB I/O（adb server 不擅长并发）。

        所有会触发 adb 命令的端点都应用 ``with SESSION.device_op() as dev:`` 包住，
        替代裸 ``SESSION.device`` 访问，避免直播截图/OCR/自动测速/卸装操作并发竞争。
        """
        with self._lock:
            yield self.device

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
        with self._lock:
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

            target = Path(tempfile.gettempdir()) / f"_cst_live_{os.getpid()}.png"
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
            self.shot_avg_ms = self.shot_avg_ms * (1 - 1/w) + elapsed / w

            # 后端日志（让用户在黑窗口能看到工作状态）
            print(
                f"[shot] #{self.shot_total} {len(data)//1024}KB {elapsed:.0f}ms"
                f" (avg {self.shot_avg_ms:.0f}ms, cache_hits={self.shot_cache_hits})",
                flush=True,
            )

            return data, {
                "ms": round(elapsed, 1),
                "bytes": len(data),
                "cache": False,
                "shot_at": self._last_shot_at,
            }

    def ocr(self) -> dict:
        with self._lock:
            shot = Path(tempfile.gettempdir()) / f"_cst_ocr_{os.getpid()}.png"
            self.device.screenshot(shot)
            try:
                items = self._ocr.recognize(shot)
            finally:
                try:
                    shot.unlink()
                except OSError:
                    pass
            # 同步把屏幕尺寸更新到 _last_size，后续 tap_norm 用
            try:
                w, h = self.device.screen_size()
            except AdbError:
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
    """
    import glob
    import shutil
    tempdir = tempfile.gettempdir()
    # 老的单数文件（兼容历史）
    for pattern in ("_cst_live_*.png", "_cst_ocr_*.png", "_cst_upload.apk",
                    "_cst_marker.png", "_cst_marker_src_*.png", "_cst_marker_chk_*.png",
                    "_cst_skip_*.png"):
        for path in glob.glob(str(Path(tempdir) / pattern)):
            try:
                Path(path).unlink()
            except OSError:
                pass  # 文件可能正被占用
    # 跳过模板目录（通知权限等）
    if SKIP_TEMPLATE_DIR.exists():
        shutil.rmtree(SKIP_TEMPLATE_DIR, ignore_errors=True)
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
    """
    cx: float = Field(ge=0.0, le=1.0)
    cy: float = Field(ge=0.0, le=1.0)
    box_w: Optional[float] = Field(default=None, ge=0.0, le=1.0)  # 归一化宽（0~1）
    box_h: Optional[float] = Field(default=None, ge=0.0, le=1.0)  # 归一化高（0~1）


class ClearSkipReq(BaseModel):
    """清除跳过模板。id 为 None 时清空全部；指定 id 只删一条。"""
    id: Optional[int] = None


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
    """从磁盘项目加载模板到 Session（运行时内存），返回给前端的摘要。"""
    import shutil

    meta = _read_project_meta(pid)
    pdir = _project_dir(pid)

    with SESSION._lock:
        # 清当前跳过模板
        for t in SESSION._skip_templates:
            try:
                Path(t["path"]).unlink(missing_ok=True)
            except OSError:
                pass
        SESSION._skip_templates.clear()
        SKIP_TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)

        marker_src = pdir / "marker.png"
        if marker_src.is_file():
            shutil.copy2(marker_src, MARKER_TEMPLATE_PATH)
            SESSION._marker_template = MARKER_TEMPLATE_PATH
            m = meta.get("marker") or {}
            SESSION._marker_w = int(m.get("w") or 0)
            SESSION._marker_h = int(m.get("h") or 0)
            SESSION._marker_cx = float(m.get("cx") if m.get("cx") is not None else 0.5)
            SESSION._marker_cy = float(m.get("cy") if m.get("cy") is not None else 0.5)
            import cv2
            im = cv2.imread(str(MARKER_TEMPLATE_PATH))
            if im is not None:
                SESSION.set_marker_image(im)
                if SESSION._marker_w <= 0 or SESSION._marker_h <= 0:
                    SESSION._marker_h, SESSION._marker_w = im.shape[:2]
            else:
                SESSION.set_marker_image(None)
            SESSION.reset_marker_watch()
            marker_preview, marker_mime = _preview_b64_from_path(MARKER_TEMPLATE_PATH)
        else:
            SESSION._marker_template = None
            SESSION.set_marker_image(None)
            SESSION._marker_w = SESSION._marker_h = 0
            SESSION.reset_marker_watch()
            marker_preview, marker_mime = "", "image/jpeg"

        skip_out = []
        for s in meta.get("skips") or []:
            sid = int(s["id"])
            src = pdir / f"skip_{sid}.png"
            if not src.is_file():
                continue
            dst = SKIP_TEMPLATE_DIR / f"skip_{sid}.png"
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
                "last_tap_at": 0.0,
                "preview_base64": prev,
                "preview_mime": mime,
            }
            SESSION._skip_templates.append(entry)
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
        "marker_ready": SESSION._marker_template is not None,
        "marker_width": SESSION._marker_w,
        "marker_height": SESSION._marker_h,
        "marker_center_x": round(SESSION._marker_cx, 5) if SESSION._marker_template else None,
        "marker_center_y": round(SESSION._marker_cy, 5) if SESSION._marker_template else None,
        "marker_preview_base64": marker_preview,
        "marker_preview_mime": marker_mime,
        "skips": skip_out,
        "updated_at": meta.get("updated_at") or "",
    }


class TapReq(BaseModel):
    x: float
    y: float
    norm: bool = True


class KeyReq(BaseModel):
    code: int


class SwipeReq(BaseModel):
    x1: float
    y1: float
    x2: float
    y2: float
    norm: bool = True
    dur_ms: int = 200


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


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "adb": ADB_EXE, "version": "2.0"}


@app.get("/api/devices")
def list_devices() -> dict:
    """列出所有连接的设备（Android + iOS 合并返回）。

    Android 设备有 platform="android"，iOS 设备有 platform="ios"。
    前端选择设备时带上 platform，后端据此创建 AdbDevice 或 IosDevice。
    """
    try:
        devices = AdbDevice.devices()
        # 给 Android 设备补 platform 字段（兼容前端）
        for d in devices:
            d.setdefault("platform", "android")
    except AdbError as e:
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
def list_apps() -> dict:
    """列出当前设备上的第三方包名，供前端下拉选择。

    复用 Session.device（已选设备）；未选设备时返回空列表 + error。
    """
    try:
        with SESSION.device_op() as dev:
            pkgs = dev.list_packages()
        return {"apps": pkgs}
    except AdbError as e:
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
def screenshot(manual: int = 0) -> Response:
    """截屏 PNG。

    `?manual=1` 跳过缓存（用于「手动截图」按钮，确保拿到最新画面）。
    响应头带诊断字段，前端据此显示耗时/缓存命中/失败原因：
      - X-Shot-Ms: 本次截图耗时（毫秒）
      - X-Shot-Cache: 1=命中缓存 0=新截图
      - X-Shot-Bytes: 字节数
      - X-Shot-Total: 后端累计截图次数
    """
    try:
        data, meta = SESSION.screenshot_bytes(use_cache=manual == 0)
    except AdbError as e:
        SESSION.shot_errors += 1
        raise _err(400, str(e))
    headers = {
        "X-Shot-Ms": str(meta["ms"]),
        "X-Shot-Cache": "1" if meta["cache"] else "0",
        "X-Shot-Bytes": str(meta["bytes"]),
        "X-Shot-Total": str(SESSION.shot_total),
        "X-Shot-Avg-Ms": str(round(SESSION.shot_avg_ms, 1)),
        # 用 no-store 防止浏览器/中间代理缓存这个响应（很重要！否则永远同一张图）
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    return Response(content=data, media_type="image/png", headers=headers)


@app.get("/api/shot_stats")
def shot_stats() -> dict:
    """后端截图累计统计，让前端诊断"轮询到底有没有在工作"。"""
    return {
        "total": SESSION.shot_total,
        "cache_hits": SESSION.shot_cache_hits,
        "errors": SESSION.shot_errors,
        "last_ms": round(SESSION.shot_last_ms, 1),
        "avg_ms": round(SESSION.shot_avg_ms, 1),
        "device": SESSION._serial,
        "ready": SESSION._device is not None,
    }


@app.get("/api/ocr")
def ocr() -> dict:
    try:
        return SESSION.ocr()
    except AdbError as e:
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

        with SESSION._lock:
            # 1) 截当前屏（不复用缓存，确保是用户当前看到的画面）
            shot = Path(tempfile.gettempdir()) / f"_cst_marker_src_{os.getpid()}.png"
            try:
                SESSION.device.screenshot(shot)
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

            # 4) 存模板（覆盖式，单文件）
            cv2.imwrite(str(MARKER_TEMPLATE_PATH), template)

            # 5) 记录到 session（运行时 check_marker 用）
            actual_h, actual_w = template.shape[:2]
            SESSION._marker_template = MARKER_TEMPLATE_PATH
            SESSION._marker_w = actual_w
            SESSION._marker_h = actual_h
            # 中心归一化坐标（实际截取后的中心，可能与请求的 cx/cy 略有偏差——因越界裁剪）
            SESSION._marker_cx = (x1 + actual_w / 2) / w_px
            SESSION._marker_cy = (y1 + actual_h / 2) / h_px
            SESSION.set_marker_image(template)
            SESSION.reset_marker_watch()

            # 6) 返回预览（base64 JPG，体积小，前端 <img> 直接显示）
            ok, buf = cv2.imencode(".jpg", template, [cv2.IMWRITE_JPEG_QUALITY, 80])
            preview_b64 = base64.b64encode(buf).decode("ascii") if ok else ""

            return {
                "ok": True,
                "width": actual_w,
                "height": actual_h,
                "center_x": round(SESSION._marker_cx, 5),
                "center_y": round(SESSION._marker_cy, 5),
                "preview_base64": preview_b64,
                "preview_mime": "image/jpeg",
            }
    except AdbError as e:
        raise _err(400, str(e))


@app.get("/api/check_marker")
def check_marker() -> dict:
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
        import numpy as np

        with SESSION._lock:
            if SESSION._marker_template is None or not SESSION._marker_template.exists():
                return {"hit": False, "confidence": 0.0, "ms": 0.0, "error": "未设模板"}

            # 1) 截当前屏
            shot = Path(tempfile.gettempdir()) / f"_cst_marker_chk_{os.getpid()}.png"
            try:
                SESSION.device.screenshot(shot)
                scene = cv2.imread(str(shot))
            finally:
                try:
                    shot.unlink()
                except OSError:
                    pass
            if scene is None:
                return {"hit": False, "confidence": 0.0, "ms": 0.0, "error": "截图失败"}

            scene_h, scene_w = scene.shape[:2]
            template = cv2.imread(str(SESSION._marker_template))
            if template is None:
                return {"hit": False, "confidence": 0.0, "ms": 0.0, "error": "模板读失败"}
            th, tw = template.shape[:2]

            # 2) 算搜索区域：模板中心 ± padding（裁剪到画面内）
            cx_px = int(SESSION._marker_cx * scene_w)
            cy_px = int(SESSION._marker_cy * scene_h)
            pad = MARKER_SEARCH_PADDING
            sx1 = max(0, cx_px - tw // 2 - pad)
            sy1 = max(0, cy_px - th // 2 - pad)
            sx2 = min(scene_w, cx_px + tw // 2 + pad)
            sy2 = min(scene_h, cy_px + th // 2 + pad)

            # 搜索区域必须不小于模板尺寸，否则 matchTemplate 报错
            if sx2 - sx1 < tw or sy2 - sy1 < th:
                # padding 不够时退化成全图搜（保证能跑，只是慢一点）
                sx1, sy1, sx2, sy2 = 0, 0, scene_w, scene_h

            roi = scene[sy1:sy2, sx1:sx2]

            # 3) matchTemplate（TM_CCOEFF_NORMED：归一化相关系数）
            res = cv2.matchTemplate(roi, template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(res)

            elapsed_ms = (time.perf_counter() - t0) * 1000
            SESSION.marker_check_total += 1
            SESSION.marker_check_last_ms = elapsed_ms
            SESSION.marker_check_last_conf = float(max_val)

            return {
                "hit": bool(max_val >= MARKER_MATCH_THRESHOLD),
                "confidence": round(float(max_val), 4),
                "threshold": MARKER_MATCH_THRESHOLD,
                "ms": round(elapsed_ms, 1),
            }
    except AdbError as e:
        return {"hit": False, "confidence": 0.0, "ms": 0.0, "error": str(e)}
    except Exception as e:
        # cv2/numpy 出错不抛 500，让前端能继续轮询（按未命中处理）
        return {"hit": False, "confidence": 0.0, "ms": 0.0, "error": f"{type(e).__name__}: {e}"}


@app.get("/api/marker_status")
def marker_status() -> dict:
    """查询启动成功模板是否已设（供前端刷新后恢复 markerTemplateReady，无需再截屏匹配）。"""
    ready = (
        SESSION._marker_template is not None
        and Path(SESSION._marker_template).exists()
    )
    return {
        "ready": ready,
        "width": SESSION._marker_w if ready else 0,
        "height": SESSION._marker_h if ready else 0,
        "center_x": round(SESSION._marker_cx, 5) if ready else None,
        "center_y": round(SESSION._marker_cy, 5) if ready else None,
    }


@app.post("/api/preflight_auto")
def preflight_auto(req: ReinstallReq) -> dict:
    """自动循环开跑前自检：设备、APK 文件、包名非空。不占设备锁、不做 adb 卸装。"""
    errors: list[str] = []
    if SESSION._device is None:
        errors.append("未选择设备")
    if not (req.package or "").strip():
        errors.append("包名为空")
    apk = Path(req.apk_path or "")
    if not apk.is_file():
        errors.append(f"APK 不存在：{req.apk_path}（请重新上传）")
    marker_ok = (
        SESSION._marker_template is not None
        and Path(SESSION._marker_template).exists()
    )
    if not marker_ok:
        errors.append("未设启动元素模板")
    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "apk_name": apk.name if apk.is_file() else "",
        "apk_size_mb": round(apk.stat().st_size / 1048576, 1) if apk.is_file() else 0,
        "marker_ready": marker_ok,
        "device": SESSION._serial,
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
    """手动保存：把当前 Session 模板 + 请求里的表单配置写入 projects/<id>/。

    不复制 APK——只记 apk_hint 文件名提醒用户下次再传。
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

        with SESSION._lock:
            marker_meta = None
            marker_dst = pdir / "marker.png"
            if (
                SESSION._marker_template is not None
                and Path(SESSION._marker_template).is_file()
            ):
                shutil.copy2(SESSION._marker_template, marker_dst)
                marker_meta = {
                    "cx": SESSION._marker_cx,
                    "cy": SESSION._marker_cy,
                    "w": SESSION._marker_w,
                    "h": SESSION._marker_h,
                }
            else:
                marker_dst.unlink(missing_ok=True)

            for old_skip in pdir.glob("skip_*.png"):
                try:
                    old_skip.unlink()
                except OSError:
                    pass
            skips_meta = []
            for t in SESSION._skip_templates:
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


def _match_template_in_scene(scene, template, cx: float, cy: float, pad: int) -> float:
    """在 scene 上以 (cx,cy) 为中心、pad 为边距做 matchTemplate，返回最高置信度。"""
    import cv2

    scene_h, scene_w = scene.shape[:2]
    th, tw = template.shape[:2]
    cx_px = int(cx * scene_w)
    cy_px = int(cy * scene_h)
    sx1 = max(0, cx_px - tw // 2 - pad)
    sy1 = max(0, cy_px - th // 2 - pad)
    sx2 = min(scene_w, cx_px + tw // 2 + pad)
    sy2 = min(scene_h, cy_px + th // 2 + pad)
    if sx2 - sx1 < tw or sy2 - sy1 < th:
        sx1, sy1, sx2, sy2 = 0, 0, scene_w, scene_h
    roi = scene[sy1:sy2, sx1:sx2]
    res = cv2.matchTemplate(roi, template, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, _ = cv2.minMaxLoc(res)
    return float(max_val)


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

        with SESSION._lock:
            if len(SESSION._skip_templates) >= SKIP_TEMPLATE_MAX:
                raise AdbError(f"跳过模板已满（最多 {SKIP_TEMPLATE_MAX} 个），请先清除再添加")

            shot = Path(tempfile.gettempdir()) / f"_cst_skip_src_{os.getpid()}.png"
            try:
                SESSION.device.screenshot(shot)
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

            SKIP_TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
            # id 用递增：已有 max+1，避免删中间后撞名
            next_id = (max((t["id"] for t in SESSION._skip_templates), default=0) + 1)
            path = SKIP_TEMPLATE_DIR / f"skip_{next_id}.png"
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
                "last_tap_at": 0.0,
                "preview_base64": preview_b64,
                "preview_mime": "image/jpeg",
            }
            SESSION._skip_templates.append(entry)
            print(f"[skip] 添加模板 #{next_id} {actual_w}x{actual_h} @({cx_n:.3f},{cy_n:.3f})", flush=True)

            return {
                "ok": True,
                "id": next_id,
                "width": actual_w,
                "height": actual_h,
                "center_x": round(cx_n, 5),
                "center_y": round(cy_n, 5),
                "count": len(SESSION._skip_templates),
                "preview_base64": preview_b64,
                "preview_mime": "image/jpeg",
            }
    except AdbError as e:
        raise _err(400, str(e))


@app.get("/api/skip_templates")
def list_skip_templates() -> dict:
    """列出当前跳过模板（含预览），供前端刷新列表。"""
    items = []
    for t in SESSION._skip_templates:
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
    with SESSION._lock:
        if req.id is None:
            for t in SESSION._skip_templates:
                try:
                    Path(t["path"]).unlink(missing_ok=True)
                except OSError:
                    pass
            SESSION._skip_templates.clear()
            return {"ok": True, "count": 0}
        kept = []
        removed = False
        for t in SESSION._skip_templates:
            if t["id"] == req.id:
                try:
                    Path(t["path"]).unlink(missing_ok=True)
                except OSError:
                    pass
                removed = True
            else:
                kept.append(t)
        SESSION._skip_templates = kept
        if not removed:
            raise _err(400, f"找不到跳过模板 id={req.id}")
        return {"ok": True, "count": len(kept)}


@app.get("/api/check_auto")
def check_auto(
    check_skips: bool = Query(
        True,
        description="是否匹配跳过弹窗模板。二次冷启动应传 false（首次装后不会再弹允许类弹窗）",
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

        with SESSION._lock:
            # 1) 截当前屏（热路径：raw|gzip → BGR，免落盘）
            t_shot0 = time.perf_counter()
            try:
                scene, shot_via = SESSION.device.screenshot_bgr()
            except AdbError as e:
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
                for t in SESSION._skip_templates:
                    if t["id"] in SESSION._skip_fired_ids:
                        continue
                    template = t.get("img")
                    if template is None:
                        path = Path(t["path"])
                        if path.exists():
                            template = cv2.imread(str(path))
                            t["img"] = template
                    if template is None:
                        continue
                    conf = _match_template_in_scene(
                        scene, template, t["cx"], t["cy"], SKIP_SEARCH_PADDING
                    )
                    if conf < SKIP_MATCH_THRESHOLD:
                        continue
                    if now - float(t.get("last_tap_at") or 0) < SKIP_TAP_COOLDOWN_S:
                        continue
                    SESSION.device.tap_norm(t["cx"], t["cy"])
                    t["last_tap_at"] = now
                    SESSION._skip_fired_ids.add(t["id"])
                    SESSION._marker_hit_streak = 0
                    # 弹窗屏 ≠ 启动成功态：视为已见过「低于成功阈值」，避免点完后
                    # 首页已就绪时上升沿永远充不上能（R5 死锁：conf 一直 100% 等上升沿）
                    SESSION._marker_seen_below = True
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

            # 3) 启动成功模板（内存缓存 + 连续确认 + 上升沿）
            template = SESSION.ensure_marker_image()
            if template is None:
                match_ms = (time.perf_counter() - t_match0) * 1000
                elapsed_ms = (time.perf_counter() - t0) * 1000
                return {
                    "skipped": False, "hit": False, "confidence": 0.0,
                    "check_skips": bool(check_skips),
                    "ms": round(elapsed_ms, 1), "shot_ms": round(shot_ms, 1),
                    "match_ms": round(match_ms, 1), "shot_via": shot_via,
                    "error": "未设模板",
                }

            conf = _match_template_in_scene(
                scene, template,
                SESSION._marker_cx, SESSION._marker_cy,
                MARKER_SEARCH_PADDING,
            )
            match_ms = (time.perf_counter() - t_match0) * 1000
            elapsed_ms = (time.perf_counter() - t0) * 1000

            above = conf >= MARKER_MATCH_THRESHOLD
            if not above:
                SESSION._marker_seen_below = True
                SESSION._marker_hit_streak = 0
            else:
                if MARKER_REQUIRE_RISING_EDGE and not SESSION._marker_seen_below:
                    # 开跑时桌面/残留已过高：等掉下去再上来，避免误停
                    SESSION._marker_hit_streak = 0
                else:
                    SESSION._marker_hit_streak += 1

            hit = (
                above
                and SESSION._marker_hit_streak >= MARKER_CONFIRM_FRAMES
                and (not MARKER_REQUIRE_RISING_EDGE or SESSION._marker_seen_below)
            )

            SESSION.marker_check_total += 1
            SESSION.marker_check_last_ms = elapsed_ms
            SESSION.marker_check_last_conf = conf
            return {
                "skipped": False,
                "hit": bool(hit),
                "confidence": round(conf, 4),
                "threshold": MARKER_MATCH_THRESHOLD,
                "above": bool(above),
                "streak": SESSION._marker_hit_streak,
                "confirm_need": MARKER_CONFIRM_FRAMES,
                "rising_ready": bool(SESSION._marker_seen_below) or (not MARKER_REQUIRE_RISING_EDGE),
                "check_skips": bool(check_skips),
                "ms": round(elapsed_ms, 1),
                "shot_ms": round(shot_ms, 1),
                "match_ms": round(match_ms, 1),
                "shot_via": shot_via,
            }
    except AdbError as e:
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
def marker_watch_reset() -> dict:
    """测速开始前清零连续确认/上升沿（cold_start 成功时也会自动清）。"""
    SESSION.reset_marker_watch()
    return {
        "ok": True,
        "confirm_need": MARKER_CONFIRM_FRAMES,
        "rising_edge": MARKER_REQUIRE_RISING_EDGE,
        "threshold": MARKER_MATCH_THRESHOLD,
    }


@app.post("/api/tap")
def tap(req: TapReq) -> dict:
    try:
        with SESSION.device_op() as dev:
            if req.norm:
                dev.tap_norm(req.x, req.y)
            else:
                dev.tap_pixel(int(req.x), int(req.y))
    except AdbError as e:
        raise _err(400, str(e))
    return {"ok": True}


@app.post("/api/key")
def key(req: KeyReq) -> dict:
    try:
        with SESSION.device_op() as dev:
            dev.keyevent(req.code)
    except AdbError as e:
        raise _err(400, str(e))
    return {"ok": True}


@app.post("/api/swipe")
def swipe(req: SwipeReq) -> dict:
    try:
        with SESSION.device_op() as dev:
            if req.norm:
                w, h = dev.screen_size()
                x1, y1 = int(req.x1 * w), int(req.y1 * h)
                x2, y2 = int(req.x2 * w), int(req.y2 * h)
            else:
                x1, y1, x2, y2 = int(req.x1), int(req.y1), int(req.x2), int(req.y2)
            dev.swipe(x1, y1, x2, y2, req.dur_ms)
    except AdbError as e:
        raise _err(400, str(e))
    return {"ok": True}


@app.post("/api/launch_pkg")
def launch_pkg(req: LaunchPkgReq) -> dict:
    if req.serial and req.serial != SESSION._serial:
        SESSION.select(req.serial)
    try:
        with SESSION.device_op() as dev:
            dev.launch_pkg(req.package)
    except AdbError as e:
        raise _err(400, str(e))
    return {"ok": True}


@app.post("/api/force_stop")
def force_stop(req: ForceStopReq) -> dict:
    if req.serial and req.serial != SESSION._serial:
        SESSION.select(req.serial)
    try:
        with SESSION.device_op() as dev:
            dev.force_stop(req.package)
    except AdbError as e:
        raise _err(400, str(e))
    return {"ok": True}


@app.post("/api/kill_all")
def kill_all(req: Optional[KillAllReq] = None) -> dict:
    """测速间隔清后台：am kill-all（方案 A，温和）。"""
    body = req or KillAllReq()
    if body.serial and body.serial != SESSION._serial:
        SESSION.select(body.serial)
    try:
        with SESSION.device_op() as dev:
            out = dev.kill_all()
    except AdbError as e:
        raise _err(400, str(e))
    return {"ok": True, "log": out or "(ok)"}


@app.post("/api/reinstall")
def reinstall(req: ReinstallReq) -> dict:
    if req.serial and req.serial != SESSION._serial:
        SESSION.select(req.serial)
    # 锁外先查 APK：否则直播截图占着 device_op 锁时，连「文件不存在」都要干等十几秒，
    # 前端只看到「卸装重装」一行日志，像没执行。
    apk = Path(req.apk_path)
    if not apk.is_file():
        return {
            "ok": False,
            "error": f"APK 文件不存在：{req.apk_path}（若刚重启过后端，请重新上传 APK）",
            "log": [],
        }
    print(f"[reinstall] 开始 pkg={req.package} apk={apk.name} size={apk.stat().st_size}", flush=True)
    try:
        with SESSION.device_op() as dev:
            print("[reinstall] 已拿到设备锁，执行 uninstall…", flush=True)
            log = dev.reinstall(req.package, req.apk_path)
    except AdbError as e:
        print(f"[reinstall] 失败：{e}", flush=True)
        return {"ok": False, "error": str(e), "log": []}
    print("[reinstall] 完成", flush=True)
    return {"ok": True, "log": log}


@app.post("/api/cold_start")
def cold_start(req: ColdStartReq) -> dict:
    """冷启动编排：force_stop → tap/launch，返回 start_wall（供诊断）。

    计时由前端完成（v1 单一 performance.now() 方案，详见 ColdStartReq docstring）。
    本端点不自动回主页 —— 用户需确保启动前已在桌面。独立的回主页能力在
    前端"回主页"按钮 + /api/key 端点，与启动流程解耦。
    全程在 device_op() 锁下执行（审核高3：force_stop + tap 必须串行，避免并发竞争）。
    """
    if req.serial and req.serial != SESSION._serial:
        SESSION.select(req.serial)

    try:
        with SESSION.device_op() as dev:
            # 1) 先把上一次的同包进程杀掉，确保冷启动
            if req.package:
                dev.force_stop(req.package)

            # 2) 预热 screen_size（如果还没缓存），避免它计入 tap_norm 的执行
            if dev._last_size is None:
                dev.screen_size()

            # 3) 在 tap/monkey 命令实际发出前一刻记录 wall 时间（仅供诊断/将来用）
            start_wall = time.time()

            if req.mode == "tap":
                if req.x is None or req.y is None:
                    raise _err(400, "tap 模式需要 x, y 坐标")
                dev.tap_norm(req.x, req.y)
            elif req.mode == "pkg":
                if not req.package:
                    raise _err(400, "pkg 模式需要 package")
                dev.launch_pkg(req.package)
            else:
                raise _err(400, f"未知 mode：{req.mode}")

        # 新一次启动观察：清零 streak / 已点跳过；
        # after_force_stop=True：上面若杀过包（或本就无包可杀），视为已离开成功页，种上升沿
        SESSION.reset_marker_watch(after_force_stop=True)

        return {
            "ok": True,
            "start_wall": start_wall,   # unix epoch 秒，仅供诊断；前端计时用 performance.now() 不消费此字段
            "marker_confirm_frames": MARKER_CONFIRM_FRAMES,
            "marker_rising_edge": MARKER_REQUIRE_RISING_EDGE,
            "marker_threshold": MARKER_MATCH_THRESHOLD,
            "marker_rising_seeded": True,  # 诊断：本趟上升沿已因 force_stop 预置
        }
    except AdbError as e:
        raise _err(400, str(e))


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
