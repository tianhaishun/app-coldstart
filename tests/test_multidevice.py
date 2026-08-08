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
