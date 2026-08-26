"""Tests for the cross-platform ADB helper and screen poller."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adb_helper import (  # noqa: E402
    AdbHelper,
    AdbHelperError,
    PerfTimer,
    ScreenPoller,
    TemplateMatcher,
    install_error_cn,
    parse_aapt_badging,
)


def test_parse_aapt_badging():
    output = """package: name='com.example.app' versionCode='42' versionName='1.2.3'
application-label:'Example App'
application-label-en:'Example App'
"""
    info = parse_aapt_badging(output, "demo.apk")
    assert info.package == "com.example.app"
    assert info.version_code == "42"
    assert info.version_name == "1.2.3"
    assert info.label == "Example App"
    assert info.path == "demo.apk"


def test_parse_aapt_badging_prefers_generic_label():
    output = "package: name='com.example' versionCode='1' versionName='1'\napplication-label-en:'English'\napplication-label:'通用名称'"
    assert parse_aapt_badging(output).label == '通用名称'


def test_parse_aapt_badging_rejects_missing_package():
    with pytest.raises(AdbHelperError, match="package name"):
        parse_aapt_badging("application-label:'No package'")


def test_perf_timer_uses_monotonic_elapsed_time():
    timer = PerfTimer().start()
    time.sleep(0.001)
    elapsed = timer.stop()
    assert elapsed > 0
    assert timer.running is False
    assert timer.elapsed_ms >= 1.0


def test_adb_helper_builds_serialized_commands(monkeypatch, tmp_path):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"device\n", stderr=b"")

    monkeypatch.setattr("adb_helper.subprocess.run", fake_run)
    adb = AdbHelper("SERIAL", adb_path="adb", project_root=tmp_path)
    assert adb.get_state() == "device"
    assert calls[0][0][1:] == ["-s", "SERIAL", "get-state"]
    assert calls[0][1]["timeout"] == 5.0


def test_adb_helper_rejects_command_timeout(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        raise __import__("subprocess").TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr("adb_helper.subprocess.run", fake_run)
    adb = AdbHelper("SERIAL", adb_path="adb", project_root=tmp_path)
    with pytest.raises(AdbHelperError, match="超时"):
        adb.get_state()


def test_adb_helper_swipe_builds_command(monkeypatch, tmp_path):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr("adb_helper.subprocess.run", fake_run)
    adb = AdbHelper("SERIAL", adb_path="adb", project_root=tmp_path)
    adb.swipe(10, 20, 300, 400, dur_ms=150)
    # 与既有测试一致：adb 路径可解析为绝对路径，只断言 index 1 起（-s SERIAL ...）
    assert calls[0][1:] == [
        "-s", "SERIAL",
        "shell", "input", "swipe", "10", "20", "300", "400", "150",
    ]


def test_reinstall_rejects_empty_uninstall_output(monkeypatch, tmp_path):
    """教训七：uninstall 空输出（设备离线）必须抛错，不能 install -r 变覆盖安装。"""

    def fake_run(command, **kwargs):
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr("adb_helper.subprocess.run", fake_run)
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"PK")
    adb = AdbHelper("SERIAL", adb_path="adb", project_root=tmp_path)
    with pytest.raises(AdbHelperError, match="无输出"):
        adb.reinstall("com.example", apk)


def test_reinstall_uses_pm_fallback_on_failure(monkeypatch, tmp_path):
    outputs = iter(
        [SimpleNamespace(returncode=0, stdout=b"Failure [INSTALL_FAILED_ALREADY_EXISTS]", stderr=b""),
         SimpleNamespace(returncode=0, stdout=b"Success", stderr=b""),
         SimpleNamespace(returncode=0, stdout=b"Success", stderr=b"")]
    )

    def fake_run(command, **kwargs):
        return next(outputs)

    monkeypatch.setattr("adb_helper.subprocess.run", fake_run)
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"PK")
    adb = AdbHelper("SERIAL", adb_path="adb", project_root=tmp_path)
    log = adb.reinstall("com.example", apk)
    assert any("pm uninstall --user 0" in line for line in log)
    assert any("install" in line and "Success" in line for line in log)


def test_reinstall_translates_install_error(monkeypatch, tmp_path):
    outputs = iter(
        [SimpleNamespace(returncode=0, stdout=b"Success", stderr=b""),
         SimpleNamespace(returncode=0, stdout=b"Failure [INSTALL_FAILED_OLDER_SDK]", stderr=b"")]
    )

    def fake_run(command, **kwargs):
        return next(outputs)

    monkeypatch.setattr("adb_helper.subprocess.run", fake_run)
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"PK")
    adb = AdbHelper("SERIAL", adb_path="adb", project_root=tmp_path)
    with pytest.raises(AdbHelperError, match="OLDER_SDK|系统版本过低"):
        adb.reinstall("com.example", apk)


def test_install_error_cn_translation():
    assert "系统版本过低" in install_error_cn("Failure [INSTALL_FAILED_OLDER_SDK]")
    assert install_error_cn("Success") is None


# ── 覆盖安装（2026-08：GP/iOS 同步的 install -r 升级保数据路径）──


def test_install_overwrite_single_install_no_uninstall(monkeypatch, tmp_path):
    """覆盖安装 = 单条 install -r，绝不发 uninstall（清数据）命令。"""
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=b"Success", stderr=b"")

    monkeypatch.setattr("adb_helper.subprocess.run", fake_run)
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"PK")
    adb = AdbHelper("SERIAL", adb_path="adb", project_root=tmp_path)
    log = adb.install_overwrite("com.example", apk)
    assert len(calls) == 1
    assert "install" in calls[0] and "-r" in calls[0]
    assert all("uninstall" not in c for c in calls)
    assert "Success" in "\n".join(log)


def test_install_overwrite_rejects_empty_output(monkeypatch, tmp_path):
    """教训七同标准：空输出（device offline）必须抛错，失败不可见会污染测速前提。"""

    def fake_run(command, **kwargs):
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr("adb_helper.subprocess.run", fake_run)
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"PK")
    adb = AdbHelper("SERIAL", adb_path="adb", project_root=tmp_path)
    with pytest.raises(AdbHelperError, match="空"):
        adb.install_overwrite("com.example", apk)


def test_install_overwrite_rejects_failure_with_cn_hint(monkeypatch, tmp_path):
    """Failure 输出抛错并附中文翻译（与 reinstall 同款 install_error_cn 兜底）。"""

    def fake_run(command, **kwargs):
        return SimpleNamespace(returncode=0, stdout=b"Failure [INSTALL_FAILED_OLDER_SDK]", stderr=b"")

    monkeypatch.setattr("adb_helper.subprocess.run", fake_run)
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"PK")
    adb = AdbHelper("SERIAL", adb_path="adb", project_root=tmp_path)
    with pytest.raises(AdbHelperError, match="OLDER_SDK|系统版本过低"):
        adb.install_overwrite("com.example", apk)


def test_install_overwrite_missing_file(monkeypatch, tmp_path):
    """文件不存在直接抛错，不碰 adb。"""
    adb = AdbHelper("SERIAL", adb_path="adb", project_root=tmp_path)
    with pytest.raises(AdbHelperError, match="不存在"):
        adb.install_overwrite("com.example", tmp_path / "nope.apk")


def test_screen_poller_captures_and_matches_at_fixed_interval():
    cv2 = pytest.importorskip("cv2", reason="OpenCV is required for matcher tests")
    frame = np.zeros((80, 100, 3), dtype=np.uint8)
    template = np.zeros((20, 30, 3), dtype=np.uint8)
    cv2.rectangle(template, (3, 3), (26, 16), (30, 180, 240), -1)
    frame[30:50, 35:65] = template

    class FakeAdb:
        def screenshot_bgr(self):
            return frame.copy(), "fake"

    matcher = TemplateMatcher(template, 0.5, 0.5, padding=10, threshold=0.85)
    samples = []
    poller = ScreenPoller(FakeAdb(), interval_ms=5, matcher=matcher, on_sample=samples.append)
    poller.start()
    time.sleep(0.035)
    poller.stop()

    assert samples
    assert samples[0].sequence == 0
    assert all(sample.frame.shape == frame.shape for sample in samples)
    assert any(sample.match and sample.match.hit for sample in samples)
    assert all(sample.capture_ms >= 0 for sample in samples)


def test_template_matcher_rejects_too_small_frame():
    cv2 = pytest.importorskip("cv2", reason="OpenCV is required for matcher tests")
    template = np.zeros((40, 40, 3), dtype=np.uint8)
    template[5:35, 5:35] = (20, 100, 220)
    matcher = TemplateMatcher(template, 0.5, 0.5)
    result = matcher.match(np.zeros((20, 20, 3), dtype=np.uint8))
    assert result.hit is False
    assert result.confidence == 0.0
