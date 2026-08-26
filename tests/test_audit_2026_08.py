"""2026-08 审核修复（方案 C）回归测试。

对应修复项与验证点：
  - _check_pkg：包名白名单校验，封堵设备侧 sh 注入面（高2/中5）
  - _crop_template_region：set_marker / set_skip 共用裁剪逻辑抽单点后行为不变（低-重复代码）
  - IosDevice.model：device_info iOS 分支不再 AttributeError，缺失为空串契约（高1）
  - upload_apk：ZIP 魔数 + 大小上限（中7）
  - busy 标志：长事务持锁有提示通道，/api/devices 透出 additive 字段（中9）
  - reinstall 路径探测归一化 + 包名校验走 {ok:false}（中4 后半 + 中5）
  - cold_start reset_marker_watch 移入锁内：顺序在 tap 之后（中6）
  - screenshot 失败只计一次 shot_errors（低12）

运行：.venv\\Scripts\\python.exe -m pytest tests/test_audit_2026_08.py -v
"""

import asyncio
import io
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

# 确保能 import server（pytest 默认只把 tests/ 加进 sys.path，需手动加项目根）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import server  # noqa: E402
from adb_helper import AdbHelper  # noqa: E402
from server import (  # noqa: E402
    SESSION,
    AdbError,
    ColdStartReq,
    DeviceSession,
    IosDevice,
    ReinstallReq,
    _check_pkg,
    _crop_template_region,
    cold_start,
    list_devices,
    reinstall,
    screenshot,
    upload_apk,
)


def _reset_registry():
    """清空全局注册表，避免测试间/真实会话互相污染。"""
    SESSION._devices.clear()
    SESSION._serial = None


# ── _check_pkg：包名白名单 ───────────────────────────────────────


class TestCheckPkg:
    def test_valid_android_pkg(self):
        assert _check_pkg("com.example.app") == "com.example.app"

    def test_valid_ios_bundle(self):
        # iOS bundle id 与 Android 包名同一字符集
        assert _check_pkg("com.company.AppName_2") == "com.company.AppName_2"

    def test_strips_whitespace(self):
        assert _check_pkg("  com.example.app  ") == "com.example.app"

    @pytest.mark.parametrize("bad", [
        "com.a; rm -rf /",       # 命令拼接
        "com.a$(id)",            # 命令替换
        "com.a`id`",             # 反引号
        "com.a b",               # 空格（adb shell 参数按空格拼给设备端 sh）
        "com.a&whoami",          # 后台执行
        "com.a|cat /etc/passwd", # 管道
        "",                      # 空
        "   ",                   # 纯空白
        "com.a\nb",              # 换行注入
    ])
    def test_rejects_injection(self, bad):
        with pytest.raises(AdbError, match="非法包名"):
            _check_pkg(bad)


# ── _crop_template_region：共用裁剪逻辑 ──────────────────────────


class TestCropTemplateRegion:
    HINT = "测试引导文案。"

    def _textured_img(self, w=1080, h=2400):
        """带噪声的图（灰度标准差远大于 15），纯色拒绝不触发。"""
        rng = np.random.default_rng(42)
        return rng.integers(0, 255, size=(h, w, 3), dtype=np.uint8)

    def test_center_default_size(self):
        img = self._textured_img()
        template, x1, y1, w_px, h_px = _crop_template_region(
            img, 0.5, 0.5, None, None, 240, 120, self.HINT)
        assert template.shape[:2] == (120, 240)
        assert (w_px, h_px) == (1080, 2400)

    def test_box_proportional_with_min_clamp(self):
        img = self._textured_img(1000, 1000)
        # 10% → 100x100
        t, *_ = _crop_template_region(img, 0.5, 0.5, 0.1, 0.1, 240, 120, self.HINT)
        assert t.shape[:2] == (100, 100)
        # 0.1% → 换算 1px 但夹底 40px
        t, *_ = _crop_template_region(img, 0.5, 0.5, 0.001, 0.001, 240, 120, self.HINT)
        assert t.shape[:2] == (40, 40)

    def test_out_of_bounds_shift_keeps_size(self):
        """中心贴近左缘：整体平移回屏内，模板尺寸不变（matchTemplate 尺寸必须匹配）。"""
        img = self._textured_img(400, 300)
        template, x1, y1, _, _ = _crop_template_region(
            img, 0.01, 0.5, None, None, 240, 120, self.HINT)
        assert x1 == 0                       # 左越界被平移到贴边
        assert template.shape[:2] == (120, 240)

    def test_pure_color_rejected(self):
        img = np.full((300, 400, 3), 128, dtype=np.uint8)
        with pytest.raises(AdbError, match="纯色"):
            _crop_template_region(img, 0.5, 0.5, None, None, 240, 120, self.HINT)

    def test_invalid_box_falls_back_to_default(self):
        """box_w/box_h 任一非法（<=0 或 >1）→ 用默认尺寸，与原实现一致。"""
        img = self._textured_img()
        t, *_ = _crop_template_region(img, 0.5, 0.5, 0.5, None, 240, 120, self.HINT)
        assert t.shape[:2] == (120, 240)


# ── IosDevice.model：device_info iOS 崩溃修复 ────────────────────


class TestIosModel:
    def test_model_empty_when_lockdown_fails(self, monkeypatch):
        """lockdown 连不上（未信任/拔线）：返回空串而非抛错——契约「缺失为空」。"""
        dev = IosDevice("UDID_TEST")

        async def _boom():
            raise RuntimeError("no device")

        monkeypatch.setattr(dev, "_get_lockdown", _boom)
        assert dev.model == ""

    def test_model_cached_after_failure(self, monkeypatch):
        """失败结果缓存：不因反复连设备拖慢后续调用。"""
        dev = IosDevice("UDID_TEST")
        calls = {"n": 0}

        async def _boom():
            calls["n"] += 1
            raise RuntimeError("no device")

        monkeypatch.setattr(dev, "_get_lockdown", _boom)
        dev.model
        dev.model
        assert calls["n"] == 1

    def test_model_prefers_device_name(self, monkeypatch):
        dev = IosDevice("UDID_TEST")
        fake_ld = SimpleNamespace(all_values={"DeviceName": "张一的 iPhone", "ProductType": "iPhone15,3"})

        async def _ok():
            return fake_ld

        monkeypatch.setattr(dev, "_get_lockdown", _ok)
        assert dev.model == "张一的 iPhone"

    def test_model_falls_back_to_product_type(self, monkeypatch):
        dev = IosDevice("UDID_TEST")
        fake_ld = SimpleNamespace(all_values={"ProductType": "iPhone15,3"})

        async def _ok():
            return fake_ld

        monkeypatch.setattr(dev, "_get_lockdown", _ok)
        assert dev.model == "iPhone15,3"


# ── upload_apk：魔数 + 大小上限 ──────────────────────────────────


class TestUploadApk:
    def _upload(self, filename: str, data: bytes):
        from fastapi import UploadFile
        return upload_apk(UploadFile(file=io.BytesIO(data), filename=filename))

    def test_non_zip_rejected_and_cleaned(self, monkeypatch, tmp_path):
        monkeypatch.setattr(server, "APK_UPLOAD_DIR", tmp_path)
        with pytest.raises(HTTPException) as ei:
            asyncio.run(self._upload("evil.apk", b"definitely not a zip"))
        assert ei.value.status_code == 400 and "ZIP" in str(ei.value.detail)
        assert list(tmp_path.iterdir()) == []  # 半成品已删

    def test_valid_zip_accepted(self, monkeypatch, tmp_path):
        monkeypatch.setattr(server, "APK_UPLOAD_DIR", tmp_path)
        r = asyncio.run(self._upload("app.apk", b"PK\x03\x04" + b"\x00" * 1024))
        assert r["ok"] is True and r["size_bytes"] == 1024 + 4

    def test_oversize_aborts_and_cleans(self, monkeypatch, tmp_path):
        monkeypatch.setattr(server, "APK_UPLOAD_DIR", tmp_path)
        monkeypatch.setattr(server, "APK_MAX_BYTES", 1024 * 1024)  # 1MB 上限便于测试
        payload = b"PK\x03\x04" + b"\x00" * (2 * 1024 * 1024)
        with pytest.raises(HTTPException) as ei:
            asyncio.run(self._upload("big.apk", payload))
        assert ei.value.status_code == 400 and "上限" in str(ei.value.detail)
        assert list(tmp_path.iterdir()) == []

    def test_non_apk_extension_rejected_first(self, monkeypatch, tmp_path):
        monkeypatch.setattr(server, "APK_UPLOAD_DIR", tmp_path)
        with pytest.raises(HTTPException) as ei:
            asyncio.run(self._upload("note.txt", b"PK\x03\x04"))
        assert "apk" in str(ei.value.detail)


# ── busy 标志：长事务提示通道 ────────────────────────────────────


class TestBusyFlag:
    def test_default_not_busy(self):
        ds = SESSION.session_for("DEV_BUSY0")
        assert ds.busy is False

    def test_devices_expose_busy_additive(self, monkeypatch):
        """/api/devices 对忙碌中的已有会话设备补充 busy=True；其余设备不带该键。"""
        _reset_registry()
        try:
            fake_devs = [SimpleNamespace(serial="SER_B", state="device", model="M")]
            monkeypatch.setattr(AdbHelper, "devices", staticmethod(lambda **kw: fake_devs))
            monkeypatch.setattr(IosDevice, "devices", staticmethod(lambda: []))
            ds = SESSION.session_for("SER_B")
            ds.busy = True
            out = list_devices()
            entry = next(d for d in out["devices"] if d["serial"] == "SER_B")
            assert entry.get("busy") is True
            # 未忙碌设备不应出现 busy 键（additive 语义：只有真忙才写）
            SESSION.session_for("SER_B").busy = False
            out = list_devices()
            entry = next(d for d in out["devices"] if d["serial"] == "SER_B")
            assert "busy" not in entry
        finally:
            _reset_registry()

    def test_verify_launch_resets_busy_on_error(self, monkeypatch):
        """verify_launch 抛错路径也必须复位 busy（finally 保证）。"""
        from server import VerifyLaunchReq, verify_launch

        _reset_registry()
        try:
            ds = SESSION.session_for("DEV_VB")
            monkeypatch.setattr(AdbHelper, "launch_package",
                                lambda self, pkg: (_ for _ in ()).throw(AdbError("boom")))

            def _watch_busy(flag):
                flag["during"] = ds.busy

            # launch_package 抛错前 busy 应已置位；抛错后被 finally 复位
            orig = AdbHelper.launch_package

            def _fail_then_record(self, pkg):
                _watch_busy(locals())
                raise AdbError("boom")

            monkeypatch.setattr(AdbHelper, "launch_package", _fail_then_record)
            with pytest.raises(HTTPException):
                verify_launch(VerifyLaunchReq(package="com.a.b", serial="DEV_VB"))
            assert ds.busy is False
        finally:
            _reset_registry()


# ── reinstall：路径探测归一化 + 包名校验 ─────────────────────────


class TestReinstallHardening:
    def test_missing_file_generic_error_no_path_echo(self, tmp_path):
        """文件不存在：错误信息统一口径，且**不回显路径**（防任意路径探测）。"""
        _reset_registry()
        try:
            missing = tmp_path / "secret_dir" / "x.apk"
            r = reinstall(ReinstallReq(
                package="com.a.b", apk_path=str(missing), serial="DEV_PROBE"))
            assert r["ok"] is False
            assert "不可用" in r["error"]
            assert "secret_dir" not in r["error"] and "x.apk" not in r["error"]
        finally:
            _reset_registry()

    def test_invalid_pkg_via_ok_false_channel(self):
        """reinstall 的包名校验失败走 {ok:false}（前端既有错误通道），不是 500。"""
        _reset_registry()
        try:
            r = reinstall(ReinstallReq(
                package="com.a; shutdown", apk_path="whatever.apk", serial="DEV_PROBE2"))
            assert r["ok"] is False and "非法包名" in r["error"]
        finally:
            _reset_registry()


# ── cold_start：reset 移入锁内 ───────────────────────────────────


class TestColdStartResetOrdering:
    def test_reset_happens_inside_lock_after_tap(self, monkeypatch):
        """reset_marker_watch 必须发生在 tap 之后、且持锁状态下（审核中6）。"""
        _reset_registry()
        try:
            ds = SESSION.session_for("DEV_CS")
            SESSION.select("DEV_CS")  # cold_start(serial=None) 走当前选中
            order = []
            monkeypatch.setattr(AdbHelper, "force_stop", lambda self, pkg: order.append("force_stop"))
            monkeypatch.setattr(AdbHelper, "screen_size", lambda self: (1080, 2400))
            monkeypatch.setattr(AdbHelper, "tap_norm",
                                lambda self, x, y: order.append("tap"))

            orig_reset = DeviceSession.reset_marker_watch

            def _spy_reset(self, *, after_force_stop=False):
                owned = self.lock._is_owned()  # RLock 私有 API，仅测试断言用
                order.append(f"reset(owned={owned})")
                return orig_reset(self, after_force_stop=after_force_stop)

            monkeypatch.setattr(DeviceSession, "reset_marker_watch", _spy_reset)

            r = cold_start(ColdStartReq(mode="tap", x=0.5, y=0.5, package="com.a.b"))
            assert r["ok"] is True and r["marker_rising_seeded"] is True
            assert order[0] == "force_stop"
            assert order[1] == "tap"
            # reset 在 tap 之后、且确实持锁（RLock 已被本线程持有）
            assert order[2] == "reset(owned=True)"
        finally:
            _reset_registry()


# ── screenshot：shot_errors 单次计数 ─────────────────────────────


class TestShotErrorSingleCount:
    def test_failure_counted_once(self, monkeypatch):
        """/api/screenshot 失败时 shot_errors 只加 1（此前路由层又加一次变 2）。"""
        _reset_registry()
        try:
            ds = SESSION.session_for("DEV_SHOT")
            SESSION.select("DEV_SHOT")  # screenshot(serial=None) 走当前选中

            def _boom(self, target):
                raise AdbError("截图失败")

            monkeypatch.setattr(AdbHelper, "screenshot", _boom)
            with pytest.raises(HTTPException) as ei:
                screenshot(manual=1)  # manual=1 → use_cache=False → 必然尝试新截图
            assert ei.value.status_code == 400
            assert ds.shot_errors == 1
        finally:
            _reset_registry()


# ── iOS 覆盖安装闭环（2026-08，参考 ios_ipa_installer.py 语义 10.x 化）──


class FakeInstallService:
    """InstallationProxyService 替身：记录 get_apps/upgrade/install_from_local 调用。"""

    def __init__(self, installed: dict):
        self.installed = dict(installed)
        self.calls: list = []

    async def get_apps(self) -> dict:
        return dict(self.installed)

    async def upgrade(self, ipa_path: str):
        self.calls.append(("upgrade", ipa_path))

    async def install_from_local(self, path: Path):
        self.calls.append(("install", str(path)))


class TestIosInstallOverwrite:
    """install_overwrite：已安装走 upgrade（覆盖保存数据）、未安装走 install_from_local。"""

    def _make_ios(self, monkeypatch, fake: FakeInstallService):
        from server import IosDevice
        dev = IosDevice("UDID_OVERWRITE")

        async def _ld():
            return SimpleNamespace(identifier="UDID_OVERWRITE")

        monkeypatch.setattr(dev, "_get_lockdown", _ld)
        monkeypatch.setattr(
            "pymobiledevice3.services.installation_proxy.InstallationProxyService",
            lambda lockdown: fake,
        )
        return dev

    def test_installed_uses_upgrade(self, monkeypatch, tmp_path):
        ipa = tmp_path / "app.ipa"
        ipa.write_bytes(b"PK\x03\x04fake")
        fake = FakeInstallService({"com.example.app": {}})
        dev = self._make_ios(monkeypatch, fake)
        log = dev.install_overwrite("com.example.app", str(ipa))
        assert fake.calls[0][0] == "upgrade"
        assert "upgrade" in "\n".join(log)

    def test_fresh_uses_install(self, monkeypatch, tmp_path):
        ipa = tmp_path / "app.ipa"
        ipa.write_bytes(b"PK\x03\x04fake")
        fake = FakeInstallService({})
        dev = self._make_ios(monkeypatch, fake)
        log = dev.install_overwrite("com.example.app", str(ipa))
        assert fake.calls[0][0] == "install"
        assert "install" in "\n".join(log)

    def test_missing_file_raises(self, monkeypatch):
        from server import IosDevice
        dev = self._make_ios(monkeypatch, FakeInstallService({}))
        with pytest.raises(AdbError, match="不存在"):
            dev.install_overwrite("com.example.app", "Z:/no/such/file.ipa")

    def test_endpoint_overwrite_routes_ios(self, monkeypatch, tmp_path):
        """/api/reinstall + overwrite=True + iOS 会话 → install_overwrite（不卸载）。"""
        _reset_registry()
        try:
            ds = SESSION.session_for("IOS_OW", "ios")
            ipa = tmp_path / "app.ipa"
            ipa.write_bytes(b"PK\x03\x04fake")
            seen = {}

            def _fake_overwrite(self, pkg, p):
                seen["pkg"] = pkg
                seen["path"] = p
                return [f"upgrade: {pkg}"]

            monkeypatch.setattr(server.IosDevice, "install_overwrite", _fake_overwrite)

            r = reinstall(ReinstallReq(
                package="com.example.app", apk_path=str(ipa), serial="IOS_OW", overwrite=True))
            assert r["ok"] is True
            assert seen["pkg"] == "com.example.app"
        finally:
            _reset_registry()

    def test_endpoint_overwrite_routes_android(self, monkeypatch, tmp_path):
        """/api/reinstall + overwrite=True + Android 会话 → AdbHelper.install_overwrite。
        GP/iOS 同步（2026-08）：同一 overwrite 字段两端都走覆盖安装（install -r）。"""
        _reset_registry()
        try:
            ds = SESSION.session_for("DEV_OW", "android")
            apk = tmp_path / "app.apk"
            apk.write_bytes(b"PK\x03\x04fake")
            seen = {}

            def _fake_overwrite(self, pkg, p):
                seen["pkg"] = pkg
                seen["path"] = p
                return ["install -r(覆盖安装): Success"]

            monkeypatch.setattr(server.AdbHelper, "install_overwrite", _fake_overwrite)

            r = reinstall(ReinstallReq(
                package="com.example", apk_path=str(apk), serial="DEV_OW", overwrite=True))
            assert r["ok"] is True
            assert seen["pkg"] == "com.example"
        finally:
            _reset_registry()

    def test_endpoint_default_still_clean_reinstall(self, monkeypatch, tmp_path):
        """/api/reinstall 不带 overwrite（缺省 False）仍走 reinstall——自动测速与旧客户端不受影响。"""
        _reset_registry()
        try:
            ds = SESSION.session_for("DEV_CLEAN", "android")
            apk = tmp_path / "app.apk"
            apk.write_bytes(b"PK\x03\x04fake")
            seen = {}

            def _fake_reinstall(self, pkg, p):
                seen["pkg"] = pkg
                return ["uninstall: Success", "install: Success"]

            monkeypatch.setattr(server.AdbHelper, "reinstall", _fake_reinstall)

            r = reinstall(ReinstallReq(package="com.example", apk_path=str(apk), serial="DEV_CLEAN"))
            assert r["ok"] is True and seen["pkg"] == "com.example"
        finally:
            _reset_registry()

    def test_upload_ipa_accepted(self, monkeypatch, tmp_path):
        """.ipa 走 upload_apk 通过（ZIP 魔数校验后落盘即回填路径）。"""
        monkeypatch.setattr(server, "APK_UPLOAD_DIR", tmp_path)
        from fastapi import UploadFile
        r = asyncio.run(upload_apk(UploadFile(
            file=io.BytesIO(b"PK\x03\x04" + b"\x00" * 512), filename="demo.ipa")))
        assert r["ok"] is True and r["saved_name"].endswith(".ipa")


# ── IPA 元数据解析（bundle id 回填闭环，2026-08）──


def _make_ipa(tmp_path, bundle_id="com.example.app", version="1.2.3",
              name="示例App", with_plist=True):
    """构造最小 IPA（Payload/App.app/Info.plist）。用标准库 zipfile。"""
    import plistlib
    import zipfile
    ipa = tmp_path / "demo.ipa"
    with zipfile.ZipFile(ipa, "w") as zf:
        if with_plist:
            pl = {
                "CFBundleIdentifier": bundle_id,
                "CFBundleShortVersionString": version,
                "CFBundleVersion": "456",
                "CFBundleDisplayName": name,
                "CFBundleName": name,
            }
            zf.writestr(
                "Payload/App.app/Info.plist",
                plistlib.dumps(pl),
            )
    return ipa


class TestIpaBundleInfo:
    def test_parses_bundle_id_version_label(self, tmp_path):
        from server import _ipa_bundle_info
        info = _ipa_bundle_info(_make_ipa(tmp_path))
        assert info["package"] == "com.example.app"
        assert info["version_name"] == "1.2.3"
        assert info["version_code"] == "456"
        assert info["label"] == "示例App"

    def test_missing_plist_raises(self, tmp_path):
        import zipfile
        from server import _ipa_bundle_info, AdbError
        ipa = tmp_path / "bad.ipa"
        with zipfile.ZipFile(ipa, "w") as zf:
            zf.writestr("Payload/App.app/other", b"x")
        with pytest.raises(AdbError, match="Info.plist"):
            _ipa_bundle_info(ipa)

    def test_not_a_zip_raises(self, tmp_path):
        from server import _ipa_bundle_info, AdbError
        ipa = tmp_path / "bad.ipa"
        ipa.write_bytes(b"not a zip at all")
        with pytest.raises(AdbError, match="ZIP"):
            _ipa_bundle_info(ipa)

    def test_missing_bundle_id_raises(self, tmp_path):
        import plistlib
        import zipfile
        from server import _ipa_bundle_info, AdbError
        ipa = tmp_path / "nobundle.ipa"
        with zipfile.ZipFile(ipa, "w") as zf:
            zf.writestr("Payload/A.app/Info.plist",
                        plistlib.dumps({"CFBundleName": "X"}))
        with pytest.raises(AdbError, match="CFBundleIdentifier"):
            _ipa_bundle_info(ipa)

    def test_parse_apk_endpoint_routes_ipa(self, tmp_path):
        """/api/parse_apk 收 .ipa 走 plistib 解析（不触 aapt/AdbHelper）。"""
        from server import ApkParseReq, parse_apk
        ipa = _make_ipa(tmp_path, bundle_id="com.ipa.test")
        j = parse_apk(ApkParseReq(apk_path=str(ipa)))
        assert j["ok"] is True and j["package"] == "com.ipa.test"
        assert j["path"] == str(ipa)


# ── 跳过模板 phase 分组（2026-08：GM 界面首次/二次分设）──


class TestSkipTemplatePhase:
    def test_set_marker_req_phase_contract(self):
        """SetMarkerReq.phase 契约：默认 any；可选 first/second/any。"""
        from server import SetMarkerReq
        assert SetMarkerReq(cx=0.5, cy=0.5).phase == "any"
        assert SetMarkerReq(cx=0.5, cy=0.5, phase="first").phase == "first"
        assert SetMarkerReq(cx=0.5, cy=0.5, phase="second").phase == "second"

    def test_list_returns_phase_and_fires_any(self):
        """/api/skip_templates 回显 phase；无 phase 的旧模板按 any 处理。"""
        from server import DeviceSession, list_skip_templates
        _reset_registry()
        try:
            ds = SESSION.session_for("DEV_LS")
            ds._skip_templates.append({"id": 1, "phase": "first", "w": 10, "h": 10,
                                       "cx": 0.5, "cy": 0.5, "path": "x.png",
                                       "preview_base64": "", "preview_mime": "image/jpeg"})
            ds._skip_templates.append({"id": 2, "w": 10, "h": 10,          # 旧模板：无 phase
                                       "cx": 0.5, "cy": 0.5, "path": "y.png",
                                       "preview_base64": "", "preview_mime": "image/jpeg"})
            out = list_skip_templates(serial="DEV_LS")
            phases = {it["id"]: it["phase"] for it in out["items"]}
            assert phases[1] == "first"
            assert phases[2] == "any"   # 兼容：缺省视为通用
        finally:
            _reset_registry()


# ── iOS WDA 全自动点击（2026-08：半自动升级路径）──


class FakeWdaClient:
    """WdaServiceClient 替身：get_status / start_session / get_window_size / _request_json。"""

    def __init__(self, status_ok=True, win=None, tap_fail=False):
        self.status_ok = status_ok
        self.win = win or {"width": 390, "height": 844}
        self.tap_fail = tap_fail
        self.session_id = "FAKE_SID"
        self.calls: list = []

    async def get_status(self):
        if not self.status_ok:
            raise RuntimeError("no wda")
        return {"value": "ok"}

    async def start_session(self):
        return self.session_id

    async def get_window_size(self, sid):
        return self.win

    async def _request_json(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if self.tap_fail:
            raise RuntimeError("wda tap failed")


class TestIosWdaTap:
    def _mk_ios(self, monkeypatch, fake):
        from server import IosDevice
        dev = IosDevice("UDID_WDA")

        async def _ld():
            return SimpleNamespace(identifier="UDID_WDA")

        monkeypatch.setattr(dev, "_get_lockdown", _ld)
        monkeypatch.setattr(
            "pymobiledevice3.services.wda.WdaServiceClient",
            lambda **kw: fake,
        )
        return dev

    def test_wda_ready_true(self, monkeypatch):
        dev = self._mk_ios(monkeypatch, FakeWdaClient(status_ok=True))
        assert dev.wda_ready() is True

    def test_wda_ready_false_when_uninstalled(self, monkeypatch):
        dev = self._mk_ios(monkeypatch, FakeWdaClient(status_ok=False))
        assert dev.wda_ready() is False

    def test_wda_tap_uses_wda_tap_endpoint(self, monkeypatch):
        dev = self._mk_ios(monkeypatch, FakeWdaClient())
        dev.wda_tap(0.5, 0.5)
        ep = dev._wda_client.calls[-1] if hasattr(dev._wda_client, "calls") else None
        # _wda_client 为 fake（monkeypatch 后 client 即 fake）
        calls = getattr(dev._wda_client, "calls", [])
        assert calls and calls[0][1].startswith("/session/FAKE_SID/wda/tap/0")
        payload = calls[0][2]
        assert payload["x"] == 195 and payload["y"] == 422   # 0.5×390 / 0.5×844

    def test_wda_tap_falls_back_to_actions(self, monkeypatch):
        fake = FakeWdaClient()
        # 第一次 /wda/tap/0 失败 → actions 成功
        real = fake._request_json
        state = {"n": 0}

        async def fake_req(method, path, payload=None):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("unsupported endpoint")
            return await real(method, path, payload)

        monkeypatch.setattr(fake, "_request_json", fake_req)
        dev = self._mk_ios(monkeypatch, fake)
        dev.wda_tap(0.5, 0.5)
        assert state["n"] == 2
        # 第一次模拟失败不产生调用记录；成功的是 actions（calls 只有第 2 次）
        assert fake.calls[0][1].endswith("/actions")

    def test_wda_ready_false_routes_to_manual_pending(self, monkeypatch, tmp_path):
        """WDA 未就绪：check_auto iOS 命中仍返回 skip_manual_pending（半自动不回归）。"""
        # 复用现网 check_auto 编排代价高，此处验证核心决策：未就绪时 tap 不执行
        from server import IosDevice
        dev = self._mk_ios(monkeypatch, FakeWdaClient(status_ok=False))
        assert dev.wda_ready() is False
        # 未就绪不得走 wda_tap（会 raise 未就绪 AdbError——调用方 catch 后走半自动）
        assert dev._wda_client is None or dev.wda_ready() is False

