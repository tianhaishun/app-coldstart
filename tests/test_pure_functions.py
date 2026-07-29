"""server.py 纯函数单元测试。

运行：.venv\\Scripts\\python.exe -m pytest tests/ -v

测试范围（AGENTS.md §1.4「改完必须验证」的自动化基础）：
  - _safe_apk_filename：APK 文件名安全过滤（路径穿越防御，安全关键代码）
  - _safe_project_id：项目 id 安全过滤（路径穿越防御）
  - _raw_screencap_to_bgr：raw screencap 缓冲 → BGR 解析（自动测速热路径）
"""

import re
import struct
import sys
from pathlib import Path

# 确保能 import server（pytest 默认只把 tests/ 加进 sys.path，需手动加项目根）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from server import (  # noqa: E402
    _safe_apk_filename,
    _safe_project_id,
    _raw_screencap_to_bgr,
    AdbError,
)


# ── _safe_apk_filename ──────────────────────────────────────────


class TestSafeApkFilename:
    """APK 文件名安全过滤：路径穿越、中文/特殊字符、空输入、后缀强制、同名冲突。"""

    def test_normal_name(self, monkeypatch, tmp_path):
        """正常 APK 文件名原样保留。"""
        monkeypatch.setattr("server.APK_UPLOAD_DIR", tmp_path)
        assert _safe_apk_filename("myapp.apk") == "myapp.apk"

    def test_unix_path_traversal(self, monkeypatch, tmp_path):
        """Unix 路径穿越：只取 basename，目录分隔符被丢弃。"""
        monkeypatch.setattr("server.APK_UPLOAD_DIR", tmp_path)
        result = _safe_apk_filename("../../evil.apk")
        assert result == "evil.apk"
        assert "/" not in result
        assert ".." not in result

    def test_windows_path_traversal(self, monkeypatch, tmp_path):
        """Windows 路径穿越：反斜杠分隔符同样被 basename 去掉。"""
        monkeypatch.setattr("server.APK_UPLOAD_DIR", tmp_path)
        result = _safe_apk_filename("..\\..\\evil.apk")
        assert "\\" not in result
        assert result == "evil.apk"

    def test_chinese_chars_replaced(self, monkeypatch, tmp_path):
        """中文字符替换为下划线（混合 ASCII，不触发兜底）。"""
        monkeypatch.setattr("server.APK_UPLOAD_DIR", tmp_path)
        # "app测试" → safe_stem="app__"（中文变_，ASCII 保留），非全下划线不触发兜底
        result = _safe_apk_filename("app测试.apk")
        assert result == "app__.apk"

    def test_spaces_replaced(self, monkeypatch, tmp_path):
        """空格替换为下划线。"""
        monkeypatch.setattr("server.APK_UPLOAD_DIR", tmp_path)
        assert _safe_apk_filename("my app.apk") == "my_app.apk"

    def test_force_apk_extension(self, monkeypatch, tmp_path):
        """非 .apk 后缀也强制追加 .apk。"""
        monkeypatch.setattr("server.APK_UPLOAD_DIR", tmp_path)
        result = _safe_apk_filename("test.txt")
        assert result.endswith(".apk")
        assert result == "test.txt.apk"

    def test_empty_input(self, monkeypatch, tmp_path):
        """空字符串走兜底名。"""
        monkeypatch.setattr("server.APK_UPLOAD_DIR", tmp_path)
        assert _safe_apk_filename("") == "apk_upload.apk"

    def test_none_input(self, monkeypatch, tmp_path):
        """None 输入走兜底名。"""
        monkeypatch.setattr("server.APK_UPLOAD_DIR", tmp_path)
        assert _safe_apk_filename(None) == "apk_upload.apk"

    def test_leading_dot_stripped(self, monkeypatch, tmp_path):
        """开头点被去掉（防隐藏文件）。"""
        monkeypatch.setattr("server.APK_UPLOAD_DIR", tmp_path)
        result = _safe_apk_filename(".hidden.apk")
        assert not result.startswith(".")
        assert result == "hidden.apk"

    def test_all_underscores_fallback(self, monkeypatch, tmp_path):
        """过滤后全是下划线 → 兜底名。"""
        monkeypatch.setattr("server.APK_UPLOAD_DIR", tmp_path)
        # "测试" 两个字都变成 _，set("__")=={"_"} 触发兜底
        result = _safe_apk_filename("测试.apk")
        assert result == "apk_upload.apk"

    def test_name_conflict_adds_hash(self, monkeypatch, tmp_path):
        """同名文件已存在时追加 hash 后缀，不覆盖。"""
        monkeypatch.setattr("server.APK_UPLOAD_DIR", tmp_path)
        (tmp_path / "myapp.apk").write_bytes(b"")
        result = _safe_apk_filename("myapp.apk")
        assert result.startswith("myapp_")
        assert result.endswith(".apk")
        assert result != "myapp.apk"

    def test_result_only_safe_chars(self, monkeypatch, tmp_path):
        """结果只含 [A-Za-z0-9_\\-.]。"""
        monkeypatch.setattr("server.APK_UPLOAD_DIR", tmp_path)
        result = _safe_apk_filename("app@#$%^&*().apk")
        assert re.match(r"^[A-Za-z0-9_\-.]+$", result)


# ── _safe_project_id ────────────────────────────────────────────


class TestSafeProjectId:
    """项目 id 安全过滤：路径穿越防御、非法字符过滤。"""

    def test_normal_id(self):
        assert _safe_project_id("proj_abc123") == "proj_abc123"

    def test_strips_unsafe_chars(self):
        """只保留 [A-Za-z0-9_-]，其余丢弃。"""
        assert _safe_project_id("pro/ject") == "project"
        assert _safe_project_id("a..b") == "ab"

    def test_path_traversal_filtered(self):
        """路径穿越字符（/ .）全部被过滤掉。"""
        result = _safe_project_id("../../../etc/passwd")
        assert "/" not in result
        assert "." not in result
        assert result == "etcpasswd"

    def test_empty_raises(self):
        with pytest.raises(AdbError):
            _safe_project_id("")

    def test_only_dots_raises(self):
        """全是点（过滤后为空）应抛错。"""
        with pytest.raises(AdbError):
            _safe_project_id("...")

    def test_only_slashes_raises(self):
        """全是斜杠（过滤后为空）应抛错。"""
        with pytest.raises(AdbError):
            _safe_project_id("///")

    def test_none_raises(self):
        with pytest.raises(AdbError):
            _safe_project_id(None)


# ── _raw_screencap_to_bgr ───────────────────────────────────────


def _make_raw_header(w: int, h: int, fmt: int, header_size: int = 12) -> bytes:
    """构造 raw screencap 头部。

    header_size=12（老设备：w,h,fmt）或 16（新设备含 Pixel：多一个 colorspace 字段）。
    """
    header = struct.pack("<III", w, h, fmt)
    if header_size == 16:
        header += struct.pack("<I", 0)  # colorspace 占位
    return header


class TestRawScreencapToBgr:
    """raw screencap 缓冲 → BGR ndarray 解析：像素格式转换、头部长度、异常输入。"""

    def test_rgba_12byte_header(self):
        """RGBA_8888（fmt=1），12 字节头，R/G/B 通道正确翻转成 BGR。

        像素布局（row-major）：(0,0)=红 (0,1)=绿 (1,0)=蓝 (1,1)=白
        RGBA → BGR = 通道序 [2,1,0] 翻转
        """
        w, h = 2, 2
        rgba = bytes([
            255, 0,   0,   255,  # 红 RGBA
            0,   255, 0,   255,  # 绿
            0,   0,   255, 255,  # 蓝
            255, 255, 255, 255,  # 白
        ])
        raw = _make_raw_header(w, h, fmt=1, header_size=12) + rgba
        bgr = _raw_screencap_to_bgr(raw)

        assert bgr.shape == (h, w, 3)
        assert tuple(bgr[0, 0]) == (0, 0, 255)      # 红 → BGR
        assert tuple(bgr[0, 1]) == (0, 255, 0)      # 绿 → BGR
        assert tuple(bgr[1, 0]) == (255, 0, 0)      # 蓝 → BGR
        assert tuple(bgr[1, 1]) == (255, 255, 255)  # 白 → BGR

    def test_rgba_16byte_header(self):
        """新设备 16 字节头（多 colorspace 字段）也能正确解析。"""
        w, h = 1, 1
        rgba = bytes([10, 20, 30, 255])
        raw = _make_raw_header(w, h, fmt=1, header_size=16) + rgba
        bgr = _raw_screencap_to_bgr(raw)
        # RGBA(10,20,30) → BGR=(30,20,10)
        assert tuple(bgr[0, 0]) == (30, 20, 10)

    def test_bgra_format(self):
        """BGRA_8888（fmt=5）：前 3 字节就是 BGR，直接取 [:3]。"""
        w, h = 1, 1
        bgra = bytes([100, 150, 200, 255])  # B=100 G=150 R=200
        raw = _make_raw_header(w, h, fmt=5, header_size=12) + bgra
        bgr = _raw_screencap_to_bgr(raw)
        assert tuple(bgr[0, 0]) == (100, 150, 200)

    def test_rgbx_format(self):
        """RGBX_8888（fmt=2）与 RGBA 同处理（X=不透明 padding，通道翻转一致）。"""
        w, h = 1, 1
        rgbx = bytes([255, 128, 64, 0])
        raw = _make_raw_header(w, h, fmt=2, header_size=12) + rgbx
        bgr = _raw_screencap_to_bgr(raw)
        # RGBX(255,128,64) → BGR=(64,128,255)
        assert tuple(bgr[0, 0]) == (64, 128, 255)

    def test_too_short_raises(self):
        """缓冲过短（< 12 字节）抛异常。"""
        with pytest.raises(AdbError):
            _raw_screencap_to_bgr(b"\x00" * 8)

    def test_zero_dimensions_raises(self):
        """尺寸为 0 抛异常。"""
        raw = struct.pack("<III", 0, 100, 1) + b"\x00" * 100
        with pytest.raises(AdbError):
            _raw_screencap_to_bgr(raw)

    def test_oversized_dimensions_raises(self):
        """尺寸超限（> 10000）抛异常。"""
        raw = struct.pack("<III", 99999, 1, 1) + b"\x00" * 4
        with pytest.raises(AdbError):
            _raw_screencap_to_bgr(raw)

    def test_unsupported_format_raises(self):
        """不支持的像素格式（fmt 不在 1/2/5）抛异常。"""
        w, h = 1, 1
        raw = struct.pack("<III", w, h, 99) + b"\x00" * 4
        with pytest.raises(AdbError):
            _raw_screencap_to_bgr(raw)

    def test_length_mismatch_raises(self):
        """长度既不匹配 12+need 也不匹配 16+need 时抛异常。"""
        w, h = 2, 2
        raw = struct.pack("<III", w, h, 1) + b"\x00" * 10  # need=16，给 10
        with pytest.raises(AdbError):
            _raw_screencap_to_bgr(raw)

    def test_larger_image_reshape(self):
        """4x4 图像验证 reshape 维度正确性。"""
        w, h = 4, 4
        rgba = bytes(range(w * h * 4))  # 0..63
        raw = _make_raw_header(w, h, fmt=1, header_size=12) + rgba
        bgr = _raw_screencap_to_bgr(raw)
        assert bgr.shape == (h, w, 3)
        # 像素(0,0) RGBA=(0,1,2,3) → BGR=(2,1,0)
        assert tuple(bgr[0, 0]) == (2, 1, 0)
