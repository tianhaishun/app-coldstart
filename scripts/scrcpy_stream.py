"""Reusable scrcpy 4.x video stream protocol and frame matcher helpers."""

from __future__ import annotations

import socket
import struct
import time
from typing import Any


DEVICE_NAME_LENGTH = 64
PACKET_HEADER_SIZE = 12
CONFIG_FLAG = 1 << 62
KEY_FRAME_FLAG = 1 << 61
PTS_MASK = KEY_FRAME_FLAG - 1
H264_CODEC_ID = 0x68323634


class ProbeError(RuntimeError):
    """A recoverable scrcpy stream or matcher error."""


class MarkerMatcher:
    """OpenCV ROI matcher for one decoded scrcpy frame."""

    def __init__(
        self,
        template: Any,
        cx: float,
        cy: float,
        padding: int = 20,
        threshold: float = 0.85,
    ):
        import cv2

        if template is None or len(template.shape) < 2:
            raise ProbeError("启动模板为空或不是有效图像")
        if not (0.0 <= cx <= 1.0 and 0.0 <= cy <= 1.0):
            raise ProbeError(f"启动模板坐标无效：cx={cx} cy={cy}")
        if float(template.std()) < 15.0:
            raise ProbeError("启动模板几乎是纯色（灰度标准差 < 15）")
        self.cv2 = cv2
        self.template = template
        self.cx, self.cy = float(cx), float(cy)
        self.padding, self.threshold = int(padding), float(threshold)
        self.template_height, self.template_width = template.shape[:2]

    @classmethod
    def from_file(
        cls,
        path: str,
        cx: float,
        cy: float,
        padding: int = 20,
        threshold: float = 0.85,
    ) -> "MarkerMatcher":
        import cv2

        template = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if template is None:
            raise ProbeError(f"无法读取启动模板：{path}")
        return cls(template, cx, cy, padding, threshold)

    def match(self, frame: Any) -> dict[str, Any]:
        started = time.perf_counter()
        frame_height, frame_width = frame.shape[:2]
        cx_px, cy_px = int(self.cx * frame_width), int(self.cy * frame_height)
        sx1 = max(0, cx_px - self.template_width // 2 - self.padding)
        sy1 = max(0, cy_px - self.template_height // 2 - self.padding)
        sx2 = min(frame_width, cx_px + self.template_width // 2 + self.padding)
        sy2 = min(frame_height, cy_px + self.template_height // 2 + self.padding)
        full_frame = False
        if sx2 - sx1 < self.template_width or sy2 - sy1 < self.template_height:
            sx1, sy1, sx2, sy2 = 0, 0, frame_width, frame_height
            full_frame = True
        roi = frame[sy1:sy2, sx1:sx2]
        if roi.shape[0] < self.template_height or roi.shape[1] < self.template_width:
            return {
                "confidence": 0.0,
                "hit": False,
                "match_ms": round((time.perf_counter() - started) * 1000, 3),
                "roi_width": int(roi.shape[1]),
                "roi_height": int(roi.shape[0]),
                "full_frame_fallback": full_frame,
            }
        result = self.cv2.matchTemplate(roi, self.template, self.cv2.TM_CCOEFF_NORMED)
        _, confidence, _, _ = self.cv2.minMaxLoc(result)
        return {
            "confidence": round(float(confidence), 4),
            "hit": bool(confidence >= self.threshold),
            "match_ms": round((time.perf_counter() - started) * 1000, 3),
            "roi_width": int(roi.shape[1]),
            "roi_height": int(roi.shape[0]),
            "full_frame_fallback": full_frame,
        }


class BufferedStream:
    """Resumable exact reader over a socket.

    Partially received bytes are kept in an internal buffer, so a
    socket.timeout in the middle of a packet never loses sync: the next
    read continues from where the previous one stopped.
    """

    def __init__(self, sock: Any):
        self.sock = sock
        self._buffer = bytearray()

    def read_exact(self, size: int) -> bytes:
        while len(self._buffer) < size:
            chunk = self.sock.recv(64 * 1024)
            if not chunk:
                raise ProbeError("scrcpy 视频 socket 提前断开")
            self._buffer.extend(chunk)
        out = bytes(self._buffer[:size])
        del self._buffer[:size]
        return out

    def close(self) -> None:
        self.sock.close()


def read_exact(sock: Any, size: int) -> bytes:
    """Read exactly ``size`` bytes from a recv-compatible object."""

    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ProbeError("scrcpy 视频 socket 提前断开")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_stream_header(stream: Any) -> tuple[str, int, int]:
    device_name = stream.read_exact(DEVICE_NAME_LENGTH).rstrip(b"\x00").decode("utf-8", "replace")
    if not device_name:
        raise ProbeError("scrcpy 设备名为空")
    codec_id = struct.unpack(">I", stream.read_exact(4))[0]
    if codec_id != H264_CODEC_ID:
        raise ProbeError(f"期望 H264 codec id，收到 0x{codec_id:08x}")
    return device_name, codec_id, 0


def read_session(stream: Any) -> tuple[int, int]:
    header = stream.read_exact(PACKET_HEADER_SIZE)
    if not (header[0] & 0x80):
        raise ProbeError("scrcpy session header 缺失")
    return struct.unpack(">II", header[4:12])


def read_video_packet(stream: Any) -> tuple[int, bytes, bool, tuple[int, int] | None]:
    header = stream.read_exact(PACKET_HEADER_SIZE)
    if struct.unpack(">I", header[:4])[0] & 0x80000000:
        return -1, b"", False, struct.unpack(">II", header[4:12])
    pts_flags, size = struct.unpack(">QI", header)
    if size <= 0 or size > 64 * 1024 * 1024:
        raise ProbeError(f"非法视频 packet size={size}")
    payload = stream.read_exact(size)
    if pts_flags & CONFIG_FLAG:
        return -1, payload, False, None
    return pts_flags & PTS_MASK, payload, bool(pts_flags & KEY_FRAME_FLAG), None


def read_packet(stream: Any) -> tuple[int, bytes, bool]:
    pts, payload, key, session = read_video_packet(stream)
    if session is not None:
        raise ProbeError("读取到 session packet，不能当作媒体 packet")
    return pts, payload, key


def process_packet(
    codec: Any,
    payload: bytes,
    pts_us: int,
    frame_index: int,
    samples: list[dict[str, Any]],
    received_at: float,
    matcher: MarkerMatcher | None = None,
) -> int:
    decoded_at = time.perf_counter()
    for packet in codec.parse(payload):
        if pts_us >= 0:
            packet.pts = packet.dts = pts_us
        for frame in codec.decode(packet):
            bgr = frame.to_ndarray(format="bgr24")
            frame_index += 1
            sample = {
                "index": frame_index,
                "pts_us": pts_us,
                "received_at": received_at,
                "decoded_at": time.perf_counter(),
                "decode_to_bgr_ms": round((time.perf_counter() - decoded_at) * 1000, 3),
                "width": int(bgr.shape[1]),
                "height": int(bgr.shape[0]),
            }
            if matcher is not None:
                sample["match"] = matcher.match(bgr)
            samples.append(sample)
    return frame_index
