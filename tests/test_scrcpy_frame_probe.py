"""Pure protocol/statistics/matcher tests for the scrcpy frame POC.

Device integration remains a separate manual acceptance step.
"""

from __future__ import annotations

import io
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.scrcpy_frame_probe import (  # noqa: E402
    CONFIG_FLAG,
    H264_CODEC_ID,
    KEY_FRAME_FLAG,
    MarkerMatcher,
    ProbeError,
    read_exact,
    read_packet,
    read_session,
    read_stream_header,
    summarize,
)


class FragmentedSocket:
    def __init__(self, data: bytes, chunk_size: int = 2):
        self._data = io.BytesIO(data)
        self._chunk_size = chunk_size

    def recv(self, size: int) -> bytes:
        return self._data.read(min(size, self._chunk_size))


def cv2_or_skip():
    return pytest.importorskip("cv2", reason="OpenCV is required for matcher tests")


def test_read_exact_handles_fragmented_socket():
    assert read_exact(FragmentedSocket(b"abcdef", chunk_size=2), 6) == b"abcdef"


def test_read_exact_rejects_early_close():
    with pytest.raises(ProbeError, match="提前断开"):
        read_exact(FragmentedSocket(b"abc"), 4)


def test_read_stream_header():
    payload = b"Pixel 6a".ljust(64, b"\x00") + struct.pack(">III", H264_CODEC_ID, 1080, 2400)
    name, codec_id, unused = read_stream_header(FragmentedSocket(payload, chunk_size=7))
    assert name == "Pixel 6a"
    assert codec_id == H264_CODEC_ID
    assert unused == 0
    width, height = read_session(FragmentedSocket(struct.pack(">III", 0x80000000, 1080, 2400), chunk_size=7))
    assert (width, height) == (1080, 2400)


def test_read_stream_header_rejects_wrong_codec():
    payload = b"device".ljust(64, b"\x00") + struct.pack(">III", 0, 1, 1)
    with pytest.raises(ProbeError, match="H264"):
        read_stream_header(FragmentedSocket(payload, chunk_size=64))


def test_read_session_rejects_media_packet():
    with pytest.raises(ProbeError, match="session"):
        read_session(FragmentedSocket(struct.pack(">QI", 123, 1) + b"x"))


def test_read_packet_extracts_pts_and_key_flag():
    flags = 123456 | KEY_FRAME_FLAG
    payload = b"h264-packet"
    raw = struct.pack(">QI", flags, len(payload)) + payload
    pts, actual, key = read_packet(FragmentedSocket(raw, chunk_size=3))
    assert pts == 123456
    assert actual == payload
    assert key is True


def test_read_packet_extracts_config_packet():
    payload = b"codec-config"
    raw = struct.pack(">QI", CONFIG_FLAG, len(payload)) + payload
    pts, actual, key = read_packet(FragmentedSocket(raw, chunk_size=4))
    assert pts == -1
    assert actual == payload
    assert key is False


def test_marker_matcher_matches_template_in_roi():
    cv2 = cv2_or_skip()
    import numpy as np

    frame = np.zeros((240, 320, 3), dtype=np.uint8)
    template = np.zeros((40, 60, 3), dtype=np.uint8)
    cv2.rectangle(template, (5, 5), (54, 34), (30, 180, 240), -1)
    frame[100:140, 130:190] = template
    matcher = MarkerMatcher(template, 0.5, 0.5, padding=20, threshold=0.85)
    result = matcher.match(frame)
    assert result["hit"] is True
    assert result["confidence"] >= 0.99
    assert result["roi_width"] == 100
    assert result["roi_height"] == 80
    assert result["full_frame_fallback"] is False


def test_marker_matcher_rejects_pure_color_template():
    cv2_or_skip()
    import numpy as np

    with pytest.raises(ProbeError, match="纯色"):
        MarkerMatcher(np.full((20, 20, 3), 100, dtype=np.uint8), 0.5, 0.5)


def test_marker_matcher_uses_full_frame_when_roi_is_too_small():
    cv2_or_skip()
    import numpy as np

    template = np.zeros((80, 80, 3), dtype=np.uint8)
    template[10:70, 10:70] = (20, 100, 220)
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    frame[10:90, 10:90] = template
    matcher = MarkerMatcher(template, 0.02, 0.02, padding=20, threshold=0.8)
    result = matcher.match(frame)
    assert result["full_frame_fallback"] is True
    assert result["hit"] is True


def test_marker_matcher_returns_low_confidence_for_different_frame():
    cv2_or_skip()
    import numpy as np

    template = np.zeros((30, 40, 3), dtype=np.uint8)
    template[5:25, 5:35] = (20, 100, 220)
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    matcher = MarkerMatcher(template, 0.5, 0.5, padding=20, threshold=0.85)
    result = matcher.match(frame)
    assert result["hit"] is False
    assert result["confidence"] < 0.85


def test_summarize_reports_pts_and_receive_cadence():
    samples = [
        {"index": 1, "pts_us": 0, "received_at": 0.0, "decode_to_bgr_ms": 2.0},
        {"index": 2, "pts_us": 16667, "received_at": 0.017, "decode_to_bgr_ms": 3.0},
        {"index": 3, "pts_us": 33334, "received_at": 0.034, "decode_to_bgr_ms": 2.5},
    ]
    result = summarize(samples, 0.034)
    assert result["decoded_frames"] == 3
    assert result["pts_fps"] == 59.999
    assert result["receive_fps"] == 58.824
    assert result["pts_interval_ms"]["p50"] == 16.667
    assert result["decode_to_bgr_ms"]["p95"] == 3.0
