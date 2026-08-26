"""Cross-platform ADB helpers and high-precision screen polling.

The module intentionally keeps ADB process management, APK metadata parsing,
screen capture, matching, and timing independent from FastAPI.  ``AdbHelper``
can therefore be used by the desktop backend, tests, or a standalone probe on
Windows and macOS.
"""

from __future__ import annotations

import gzip
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


# platform-specific executable names are resolved at runtime; no shell is used.


class AdbHelperError(RuntimeError):
    """ADB, APK, capture, or polling failure."""


@dataclass(frozen=True)
class DeviceInfo:
    serial: str
    state: str
    model: str = ""


# adb install 失败错误码中文翻译（从 server.py AdbDevice 迁移，对齐 AGENTS 教训七）。
# adb 输出形如 "Failure [INSTALL_FAILED_OLDER_SDK]"，匹配后附加中文解释。
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


def install_error_cn(output: str) -> str | None:
    """从 adb install 输出匹配 INSTALL_FAILED_* 错误码，返回中文解释（无匹配返回 None）。"""
    for code, cn in _INSTALL_ERROR_CN.items():
        if code in output:
            return f"{code}：{cn}"
    return None


@dataclass(frozen=True)
class ApkInfo:
    path: str
    package: str
    version_name: str = ""
    version_code: str = ""
    label: str = ""


@dataclass(frozen=True)
class MatchResult:
    hit: bool
    confidence: float
    elapsed_ms: float
    roi_width: int
    roi_height: int


@dataclass(frozen=True)
class ScreenSample:
    sequence: int
    started_at: float
    captured_at: float
    capture_ms: float
    frame: Any
    match: MatchResult | None = None
    error: str | None = None


class PerfTimer:
    """Monotonic high-resolution timer backed exclusively by perf_counter."""

    def __init__(self) -> None:
        self._started_at: float | None = None
        self._stopped_at: float | None = None

    def start(self) -> "PerfTimer":
        self._started_at = time.perf_counter()
        self._stopped_at = None
        return self

    def stop(self) -> float:
        if self._started_at is None:
            raise AdbHelperError("计时器尚未启动")
        self._stopped_at = time.perf_counter()
        return self.elapsed_s

    @property
    def running(self) -> bool:
        return self._started_at is not None and self._stopped_at is None

    @property
    def elapsed_s(self) -> float:
        if self._started_at is None:
            return 0.0
        end = self._stopped_at if self._stopped_at is not None else time.perf_counter()
        return max(0.0, end - self._started_at)

    @property
    def elapsed_ms(self) -> float:
        return self.elapsed_s * 1000.0


class AdbHelper:
    """Common cross-platform ADB operations for one selected device."""

    def __init__(
        self,
        serial: str | None = None,
        *,
        adb_path: str | Path | None = None,
        project_root: str | Path | None = None,
    ) -> None:
        self.serial = serial
        self.project_root = Path(project_root) if project_root else Path(__file__).resolve().parent
        self.adb_path = self.resolve_adb_path(adb_path, self.project_root)
        self._last_size: tuple[int, int] | None = None

    @staticmethod
    def resolve_adb_path(
        value: str | Path | None = None,
        project_root: str | Path | None = None,
    ) -> str:
        """Resolve adb across Windows/macOS, env overrides, and PATH."""

        root = Path(project_root) if project_root else Path(__file__).resolve().parent
        candidates: list[str] = []
        if value:
            candidates.append(str(value))
        if os.environ.get("ADB"):
            candidates.append(os.environ["ADB"])
        executable = "adb.exe" if os.name == "nt" else "adb"
        candidates.append(str(root / "adb" / executable))
        candidates.append(executable)
        for candidate in candidates:
            path = Path(candidate)
            if path.is_file():
                return str(path.resolve())
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
        raise AdbHelperError(f"找不到 adb：{value or '项目目录/PATH'}")

    def _args(self, args: list[str], *, include_serial: bool = True) -> list[str]:
        return [self.adb_path] + (["-s", self.serial] if include_serial and self.serial else []) + list(args)

    def run(
        self,
        args: list[str],
        *,
        timeout: float = 30.0,
        check: bool = True,
        include_serial: bool = True,
    ) -> str:
        cmd = self._args(args, include_serial=include_serial)
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise AdbHelperError(f"adb 命令超时（{timeout:g}s）：{' '.join(args)}") from exc
        except FileNotFoundError as exc:
            raise AdbHelperError(f"找不到 adb：{self.adb_path}") from exc
        stdout = result.stdout.decode("utf-8", "replace")
        stderr = result.stderr.decode("utf-8", "replace")
        if check and result.returncode != 0:
            detail = (stderr or stdout).strip().splitlines()
            raise AdbHelperError(detail[-1] if detail else f"adb exit={result.returncode}")
        return stdout.strip()

    def run_bytes(
        self,
        args: list[str],
        *,
        timeout: float = 30.0,
        include_serial: bool = True,
    ) -> bytes:
        try:
            result = subprocess.run(
                self._args(args, include_serial=include_serial),
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AdbHelperError(f"adb 命令超时（{timeout:g}s）：{' '.join(args)}") from exc
        except FileNotFoundError as exc:
            raise AdbHelperError(f"找不到 adb：{self.adb_path}") from exc
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
            raise AdbHelperError(detail[-1] if detail else f"adb exit={result.returncode}")
        return result.stdout

    def get_state(self) -> str:
        return self.run(["get-state"], timeout=5.0)

    def screen_size(self) -> tuple[int, int]:
        output = self.run(["shell", "wm", "size"], timeout=5.0)
        match = re.search(r"(\d+)x(\d+)", output)
        if not match:
            raise AdbHelperError(f"解析屏幕尺寸失败：{output!r}")
        self._last_size = (int(match.group(1)), int(match.group(2)))
        return self._last_size

    def screenshot_bgr(self) -> tuple[Any, str]:
        """Capture BGR, preferring raw gzip and falling back to PNG."""

        import cv2
        import numpy as np

        try:
            compressed = self.run_bytes(
                ["exec-out", "sh", "-c", "screencap | gzip -1 -c"],
                timeout=15.0,
            )
            raw = gzip.decompress(compressed)
            return _raw_screencap_to_bgr(raw), "raw_gzip"
        except Exception as raw_error:
            try:
                data = self.run_bytes(["exec-out", "screencap", "-p"], timeout=15.0)
                image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
                if image is None:
                    raise AdbHelperError("PNG 解码失败")
                self._last_size = (int(image.shape[1]), int(image.shape[0]))
                return image, "png"
            except Exception as png_error:
                raise AdbHelperError(f"截图失败（raw: {raw_error}; png: {png_error}）") from png_error

    def screenshot(self, target: str | Path) -> Path:
        """截屏保存 PNG 到 target。优先 exec-out 直出；失败回退「设备端落盘 + pull」。

        与 server.py 老 AdbDevice 行为对齐：exec-out 直出不支持/超时的设备走兜底路径。
        """
        path = Path(target)
        try:
            data = self.run_bytes(["exec-out", "screencap", "-p"], timeout=15.0)
            if len(data) < 1024 or not data.startswith(b"\x89PNG"):
                raise AdbHelperError("exec-out screencap 返回的不是有效 PNG")
            path.write_bytes(data)
            return path
        except AdbHelperError:
            # 回退：手机端落盘 + pull
            device_path = "/sdcard/_cst_shot.png"
            self.run(["shell", "screencap", "-p", device_path], timeout=15.0)
            self.run(["pull", device_path, str(path)], timeout=30.0)
            try:
                self.run(["shell", "rm", "-f", device_path], check=False, timeout=5.0)
            except AdbHelperError:
                pass
            if not path.exists():
                raise AdbHelperError("pull 后截图不存在")
            return path

    def tap_pixel(self, x: int, y: int) -> None:
        self.run(["shell", "input", "tap", str(int(x)), str(int(y))], timeout=10.0)

    def tap_norm(self, cx: float, cy: float) -> None:
        width, height = self._last_size or self.screen_size()
        self.tap_pixel(round(cx * width), round(cy * height))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, dur_ms: int = 200) -> None:
        self.run(
            [
                "shell",
                "input",
                "swipe",
                str(int(x1)),
                str(int(y1)),
                str(int(x2)),
                str(int(y2)),
                str(int(dur_ms)),
            ],
            timeout=15.0,
        )

    def keyevent(self, code: int) -> None:
        self.run(["shell", "input", "keyevent", str(int(code))], timeout=5.0)

    def force_stop(self, package: str) -> None:
        self.run(["shell", "am", "force-stop", package], check=False, timeout=5.0)

    def kill_all(self) -> str:
        return self.run(["shell", "am", "kill-all"], check=False, timeout=10.0)

    def launch_package(self, package: str) -> None:
        output = self.run(
            ["shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"],
            check=False,
            timeout=10.0,
        )
        lowered = output.lower()
        if "aborted" in lowered or "no activities found" in lowered:
            raise AdbHelperError(f"找不到可启动的 Activity：{package}")
        if "events injected" not in lowered:
            raise AdbHelperError(f"包名启动失败：{output or '(adb 无输出)'}")

    def list_packages(self) -> list[str]:
        output = self.run(["shell", "pm", "list", "packages", "-3"], timeout=15.0)
        return sorted(
            line.removeprefix("package:").strip()
            for line in output.splitlines()
            if line.strip().startswith("package:")
        )

    def uninstall(self, package: str) -> str:
        return self.run(["uninstall", package], check=False, timeout=60.0)

    def install(self, apk_path: str | Path, *, replace: bool = True) -> str:
        path = Path(apk_path)
        if not path.is_file():
            raise AdbHelperError(f"APK 文件不存在：{path}")
        args = ["install"] + (["-r"] if replace else []) + [str(path)]
        output = self.run(args, check=False, timeout=180.0)
        if "Success" not in output:
            raise AdbHelperError(f"APK 安装失败：{output or '(adb 无输出)'}")
        return output

    def reinstall(self, package: str, apk_path: str | Path) -> list[str]:
        """卸载重装（严格版，对齐 AGENTS 教训七），返回 adb 原始输出日志行。

        严格性：adb 输出必须有明确 Success/Failure 字样；空输出（device offline /
        adb 断连）立即抛错，不静默继续——否则 uninstall 静默失败后 install -r 会
        覆盖安装，被当「干净重装」，污染首次冷启动样本。失败时抛 AdbHelperError。
        """
        path = Path(apk_path)
        if not path.is_file():
            raise AdbHelperError(f"APK 文件不存在：{path}")
        log: list[str] = []
        out = self.run(["uninstall", package], check=False, timeout=60.0)
        log.append(f"uninstall: {out}")
        if not out:
            raise AdbHelperError("卸载失败：adb 无输出（设备离线或 adb 断连）")
        if "Failure" in out:
            # 兜底：部分设备（如装为系统用户）需要 --user 0 才能卸
            out2 = self.run(
                ["shell", "pm", "uninstall", "--user", "0", package],
                check=False, timeout=30.0,
            )
            log.append(f"pm uninstall --user 0: {out2}")
            if not out2:
                raise AdbHelperError("pm uninstall --user 0 无输出（设备离线或 adb 断连）")
        out3 = self.run(["install", "-r", str(path)], check=False, timeout=180.0)
        log.append(f"install: {out3}")
        if not out3:
            raise AdbHelperError("安装失败：adb 无输出（设备离线或 adb 断连）")
        if "Success" not in out3:
            hint = install_error_cn(out3)
            raise AdbHelperError(f"安装失败：{out3}" + (f"\n💡 {hint}" if hint else ""))
        return log

    def install_overwrite(self, package: str, apk_path: str | Path) -> list[str]:
        """覆盖安装（升级保数据）：单条 ``install -r``，不卸载、不清 App 数据。

        GP 与 iOS 同步的安装模式（2026-08）：adb 原生 ``install -r`` 对已装同包名
        即为升级覆盖。严格校验与 reinstall 同标准（教训七）：空输出（device offline）
        抛 AdbHelperError；无 Success 抛错并附中文错误码翻译；失败可接受场景不适用
        本方法——覆盖是用户显式选择，失败必须可见。
        """
        path = Path(apk_path)
        if not path.is_file():
            raise AdbHelperError(f"APK 文件不存在：{path}")
        log: list[str] = []
        out = self.run(["install", "-r", str(path)], check=False, timeout=180.0)
        log.append(f"install -r(覆盖安装): {out}")
        if not out:
            raise AdbHelperError("覆盖安装失败：adb 返回空（设备离线或 adb 异常）")
        if "Success" not in out:
            hint = install_error_cn(out)
            raise AdbHelperError(f"覆盖安装失败：{out}" + (f"\n💡 {hint}" if hint else ""))
        return log

    def parse_apk(self, apk_path: str | Path) -> ApkInfo:
        """Parse package/version metadata using aapt or aapt2 on either OS."""

        path = Path(apk_path)
        if not path.is_file():
            raise AdbHelperError(f"APK 文件不存在：{path}")
        tool = self._resolve_aapt()
        if tool is None:
            raise AdbHelperError("找不到 aapt/aapt2，无法解析 APK；请安装 Android build-tools")
        if Path(tool).name.lower().startswith("aapt2"):
            command = [tool, "dump", "badging", str(path)]
        else:
            command = [tool, "dump", "badging", str(path)]
        try:
            result = subprocess.run(command, capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AdbHelperError(f"APK 解析失败：{exc}") from exc
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        if result.returncode != 0:
            raise AdbHelperError(f"APK 解析失败：{output.strip() or result.returncode}")
        return parse_aapt_badging(output, path)

    def _resolve_aapt(self) -> str | None:
        candidates = [os.environ.get("AAPT"), os.environ.get("AAPT2")]
        executable_names = ["aapt.exe", "aapt2.exe"] if os.name == "nt" else ["aapt", "aapt2"]
        sdk_roots = [
            os.environ.get("ANDROID_HOME"),
            os.environ.get("ANDROID_SDK_ROOT"),
            str(Path.home() / "Library" / "Android" / "sdk"),
            os.environ.get("LOCALAPPDATA", "") + ("\\Android\\Sdk" if os.name == "nt" else ""),
        ]
        for sdk_root in sdk_roots:
            if not sdk_root:
                continue
            build_tools = Path(sdk_root) / "build-tools"
            if build_tools.is_dir():
                for version_dir in sorted(build_tools.iterdir(), reverse=True):
                    if version_dir.is_dir():
                        for name in executable_names:
                            candidates.append(str(version_dir / name))
        for name in executable_names:
            candidates.append(name)
            candidates.append(str(self.project_root / "build-tools" / name))
        for candidate in candidates:
            if not candidate:
                continue
            path = Path(candidate)
            if path.is_file():
                return str(path.resolve())
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
        return None

    @staticmethod
    def devices(adb_path: str | Path | None = None, project_root: str | Path | None = None) -> list[DeviceInfo]:
        helper = AdbHelper(None, adb_path=adb_path, project_root=project_root)
        output = helper.run(["devices"], include_serial=False, timeout=5.0)
        devices: list[DeviceInfo] = []
        for line in output.splitlines():
            parts = line.split()
            if len(parts) < 2 or parts[0] == "List":
                continue
            model = ""
            if parts[1] == "device":
                try:
                    model = helper.__class__(parts[0], adb_path=helper.adb_path, project_root=helper.project_root).run(
                        ["shell", "getprop", "ro.product.model"], timeout=3.0
                    )
                except AdbHelperError:
                    pass
            devices.append(DeviceInfo(parts[0], parts[1], model))
        return devices


class TemplateMatcher:
    """ROI OpenCV matcher used by ``ScreenPoller``."""

    def __init__(self, template: Any, cx: float, cy: float, *, padding: int = 20, threshold: float = 0.85) -> None:
        if template is None or len(template.shape) < 2:
            raise AdbHelperError("匹配模板无效")
        if float(template.std()) < 15.0:
            raise AdbHelperError("匹配模板纹理不足（标准差 < 15）")
        if not (0.0 <= cx <= 1.0 and 0.0 <= cy <= 1.0):
            raise AdbHelperError("匹配模板坐标必须在 0~1")
        self.template = template
        self.cx, self.cy = float(cx), float(cy)
        self.padding, self.threshold = int(padding), float(threshold)

    @classmethod
    def from_file(cls, path: str | Path, cx: float, cy: float, **kwargs: Any) -> "TemplateMatcher":
        import cv2
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None:
            raise AdbHelperError(f"无法读取匹配模板：{path}")
        return cls(image, cx, cy, **kwargs)

    def match(self, frame: Any) -> MatchResult:
        import cv2
        started = time.perf_counter()
        fh, fw = frame.shape[:2]
        th, tw = self.template.shape[:2]
        cx, cy = int(self.cx * fw), int(self.cy * fh)
        x1, y1 = max(0, cx - tw // 2 - self.padding), max(0, cy - th // 2 - self.padding)
        x2, y2 = min(fw, cx + tw // 2 + self.padding), min(fh, cy + th // 2 + self.padding)
        roi = frame[y1:y2, x1:x2]
        if roi.shape[0] < th or roi.shape[1] < tw:
            return MatchResult(False, 0.0, (time.perf_counter() - started) * 1000, roi.shape[1], roi.shape[0])
        result = cv2.matchTemplate(roi, self.template, cv2.TM_CCOEFF_NORMED)
        _, confidence, _, _ = cv2.minMaxLoc(result)
        return MatchResult(
            bool(confidence >= self.threshold),
            round(float(confidence), 4),
            round((time.perf_counter() - started) * 1000, 3),
            int(roi.shape[1]),
            int(roi.shape[0]),
        )


class ScreenPoller:
    """Serial screen capture/matching loop with a fixed N-ms cadence."""

    def __init__(
        self,
        adb: AdbHelper,
        *,
        interval_ms: float = 100.0,
        matcher: TemplateMatcher | None = None,
        on_sample: Callable[[ScreenSample], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        if interval_ms <= 0:
            raise ValueError("interval_ms 必须大于 0")
        self.adb = adb
        self.interval_s = float(interval_ms) / 1000.0
        self.matcher = matcher
        self.on_sample = on_sample
        self.on_error = on_error
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sequence = 0
        self.started_at: float | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> "ScreenPoller":
        if self.running:
            return self
        self._stop.clear()
        self.started_at = time.perf_counter()
        self._thread = threading.Thread(target=self._run, name="adb-screen-poller", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        if thread is None or not thread.is_alive():
            self._thread = None

    def poll_once(self) -> ScreenSample:
        started = time.perf_counter()
        try:
            frame, _via = self.adb.screenshot_bgr()
            captured_at = time.perf_counter()
            match = self.matcher.match(frame) if self.matcher else None
            sample = ScreenSample(
                sequence=self._sequence,
                started_at=started,
                captured_at=captured_at,
                capture_ms=round((captured_at - started) * 1000, 3),
                frame=frame,
                match=match,
            )
        except Exception as exc:
            sample = ScreenSample(
                sequence=self._sequence,
                started_at=started,
                captured_at=time.perf_counter(),
                capture_ms=round((time.perf_counter() - started) * 1000, 3),
                frame=None,
                error=str(exc),
            )
            if self.on_error:
                self.on_error(exc)
        self._sequence += 1
        if self.on_sample:
            self.on_sample(sample)
        return sample

    def _run(self) -> None:
        next_tick = time.perf_counter()
        while not self._stop.is_set():
            self.poll_once()
            next_tick += self.interval_s
            delay = next_tick - time.perf_counter()
            if delay <= 0:
                next_tick = time.perf_counter()
                continue
            self._stop.wait(delay)


def parse_aapt_badging(output: str, path: str | Path = "") -> ApkInfo:
    """Parse the stable fields emitted by ``aapt dump badging``."""
    if not isinstance(output, str):
        raise AdbHelperError("APK badging 输出必须是文本")

    # Different aapt versions use either ``application-label:`` or
    # ``application-label-en:``; prefer the generic label, then English.
    label = re.search(r"application-label\s*:\s*'([^']*)'", output)
    if label is None:
        label = re.search(r"application-label-en\s*:\s*'([^']*)'", output)
    package = re.search(r"package:\s+name='([^']+)'", output)
    version_name = re.search(r"versionName='([^']*)'", output)
    version_code = re.search(r"versionCode='([^']*)'", output)
    if not package:
        raise AdbHelperError("APK badging 输出中没有 package name")
    return ApkInfo(
        path=str(path),
        package=package.group(1),
        version_name=version_name.group(1) if version_name else "",
        version_code=version_code.group(1) if version_code else "",
        label=label.group(1) if label else "",
    )


def _raw_screencap_to_bgr(raw: bytes) -> Any:
    """Decode Android raw screencap RGBA/RGBX/BGRA bytes into BGR."""
    import numpy as np

    if len(raw) < 12:
        raise AdbHelperError("raw screencap 过短")
    width = int.from_bytes(raw[0:4], "little")
    height = int.from_bytes(raw[4:8], "little")
    pixel_format = int.from_bytes(raw[8:12], "little")
    if width <= 0 or height <= 0 or width > 10000 or height > 10000:
        raise AdbHelperError(f"raw screencap 尺寸异常：{width}x{height}")
    if pixel_format not in (1, 2, 5):
        raise AdbHelperError(f"不支持的 raw 像素格式：{pixel_format}")
    need = width * height * 4
    offset = 12 if len(raw) - 12 == need else 16 if len(raw) - 16 == need else None
    if offset is None:
        raise AdbHelperError("raw screencap 数据长度不匹配")
    pixels = np.frombuffer(raw, dtype=np.uint8, offset=offset).reshape(height, width, 4)
    return np.ascontiguousarray(pixels[:, :, :3] if pixel_format == 5 else pixels[:, :, 2::-1])


__all__ = [
    "AdbHelper", "AdbHelperError", "ApkInfo", "DeviceInfo", "MatchResult",
    "PerfTimer", "ScreenPoller", "ScreenSample", "TemplateMatcher",
    "install_error_cn", "parse_aapt_badging",
]
