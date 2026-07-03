"""冷启动计时器 v2 —— FastAPI 后端。

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
        """卸载重装，返回日志行。失败抛 AdbError。"""
        if not Path(apk_path).exists():
            raise AdbError(f"APK 文件不存在：{apk_path}")
        log: list[str] = []
        out = self.run(["uninstall", pkg], check=False, timeout=60.0)
        log.append(f"uninstall: {out}")
        if "Failure" in out:
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

app = FastAPI(title="Cold Start Timer v2", version="2.0")


@app.on_event("startup")
def cleanup_stale_temp_files() -> None:
    """启动时清理 tempdir 下的旧 _cst_* 文件（避免长时间运行后堆积）。

    72 小时连续运行 + 多次重启会累积一堆 _cst_live_{oldpid}.png 和 _cst_upload.apk
    （232MB）。这里在每次启动时清理掉所有 _cst_* 文件——本进程的会在使用中，
    但启动瞬间还没创建，所以清的是历史残留。
    """
    import glob
    tempdir = tempfile.gettempdir()
    for pattern in ("_cst_live_*.png", "_cst_ocr_*.png", "_cst_upload.apk"):
        for path in glob.glob(str(Path(tempdir) / pattern)):
            try:
                Path(path).unlink()
            except OSError:
                pass  # 文件可能正被占用
    print("[startup] 清理了 tempdir 下的旧 _cst_* 临时文件", flush=True)


class DeviceSelectReq(BaseModel):
    serial: Optional[str] = None


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
    """接收前端拖拽/选择上传的 APK，存到固定临时路径（覆盖式），返回该路径。

    设计：浏览器安全限制拿不到用户本地完整路径，所以走"上传到后端临时目录"
    方案。前端拿到返回的 temp 路径后填入 apkPath 输入框，reinstall 继续用
    apkPath 字段 —— 上传和重装两条链路解耦，reinstall 零改动。
    每次上传覆盖同一个文件，不堆积。
    """
    if not file.filename or not file.filename.lower().endswith(".apk"):
        raise _err(400, "请上传 .apk 文件")
    target = Path(tempfile.gettempdir()) / "_cst_upload.apk"
    with target.open("wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)
    return {"ok": True, "path": str(target), "size_mb": round(target.stat().st_size / 1048576, 1)}


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
    """其它路径都尝试从 static/ 取，找不到回 index.html（SPA 兜底）。"""
    candidate = STATIC_DIR / path
    if candidate.is_file():
        return FileResponse(candidate)
    return FileResponse(STATIC_DIR / "index.html")
