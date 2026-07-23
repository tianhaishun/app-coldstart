"""App 冷启测速 —— FastAPI 后端。

参考 GameAuto（D:\\work\\GameAuto）的设计哲学：
  - 截图走 `adb exec-out screencap -p`（直出 PNG bytes，比 screencap+pull 快）
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

import base64
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# uvicorn 加载本模块时把根目录加进 sys.path，便于 from server import ...
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 内置 adb（同目录 adb\\adb.exe），找不到再回退 PATH
_BUNDLED_ADB = ROOT / "adb" / "adb.exe"
ADB_EXE = str(_BUNDLED_ADB) if _BUNDLED_ADB.exists() else "adb"

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
        """优先 exec-out screencap -p 直出 PNG；不支持则回退 screencap + pull。"""
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
        """包名启动（绕过 Launcher 触摸）。"""
        self.run(
            ["shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1"],
            check=False, timeout=10.0,
        )

    def force_stop(self, pkg: str) -> None:
        self.run(["shell", "am", "force-stop", pkg], check=False, timeout=5.0)

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
        """
        if not Path(apk_path).exists():
            raise AdbError(f"APK 文件不存在：{apk_path}")
        log: list[str] = []
        out = self.run(["uninstall", pkg], check=False, timeout=60.0)
        log.append(f"uninstall: {out}")
        if "Failure" in out:
            # 兜底：部分设备（如装为系统用户）需要 --user 0 才能卸
            out2 = self.run(["shell", "pm", "uninstall", "--user", "0", pkg], check=False, timeout=30.0)
            log.append(f"pm uninstall --user 0: {out2}")
        out3 = self.run(["install", "-r", apk_path], check=False, timeout=180.0)
        log.append(f"install: {out3}")
        if "Success" not in out3:
            raise AdbError(f"安装失败：{out3}")
        return log


# ── 会话（当前选中设备 + OCR 引擎）──────────────────────────────────────


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
        self._marker_w: int = 0                        # 模板像素宽
        self._marker_h: int = 0                        # 模板像素高
        self._marker_cx: float = 0.5                   # 模板中心归一化坐标（运行时搜索用）
        self._marker_cy: float = 0.5
        self.marker_check_total = 0      # check_marker 累计调用次数（诊断用）
        self.marker_check_last_ms = 0.0  # 上次 check 耗时
        self.marker_check_last_conf = 0.0  # 上次置信度

    def select(self, serial: Optional[str]) -> dict:
        with self._lock:
            self._serial = serial
            self._device = AdbDevice(serial)
            self._last_shot = None
            return {"serial": serial, "ready": True}

    def current(self) -> dict:
        return {"serial": self._serial, "ready": self._device is not None}

    @property
    def device(self) -> AdbDevice:
        if self._device is None:
            raise AdbError("未选择设备，请先在左上角连接设备")
        return self._device

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

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

app = FastAPI(title="App Cold Start Profiler", version="2.0")


@app.on_event("startup")
def cleanup_stale_temp_files() -> None:
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
                    "_cst_marker.png", "_cst_marker_src_*.png", "_cst_marker_chk_*.png"):
        for path in glob.glob(str(Path(tempdir) / pattern)):
            try:
                Path(path).unlink()
            except OSError:
                pass  # 文件可能正被占用
    # 新的 APK 上传目录：清空后重建空目录
    if APK_UPLOAD_DIR.exists():
        shutil.rmtree(APK_UPLOAD_DIR, ignore_errors=True)
    APK_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    print("[startup] 清理了 tempdir 下的旧 _cst_* 临时文件 + _cst_uploads/ + _cst_marker.png", flush=True)


class DeviceSelectReq(BaseModel):
    serial: Optional[str] = None


class SetMarkerReq(BaseModel):
    """设定启动成功模板：以当前屏 (cx, cy) 为中心截小区域存为模板。

    cx/cy 是归一化坐标（0~1），来自前端点画面或点 OCR 框。
    box_w/box_h 可选：若来自 OCR 框就用框的归一化尺寸换算像素；否则用默认。
    """
    cx: float
    cy: float
    box_w: Optional[float] = None  # 归一化宽（0~1）
    box_h: Optional[float] = None  # 归一化高（0~1）


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


class ReinstallReq(BaseModel):
    package: str
    apk_path: str
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
    mode: str = "tap"  # "tap" 或 "pkg"
    x: Optional[float] = None
    y: Optional[float] = None
    package: Optional[str] = None
    serial: Optional[str] = None


def _err(status: int, msg: str) -> HTTPException:
    return HTTPException(status_code=status, detail=msg)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "adb": ADB_EXE, "version": "2.0"}


@app.get("/api/devices")
def list_devices() -> dict:
    try:
        return {"devices": AdbDevice.devices()}
    except AdbError as e:
        return {"devices": [], "error": str(e)}


@app.post("/api/device/select")
def select_device(req: DeviceSelectReq) -> dict:
    return SESSION.select(req.serial)


@app.get("/api/apps")
def list_apps() -> dict:
    """列出当前设备上的第三方包名，供前端下拉选择。

    复用 Session.device（已选设备）；未选设备时返回空列表 + error。
    """
    try:
        pkgs = SESSION.device.list_packages()
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


@app.post("/api/tap")
def tap(req: TapReq) -> dict:
    try:
        if req.norm:
            SESSION.device.tap_norm(req.x, req.y)
        else:
            SESSION.device.tap_pixel(int(req.x), int(req.y))
    except AdbError as e:
        raise _err(400, str(e))
    return {"ok": True}


@app.post("/api/key")
def key(req: KeyReq) -> dict:
    try:
        SESSION.device.keyevent(req.code)
    except AdbError as e:
        raise _err(400, str(e))
    return {"ok": True}


@app.post("/api/swipe")
def swipe(req: SwipeReq) -> dict:
    try:
        if req.norm:
            w, h = SESSION.device.screen_size()
            x1, y1 = int(req.x1 * w), int(req.y1 * h)
            x2, y2 = int(req.x2 * w), int(req.y2 * h)
        else:
            x1, y1, x2, y2 = int(req.x1), int(req.y1), int(req.x2), int(req.y2)
        SESSION.device.swipe(x1, y1, x2, y2, req.dur_ms)
    except AdbError as e:
        raise _err(400, str(e))
    return {"ok": True}


@app.post("/api/launch_pkg")
def launch_pkg(req: LaunchPkgReq) -> dict:
    if req.serial and req.serial != SESSION._serial:
        SESSION.select(req.serial)
    try:
        SESSION.device.launch_pkg(req.package)
    except AdbError as e:
        raise _err(400, str(e))
    return {"ok": True}


@app.post("/api/force_stop")
def force_stop(req: ForceStopReq) -> dict:
    if req.serial and req.serial != SESSION._serial:
        SESSION.select(req.serial)
    try:
        SESSION.device.force_stop(req.package)
    except AdbError as e:
        raise _err(400, str(e))
    return {"ok": True}


@app.post("/api/reinstall")
def reinstall(req: ReinstallReq) -> dict:
    if req.serial and req.serial != SESSION._serial:
        SESSION.select(req.serial)
    try:
        log = SESSION.device.reinstall(req.package, req.apk_path)
    except AdbError as e:
        return {"ok": False, "error": str(e), "log": []}
    return {"ok": True, "log": log}


@app.post("/api/cold_start")
def cold_start(req: ColdStartReq) -> dict:
    """冷启动编排：force_stop → tap/launch，返回 start_wall（供诊断）。

    计时由前端完成（v1 单一 performance.now() 方案，详见 ColdStartReq docstring）。
    本端点不自动回主页 —— 用户需确保启动前已在桌面。独立的回主页能力在
    前端"回主页"按钮 + /api/key 端点，与启动流程解耦。
    """
    if req.serial and req.serial != SESSION._serial:
        SESSION.select(req.serial)

    try:
        # 1) 先把上一次的同包进程杀掉，确保冷启动
        if req.package:
            SESSION.device.force_stop(req.package)

        # 2) 预热 screen_size（如果还没缓存），避免它计入 tap_norm 的执行
        if SESSION.device._last_size is None:
            SESSION.device.screen_size()

        # 3) 在 tap/monkey 命令实际发出前一刻记录 wall 时间（仅供诊断/将来用）
        start_wall = time.time()

        if req.mode == "tap":
            if req.x is None or req.y is None:
                raise _err(400, "tap 模式需要 x, y 坐标")
            SESSION.device.tap_norm(req.x, req.y)
        elif req.mode == "pkg":
            if not req.package:
                raise _err(400, "pkg 模式需要 package")
            SESSION.device.launch_pkg(req.package)
        else:
            raise _err(400, f"未知 mode：{req.mode}")

        return {
            "ok": True,
            "start_wall": start_wall,   # unix epoch 秒，仅供诊断；前端计时用 performance.now() 不消费此字段
        }
    except AdbError as e:
        raise _err(400, str(e))


# ── 静态资源（前端单文件）─────────────────────────────────────────────

STATIC_DIR = ROOT / "static"


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


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
        return FileResponse(STATIC_DIR / "index.html")
    if candidate.is_file():
        return FileResponse(candidate)
    return FileResponse(STATIC_DIR / "index.html")
