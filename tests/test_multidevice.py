"""多设备（v4）核心验证：每设备独立锁 + 模板隔离 + 并行轮询不叠加延迟。

设计依据（AGENTS §2.4 + 用户需求「多设备 + 识别延迟 ≤400ms」）：
  - 每台设备一把独立 RLock，adb 命令只在该设备锁下串行
  - 不同设备的 check_auto 经 adb server 多路复用并行，互不阻塞
  - 测试直接调用 check_auto 函数（不走 HTTP），用 mock 截图验证并发不串行
"""

import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adb_helper import AdbHelper  # noqa: E402
from server import SESSION, check_auto  # noqa: E402


def _reset_registry():
    """清空全局注册表，避免测试间/测试内互相污染。"""
    SESSION._devices.clear()
    SESSION._serial = None


def test_device_sessions_independent_state():
    """两个 serial 得到两个独立 DeviceSession：模板/统计/锁都不共享。"""
    _reset_registry()
    try:
        a = SESSION.session_for("DEV_A")
        b = SESSION.session_for("DEV_B")
        # 独立实例
        assert a is not b
        assert a.lock is not b.lock
        # 模板文件按 serial 隔离
        assert a.marker_path != b.marker_path
        assert "DEV_A" in a.marker_path.name
        assert "DEV_B" in b.marker_path.name
        # 设 A 的模板状态不影响 B
        a._marker_w = 240
        assert b._marker_w == 0
        # 同一 serial 复用同一会话（含平台类型）
        assert SESSION.session_for("DEV_A") is a
    finally:
        _reset_registry()


def test_parallel_check_auto_not_serialized(monkeypatch):
    """两台设备并发 check_auto：截图 mock 0.3s，总耗时应 ~0.3s 而非 0.6s。

    若被全局锁串行化，wall ≥ 0.55s；每设备独立锁则 < 0.55s。
    """
    _reset_registry()

    def fake_shot(self):
        time.sleep(0.3)  # 模拟真实截图耗时（Pixel 6a raw_gzip ~350ms）
        return np.zeros((16, 16, 3), dtype=np.uint8), "raw_gzip"

    monkeypatch.setattr(AdbHelper, "screenshot_bgr", fake_shot)
    try:
        results = {}

        def run(serial):
            results[serial] = check_auto(check_skips=False, serial=serial)

        t0 = time.perf_counter()
        t1 = threading.Thread(target=run, args=("DEV_A",))
        t2 = threading.Thread(target=run, args=("DEV_B",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        wall = time.perf_counter() - t0

        # 两台设备都走到「未设模板」分支（截图已完成，模板未设）
        assert all(r["error"] == "未设模板" for r in results.values())
        assert wall < 0.55, f"两台设备 check_auto 被串行化了：wall={wall:.2f}s（应 ~0.3s）"
    finally:
        _reset_registry()


def test_check_auto_rising_edge_and_hit(monkeypatch):
    """模板比对停表核心（AGENTS §2.5）：先低于阈值（桌面）→ 再高于阈值（启动成功页）
    → 连续确认停表。直接测 check_auto 的上升沿 + hit 状态机（经得起检验）。"""
    import cv2

    _reset_registry()
    try:
        ds = SESSION.session_for("DEV_RISE")
        # 模板：彩色方块（纹理足够，避开纯色拒绝）
        template = np.zeros((24, 40, 3), dtype=np.uint8)
        cv2.rectangle(template, (2, 2), (37, 21), (40, 180, 240), -1)
        ds._marker_img = template.copy()
        ds._marker_w, ds._marker_h = template.shape[1], template.shape[0]
        ds._marker_cx, ds._marker_cy = 0.5, 0.5
        ds._marker_matcher = None
        ds.reset_marker_watch()  # 无 force_stop：须先见过低于阈值（上升沿）
        # 帧序列：桌面（无模板）→ 启动成功页（有模板）→ 启动成功页
        scene_on = np.zeros((80, 120, 3), dtype=np.uint8)
        scene_on[28:52, 40:80] = template
        scene_off = np.zeros((80, 120, 3), dtype=np.uint8)
        seq = iter([scene_off, scene_on, scene_on])

        monkeypatch.setattr(
            AdbHelper, "screenshot_bgr",
            lambda self: (next(seq).copy(), "raw_gzip"),
        )
        # 第 1 帧：低于阈值 → 记 seen_below，不停表
        r1 = check_auto(check_skips=False, serial="DEV_RISE")
        assert r1["hit"] is False
        assert r1["rising_ready"] is True  # 已见过低于阈值
        # 第 2 帧：过阈且上升沿满足 → 立即停表
        r2 = check_auto(check_skips=False, serial="DEV_RISE")
        assert r2["above"] is True
        assert r2["hit"] is True, f"启动成功页应停表：{r2}"
        assert r2["streak"] >= 1
    finally:
        _reset_registry()


def test_sys_baseline_parses_am_start(monkeypatch):
    """系统对照（审核 A-P0-1）：resolve-activity 组件解析 + am start -W TotalTime 解析。
    直接调端点函数：mock adb 输出，验证 samples/stats/errors（解析失败轮跳过不中止）。"""
    from server import SysBaselineReq, sys_baseline

    _reset_registry()
    try:
        calls = {"n": 0}

        def fake_run(self, args, **kwargs):
            joined = " ".join(args)
            if "resolve-activity" in joined:
                return "com.example/.MainActivity"
            if "am start -W" in joined:
                calls["n"] += 1
                i = calls["n"]
                if i == 2:  # 第 2 轮模拟解析失败（无 TotalTime）
                    return "Status: error\nError type 3"
                return (
                    f"Status: ok\nActivity: com.example/.MainActivity\n"
                    f"ThisTime: {100 + i}\nTotalTime: {400 + i}\nWaitTime: {410 + i}"
                )
            return ""  # force-stop 等

        monkeypatch.setattr(AdbHelper, "run", fake_run)
        result = sys_baseline(SysBaselineReq(package="com.example", rounds=3, cooldown_s=0, serial="DEV_A"))
        assert result["ok"] is True
        assert result["component"] == "com.example/.MainActivity"
        assert len(result["samples"]) == 2          # 第 2 轮被跳过
        assert result["stats"]["n"] == 2
        assert result["stats"]["total_mean_ms"] == 402.0     # (401+403)/2
        assert result["stats"]["total_median_ms"] == 402.0
        assert len(result["errors"]) == 1 and "解析失败" in result["errors"][0]

        # 全部轮次解析失败 → 抛错（HTTP 400 由端点层转）
        calls["n"] = 0
        monkeypatch.setattr(AdbHelper, "run", lambda self, args, **kw: "Status: error\nError type 3"
                            if "am start -W" in " ".join(args) else "com.example/.MainActivity")
        import pytest
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            sys_baseline(SysBaselineReq(package="com.example", rounds=2, cooldown_s=0, serial="DEV_A"))
        assert ei.value.status_code == 400  # 全部失败 → HTTP 400
    finally:
        _reset_registry()


def test_no_device_returns_400_not_500():
    """回归（审核 B 实施中发现）：无选中设备时端点必须 400 / {ok:false}，不能 500。"""
    from fastapi import HTTPException
    from server import (ForceStopReq, ReinstallReq, TapReq,
                        force_stop, marker_status, reinstall, tap)

    _reset_registry()
    try:
        with pytest.raises(HTTPException) as ei:
            marker_status(None)
        assert ei.value.status_code == 400

        with pytest.raises(HTTPException) as ei:
            force_stop(ForceStopReq(package="com.x"))
        assert ei.value.status_code == 400

        with pytest.raises(HTTPException) as ei:
            tap(TapReq(x=0.5, y=0.5))
        assert ei.value.status_code == 400

        # reinstall 走 {ok:false} 结构（前端按此处理）
        r = reinstall(ReinstallReq(package="com.x", apk_path="/nonexistent.apk"))
        assert r["ok"] is False and "apk_info" in r and "log" in r
    finally:
        _reset_registry()


def test_res_mismatch_detection(monkeypatch):
    """审核 F-P1-4：分辨率变化检测三态——变了=true、一致=false、未知（项目加载）=false。"""
    import cv2

    _reset_registry()
    try:
        ds = SESSION.session_for("DEV_RES")
        template = np.zeros((24, 40, 3), dtype=np.uint8)
        cv2.rectangle(template, (2, 2), (37, 21), (40, 180, 240), -1)
        ds._marker_img = template.copy()
        ds._marker_w, ds._marker_h = template.shape[1], template.shape[0]
        ds._marker_cx, ds._marker_cy = 0.5, 0.5
        ds._marker_matcher = None
        ds._marker_res = (1080, 2400)   # 设模板时分辨率
        ds.device._last_size = (720, 1600)  # 当前分辨率已变

        monkeypatch.setattr(
            AdbHelper, "screenshot_bgr",
            lambda self: (np.zeros((80, 120, 3), dtype=np.uint8), "raw_gzip"),
        )
        r1 = check_auto(check_skips=False, serial="DEV_RES")
        assert r1["res_mismatch"] is True
        assert r1["hit"] is False  # 仅提示，不阻塞

        ds.device._last_size = (1080, 2400)  # 分辨率一致
        r2 = check_auto(check_skips=False, serial="DEV_RES")
        assert r2["res_mismatch"] is False

        ds._marker_res = None  # 项目加载（分辨率未知）→ 跳过检查
        r3 = check_auto(check_skips=False, serial="DEV_RES")
        assert r3["res_mismatch"] is False
    finally:
        _reset_registry()


def test_ios_launch_pkg_uses_process_control(monkeypatch):
    """审核修复：iOS launch_pkg 走 DvtProvider + ProcessControl（原占位不启动 App）。
    mock pymobiledevice3，验证 launch 被调用（kill_existing=True）且 falsy PID 抛 AdbError。"""
    from server import AdbError, IosDevice

    calls = {}

    class FakePC:
        def __init__(self, dvt):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def launch(self, bundle_id, kill_existing=True):
            calls["launch"] = (bundle_id, kill_existing)
            return 12345

    class FakeDvt:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class FakeApps:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def launch_application(self, bundle_id, kill_existing=True):
            calls["launch"] = (bundle_id, kill_existing)
            return {"processToken": {"processIdentifier": 12345}}

    class FakeTunnel:
        async def __aenter__(self):
            return object()  # rsd
        async def __aexit__(self, *a):
            return False
    monkeypatch.setattr(
        "pymobiledevice3.remote.userspace_tunnel.UserspaceRsdTunnel",
        lambda serial=None, **kw: FakeTunnel(),
    )
    monkeypatch.setattr(
        "pymobiledevice3.remote.core_device.app_service.AppServiceService",
        lambda rsd: FakeApps(),
    )
    dev = IosDevice("UDID")
    dev.launch_pkg("com.example.app")
    assert calls["launch"] == ("com.example.app", True)  # kill_existing=True：等效杀进程+冷启动

    # falsy PID → AdbError（启动失败必须抛错，不静默）
    class FakeApps0(FakeApps):
        async def launch_application(self, bundle_id, kill_existing=True):
            return {}
    monkeypatch.setattr(
        "pymobiledevice3.remote.core_device.app_service.AppServiceService",
        lambda rsd: FakeApps0(),
    )
    with pytest.raises(AdbError, match="无效 PID"):
        dev.launch_pkg("com.example.app")


def test_ios_reinstall_uses_install_from_local(monkeypatch, tmp_path):
    """审核修复：iOS reinstall 用 install_from_local（原 ip.install 在 10.x 不存在，
    会 AttributeError——iOS 安装实际是坏的）。验证 uninstall+install_from_local 被调用。"""
    from server import IosDevice

    calls = {}

    class FakeIP:
        async def uninstall(self, pkg, **kw):
            calls["uninstall"] = pkg

        async def install_from_local(self, path, **kw):
            calls["install_from_local"] = str(path)

    monkeypatch.setattr(
        "pymobiledevice3.services.installation_proxy.InstallationProxyService",
        lambda lockdown=None: FakeIP(),
    )
    dev = IosDevice("UDID")

    async def fake_lockdown():
        return object()
    monkeypatch.setattr(dev, "_get_lockdown", fake_lockdown)

    ipa = tmp_path / "app.ipa"
    ipa.write_bytes(b"PK")
    log = dev.reinstall("com.example.app", str(ipa))
    assert calls.get("uninstall") == "com.example.app"
    assert calls.get("install_from_local") == str(ipa)
    assert any("install" in line for line in log)


def test_verify_launch_matches_foreground_pkg(monkeypatch):
    """启动测试（审核新功能）：Android 前台包名与期望比对——一致/不一致/无法解析。"""
    from server import VerifyLaunchReq, verify_launch

    _reset_registry()
    try:
        fg = {"value": "mCurrentFocus=Window{abc u0 com.example/.MainActivity}"}

        def fake_run(self, args, **kwargs):
            joined = " ".join(args)
            if "launch" in joined and "monkey" in joined:
                return "Events injected: 1"
            if "dumpsys" in joined:
                return fg["value"]
            return ""

        monkeypatch.setattr(AdbHelper, "run", fake_run)
        monkeypatch.setattr(AdbHelper, "launch_package", lambda self, pkg: None)

        # 一致
        r = verify_launch(VerifyLaunchReq(package="com.example", serial="DEV_A"))
        assert r["match"] is True and r["foreground_pkg"] == "com.example"
        # 不一致（装错包）
        fg["value"] = "mFocusedApp=ActivityRecord{abc u0 com.other/.Main t1}"
        r = verify_launch(VerifyLaunchReq(package="com.example", serial="DEV_A"))
        assert r["match"] is False and r["foreground_pkg"] == "com.other"
        # 无法解析（ROM 差异）
        fg["value"] = "mCurrentFocus=Window{abc u0 (no package)}"
        r = verify_launch(VerifyLaunchReq(package="com.example", serial="DEV_A"))
        assert r["match"] is None and "note" in r
    finally:
        _reset_registry()


def test_verify_launch_ios_returns_note(monkeypatch):
    """启动测试 iOS 路径：launch 成功即返回 note（非越狱无前台检测）。"""
    from server import VerifyLaunchReq, IosDevice, verify_launch
    from fastapi import HTTPException

    _reset_registry()
    try:
        SESSION.session_for("IOS_DEV", "ios")  # 预建 iOS 会话（platform 决定走 iOS 分支）
        monkeypatch.setattr(IosDevice, "launch_package", lambda self, pkg: None)
        r = verify_launch(VerifyLaunchReq(package="com.example", serial="IOS_DEV"))
        assert r["ok"] is True and r["match"] is None and "iOS" in r["note"]
    finally:
        _reset_registry()


def test_marker_persists_across_sessions():
    """模板持久化（真机痛点修复）：设模板落盘后，新 DeviceSession 自动恢复。"""
    import cv2
    from server import DeviceSession

    _reset_registry()
    try:
        # 构造一个带模板的会话（模拟 set_marker_template 落盘）
        ds1 = SESSION.session_for("DEV_PERSIST")
        template = np.zeros((24, 40, 3), dtype=np.uint8)
        cv2.rectangle(template, (2, 2), (37, 21), (40, 180, 240), -1)
        ds1._marker_template = ds1.marker_path
        ds1._marker_w, ds1._marker_h = 40, 24
        ds1._marker_cx, ds1._marker_cy = 0.3, 0.4
        ds1.marker_threshold = 0.9
        ds1._marker_res = (1080, 2400)
        # 直接调持久化保存（复现 set_marker_template 的落盘逻辑）
        from server import _device_template_files
        marker_file, meta_file = _device_template_files("DEV_PERSIST")
        ok_buf, buf = cv2.imencode(".png", template)
        assert ok_buf
        marker_file.write_bytes(buf.tobytes())
        import json
        meta_file.write_text(json.dumps({
            "w": 40, "h": 24, "cx": 0.3, "cy": 0.4,
            "threshold": 0.9, "res": [1080, 2400],
        }), encoding="utf-8")

        # 新会话（模拟后端重启）→ 自动恢复
        ds2 = DeviceSession("DEV_PERSIST")
        assert ds2._marker_template is not None
        assert ds2._marker_w == 40 and ds2._marker_h == 24
        assert ds2.marker_threshold == 0.9
        assert ds2._marker_res == (1080, 2400)
        # 恢复的模板可正常用于匹配（ensure_marker_image 读盘）
        img = ds2.ensure_marker_image()
        assert img is not None and img.shape[0] == 24
    finally:
        _reset_registry()
        # 清理持久化文件（避免污染其他测试）
        import shutil
        from server import DEVICE_TEMPLATES_DIR
        for f in DEVICE_TEMPLATES_DIR.glob("marker_DEV_PERSIST*"):
            f.unlink(missing_ok=True)
        for f in DEVICE_TEMPLATES_DIR.glob("meta_DEV_PERSIST*"):
            f.unlink(missing_ok=True)
