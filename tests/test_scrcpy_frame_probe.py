"""Pure protocol/statistics tests for the scrcpy frame POC.

These tests do not require an Android device, adb, PyAV, or a running scrcpy
server. Device integration remains a separate manual acceptance step.
"""

from __future__ import annotations

import io
import socket
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.scrcpy_frame_probe import (  # noqa: E402
    CONFIG_FLAG,
    H264_CODEC_ID,
    KEY_FRAME_FLAG,
    ProbeError,
    read_exact,
    read_packet,
    read_stream_header,
    summarize,
)


class FragmentedSocket:
    def __init__(self, data: bytes, chunk_size: int = 2):
        self._data = io.BytesIO(data)
        self._chunk_size = chunk_size

    def recv(self, size: int) -> bytes:
        return self._data.read(min(size, self._chunk_size))


def test_read_exact_handles_fragmented_socket():
    assert read_exact(FragmentedSocket(b"abcdef", chunk_size=2), 6) == b"abcdef"


def test_read_exact_rejects_early_close():
    try:
        read_exact(FragmentedSocket(b"abc"), 4)
    except ProbeError as exc:
        assert "提前断开" in str(exc)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("expected ProbeError")


def test_read_stream_header():
    payload = (
        b"\x00"
        + b"Pixel 6a".ljust(64, b"\x00")
        + struct.pack(">III", H264_CODEC_ID, 1080, 2400)
    )
    name, width, height = read_stream_header(FragmentedSocket(payload, chunk_size=7))
    assert name == "Pixel 6a"
    assert (width, height) == (1080, 2400)


def test_read_stream_header_rejects_wrong_codec():
    payload = b"\x00" + b"device".ljust(64, b"\x00") + struct.pack(">III", 0, 1, 1)
    try:
        read_stream_header(FragmentedSocket(payload, chunk_size=64))
    except ProbeError as exc:
        assert "H264" in str(exc)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("expected ProbeError")


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
