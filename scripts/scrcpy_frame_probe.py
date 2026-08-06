"""scrcpy video-frame cadence and OpenCV matcher POC."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

DEFAULT_SCRCPY_VERSION = "4.0"
SOCKET_PREFIX = "scrcpy_"
DEVICE_NAME_LENGTH = 64
PACKET_HEADER_SIZE = 12
SESSION_FLAG = 1 << 63
CONFIG_FLAG = 1 << 62
KEY_FRAME_FLAG = 1 << 61
PTS_MASK = KEY_FRAME_FLAG - 1
H264_CODEC_ID = 0x68323634
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ProbeError(RuntimeError):
    """A recoverable POC setup or stream error."""


class MarkerMatcher:
    """OpenCV ROI matcher for one decoded scrcpy frame."""

    def __init__(self, template: Any, cx: float, cy: float, padding: int = 20, threshold: float = 0.85):
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
    def from_file(cls, path: str | Path, cx: float, cy: float, padding: int = 20, threshold: float = 0.85):
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


def read_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ProbeError("scrcpy 视频 socket 提前断开")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def resolve_adb_path(value: str | None) -> str:
    candidates = [value] if value else []
    if os.environ.get("ADB"):
        candidates.append(os.environ["ADB"])
    candidates.append(str(PROJECT_ROOT / ("adb/adb.exe" if os.name == "nt" else "adb/adb")))
    candidates.append("adb")
    for candidate in candidates:
        if Path(candidate).is_file():
            return str(Path(candidate))
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise ProbeError(f"找不到 adb：{value or 'PATH/项目目录'}")


def adb_run(adb_path: str, serial: str | None, args: list[str]) -> subprocess.CompletedProcess[bytes]:
    command = [adb_path] + (["-s", serial] if serial else []) + args
    return subprocess.run(command, capture_output=True, check=False)


def serial_from_args(serial: str | None, adb_path: str) -> str:
    if serial:
        result = adb_run(adb_path, serial, ["get-state"])
        if result.returncode != 0 or result.stdout.strip() != b"device":
            raise ProbeError(f"找不到设备 serial={serial}")
        return serial
    result = subprocess.run([adb_path, "devices"], capture_output=True, check=False)
    devices = [line.split()[0] for line in result.stdout.decode("utf-8", "replace").splitlines()[1:] if len(line.split()) >= 2 and line.split()[1] == "device"]
    if len(devices) != 1:
        raise ProbeError(f"请使用 --serial 指定设备，当前设备数={len(devices)}")
    return devices[0]


def choose_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def read_stream_header(sock: socket.socket) -> tuple[str, int, int]:
    device_name = read_exact(sock, DEVICE_NAME_LENGTH).rstrip(b"\x00").decode("utf-8", "replace")
    if not device_name:
        raise ProbeError("scrcpy 设备名为空")
    codec_id = struct.unpack(">I", read_exact(sock, 4))[0]
    if codec_id != H264_CODEC_ID:
        raise ProbeError(f"期望 H264 codec id，收到 0x{codec_id:08x}")
    return device_name, codec_id, 0


def read_session(sock: socket.socket) -> tuple[int, int]:
    header = read_exact(sock, PACKET_HEADER_SIZE)
    if not (header[0] & 0x80):
        raise ProbeError("scrcpy session header 缺失")
    return struct.unpack(">II", header[4:12])


def read_video_packet(sock: socket.socket) -> tuple[int, bytes, bool, tuple[int, int] | None]:
    header = read_exact(sock, PACKET_HEADER_SIZE)
    if struct.unpack(">I", header[:4])[0] & 0x80000000:
        return -1, b"", False, struct.unpack(">II", header[4:12])
    pts_flags, size = struct.unpack(">QI", header)
    if size <= 0 or size > 64 * 1024 * 1024:
        raise ProbeError(f"非法视频 packet size={size}")
    payload = read_exact(sock, size)
    if pts_flags & CONFIG_FLAG:
        return -1, payload, False, None
    return pts_flags & PTS_MASK, payload, bool(pts_flags & KEY_FRAME_FLAG), None


def read_packet(sock: socket.socket) -> tuple[int, bytes, bool]:
    pts, payload, key, session = read_video_packet(sock)
    if session is not None:
        raise ProbeError("读取到 session packet，不能当作媒体 packet")
    return pts, payload, key


def process_packet(codec: Any, payload: bytes, pts_us: int, frame_index: int, samples: list[dict[str, Any]], received_at: float, matcher: MarkerMatcher | None = None) -> int:
    decoded_at = time.perf_counter()
    for packet in codec.parse(payload):
        if pts_us >= 0:
            packet.pts = packet.dts = pts_us
        for frame in codec.decode(packet):
            bgr = frame.to_ndarray(format="bgr24")
            frame_index += 1
            sample = {
                "index": frame_index, "pts_us": pts_us, "received_at": received_at,
                "decoded_at": time.perf_counter(),
                "decode_to_bgr_ms": round((time.perf_counter() - decoded_at) * 1000, 3),
                "width": int(bgr.shape[1]), "height": int(bgr.shape[0]),
            }
            if matcher is not None:
                sample["match"] = matcher.match(bgr)
            samples.append(sample)
    return frame_index


def summarize(samples: list[dict[str, Any]], duration: float) -> dict[str, Any]:
    if len(samples) < 2:
        raise ProbeError(f"POC 在 {duration:.1f}s 内只解码 {len(samples)} 帧，无法统计帧间隔")
    pts_deltas = [(b["pts_us"] - a["pts_us"]) / 1000 for a, b in zip(samples, samples[1:]) if a["pts_us"] >= 0 and b["pts_us"] >= 0 and b["pts_us"] > a["pts_us"]]
    receive_deltas = [(b["received_at"] - a["received_at"]) * 1000 for a, b in zip(samples, samples[1:])]
    decode_ms = [item["decode_to_bgr_ms"] for item in samples]
    match_samples = [item["match"] for item in samples if "match" in item]
    match_times = [item["match_ms"] for item in match_samples]

    def percentile(values: list[float], p: float) -> float:
        ordered = sorted(values)
        return round(ordered[min(len(ordered) - 1, int(len(ordered) * p))], 3)

    def stats(values: list[float]) -> dict[str, float | None]:
        return {"p50": percentile(values, .5), "p95": percentile(values, .95), "max": round(max(values), 3)} if values else {"p50": None, "p95": None, "max": None}

    pts_duration_s = (samples[-1]["pts_us"] - samples[0]["pts_us"]) / 1_000_000
    receive_duration_s = samples[-1]["received_at"] - samples[0]["received_at"]
    return {
        "duration_s": round(duration, 3), "decoded_frames": len(samples),
        "pts_fps": round(len(pts_deltas) / pts_duration_s, 3) if pts_duration_s > 0 else None,
        "receive_fps": round(len(receive_deltas) / receive_duration_s, 3) if receive_duration_s > 0 else None,
        "pts_interval_ms": stats(pts_deltas), "receive_interval_ms": stats(receive_deltas),
        "decode_to_bgr_ms": stats(decode_ms), "marker_checked_frames": len(match_samples),
        "marker_hit_frames": sum(1 for item in match_samples if item["hit"]),
        "marker_match_ms": stats(match_times),
        "marker_best_confidence": round(max((item["confidence"] for item in match_samples), default=0.0), 4),
        "marker_full_frame_fallbacks": sum(1 for item in match_samples if item["full_frame_fallback"]),
        "first_pts_us": samples[0]["pts_us"], "last_pts_us": samples[-1]["pts_us"],
    }


def start_server(serial: str, adb_path: str, server_path: Path, scid: str, max_fps: int, max_size: int, bitrate: int, version: str) -> tuple[subprocess.Popen[bytes], deque[str]]:
    remote = "/data/local/tmp/scrcpy-frame-probe-server.jar"
    push = adb_run(adb_path, serial, ["push", str(server_path), remote])
    if push.returncode != 0:
        raise ProbeError(push.stderr.decode("utf-8", "replace").strip() or "adb push 失败")
    args = [adb_path, "-s", serial, "shell", "CLASSPATH=" + remote, "app_process", "/", "com.genymobile.scrcpy.Server", version, f"scid={scid}", "log_level=info", "video=true", "audio=false", "control=false", "cleanup=true", f"video_bit_rate={bitrate}", f"max_size={max_size}", f"max_fps={max_fps}", "video_codec=h264", "send_frame_meta=true", "send_codec_meta=true", "send_dummy_byte=true"]
    logs: deque[str] = deque(maxlen=80)
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    def drain() -> None:
        if proc.stdout:
            for line in proc.stdout:
                logs.append(line.decode("utf-8", "replace").rstrip())
    threading.Thread(target=drain, daemon=True).start()
    return proc, logs


def run_probe(args: argparse.Namespace) -> int:
    adb_path = resolve_adb_path(args.adb)
    serial = serial_from_args(args.serial, adb_path)
    server_path = Path(args.server).resolve()
    if not server_path.is_file():
        raise ProbeError(f"scrcpy-server 不存在：{server_path}")
    scid = f"{secrets.randbits(31):08x}"
    socket_name, local_port = SOCKET_PREFIX + scid, choose_local_port()
    listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen.bind(("127.0.0.1", local_port)); listen.listen(1); listen.settimeout(1)
    remote, local = f"localabstract:{socket_name}", f"tcp:{local_port}"
    reverse = adb_run(adb_path, serial, ["reverse", remote, local])
    if reverse.returncode != 0:
        listen.close(); raise ProbeError(reverse.stderr.decode("utf-8", "replace").strip() or "adb reverse 失败")
    proc: subprocess.Popen[bytes] | None = None; sock: socket.socket | None = None
    try:
        proc, logs = start_server(serial, adb_path, server_path, scid, args.max_fps, args.max_size, args.bitrate, args.scrcpy_version)
        deadline = time.monotonic() + 10
        while True:
            try:
                sock, _ = listen.accept(); break
            except socket.timeout:
                if time.monotonic() >= deadline: raise ProbeError(f"scrcpy 视频连接超时：{' | '.join(logs)[-1000:]}")
                if proc.poll() is not None: raise ProbeError(f"scrcpy-server 提前退出：{' | '.join(logs)[-1000:]}")
        sock.settimeout(None)
        device_name, _codec, _ = read_stream_header(sock)
        width, height = read_session(sock)
        print(f"device: {device_name} serial={serial} stream={width}x{height}")
        import av
        codec = av.CodecContext.create("h264", "r")
        matcher = MarkerMatcher.from_file(args.template, args.template_cx, args.template_cy, args.template_padding, args.template_threshold) if args.template else None
        samples: list[dict[str, Any]] = []; frame_index = 0; start = time.perf_counter()
        while time.perf_counter() < start + args.duration:
            received_at = time.perf_counter(); pts_us, payload, _key, session = read_video_packet(sock)
            if session is not None:
                width, height = session; continue
            frame_index = process_packet(codec, payload, pts_us, frame_index, samples, received_at, matcher)
            if matcher and samples and samples[-1].get("match", {}).get("hit"):
                print(json.dumps({"marker_hit": samples[-1]["match"], "frame": samples[-1]}, ensure_ascii=False))
                if args.stop_on_hit: break
        print(json.dumps(summarize(samples, time.perf_counter() - start), ensure_ascii=False, indent=2))
        return 0
    finally:
        listen.close()
        if sock: sock.close()
        subprocess.run([adb_path, "-s", serial, "reverse", "--remove", remote], capture_output=True, check=False)
        subprocess.run([adb_path, "-s", serial, "shell", "rm", "-f", "/data/local/tmp/scrcpy-frame-probe-server.jar"], capture_output=True, check=False)
        if proc and proc.poll() is None:
            proc.terminate()
            try: proc.wait(timeout=3)
            except subprocess.TimeoutExpired: proc.kill()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="scrcpy video frame cadence and OpenCV POC")
    parser.add_argument("--serial"); parser.add_argument("--adb"); parser.add_argument("--server", default="scrcpy/scrcpy-server")
    parser.add_argument("--scrcpy-version", default=DEFAULT_SCRCPY_VERSION); parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--max-fps", type=int, default=60); parser.add_argument("--max-size", type=int, default=0); parser.add_argument("--bitrate", type=int, default=8000000)
    parser.add_argument("--template"); parser.add_argument("--template-cx", type=float, default=.5); parser.add_argument("--template-cy", type=float, default=.5); parser.add_argument("--template-padding", type=int, default=20); parser.add_argument("--template-threshold", type=float, default=.85); parser.add_argument("--stop-on-hit", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    try: raise SystemExit(run_probe(parse_args()))
    except ProbeError as exc: print(f"[FAIL] {exc}", file=sys.stderr); raise SystemExit(1)
