"""scrcpy video-frame cadence POC for the high-precision branch.

This probe is intentionally independent from the existing ADB screenshot
measurement path. It starts the pinned scrcpy-server already distributed in
``scrcpy/``, receives the H.264 video socket through adb reverse, decodes the
scrcpy packet stream with PyAV, and reports frame cadence and PTS data.

It is diagnostic-only: it does not change the production measurement state
machine or write test records.
"""

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

SCRCPY_VERSION = "3.3"
SOCKET_PREFIX = "scrcpy_"
DEVICE_NAME_LENGTH = 64
PACKET_HEADER_SIZE = 12
CONFIG_FLAG = 1 << 63
KEY_FRAME_FLAG = 1 << 62
PTS_MASK = KEY_FRAME_FLAG - 1
H264_CODEC_ID = 0x68323634  # ASCII "h264"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ProbeError(RuntimeError):
    """A recoverable POC setup or stream error."""


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
    candidates = []
    if value:
        candidates.append(value)
    if os.environ.get("ADB"):
        candidates.append(os.environ["ADB"])
    if os.name == "nt":
        candidates.append(str(PROJECT_ROOT / "adb" / "adb.exe"))
    else:
        candidates.append(str(PROJECT_ROOT / "adb" / "adb"))
    candidates.append("adb")

    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            return str(path)
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise ProbeError(f"找不到 adb：{value or 'PATH/项目目录'}")


def adb_run(adb_path: str, serial: str | None, args: list[str]) -> subprocess.CompletedProcess[bytes]:
    command = [adb_path]
    if serial:
        command += ["-s", serial]
    command += args
    return subprocess.run(command, capture_output=True, check=False)


def serial_from_args(serial: str | None, adb_path: str) -> str:
    if serial:
        result = adb_run(adb_path, serial, ["get-state"])
        if result.returncode != 0 or result.stdout.strip() != b"device":
            raise ProbeError(f"找不到设备 serial={serial}")
        return serial

    result = subprocess.run([adb_path, "devices"], capture_output=True, check=False)
    if result.returncode != 0:
        raise ProbeError("adb devices 执行失败")
    devices = []
    for line in result.stdout.decode("utf-8", "replace").splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "device":
            devices.append(fields[0])
    if len(devices) != 1:
        raise ProbeError(f"请使用 --serial 指定设备，当前设备数={len(devices)}")
    return devices[0]


def choose_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def read_stream_header(sock: socket.socket) -> tuple[str, int, int]:
    """Read scrcpy's dummy byte, device metadata and H264 stream header."""
    dummy = read_exact(sock, 1)
    if dummy != b"\x00":
        raise ProbeError(f"scrcpy dummy byte 异常：{dummy!r}")

    device_name = read_exact(sock, DEVICE_NAME_LENGTH).rstrip(b"\x00").decode(
        "utf-8", "replace"
    )
    codec_id = struct.unpack(">I", read_exact(sock, 4))[0]
    if codec_id != H264_CODEC_ID:
        raise ProbeError(f"期望 H264 codec id，收到 0x{codec_id:08x}")
    width, height = struct.unpack(">II", read_exact(sock, 8))
    return device_name, width, height


def read_packet(sock: socket.socket) -> tuple[int, bytes, bool]:
    """Read one scrcpy packet: (PTS in microseconds, payload, key-frame)."""
    pts_flags, size = struct.unpack(">QI", read_exact(sock, PACKET_HEADER_SIZE))
    if size <= 0 or size > 64 * 1024 * 1024:
        raise ProbeError(f"非法视频 packet size={size}")
    payload = read_exact(sock, size)
    if pts_flags & CONFIG_FLAG:
        return -1, payload, False
    return pts_flags & PTS_MASK, payload, bool(pts_flags & KEY_FRAME_FLAG)


def process_packet(
    codec: Any,
    payload: bytes,
    pts_us: int,
    frame_index: int,
    samples: list[dict[str, Any]],
    received_at: float,
) -> int:
    """Feed an encoded packet to PyAV and append decoded-frame diagnostics."""
    decoded_at = time.perf_counter()
    for packet in codec.parse(payload):
        if pts_us >= 0:
            packet.pts = pts_us
            packet.dts = pts_us
        for frame in codec.decode(packet):
            # Convert to BGR to include the OpenCV-compatible pixel conversion
            # cost in the POC rather than measuring H264 decode only.
            bgr = frame.to_ndarray(format="bgr24")
            frame_index += 1
            samples.append(
                {
                    "index": frame_index,
                    "pts_us": pts_us,
                    "received_at": received_at,
                    "decoded_at": time.perf_counter(),
                    "decode_to_bgr_ms": round(
                        (time.perf_counter() - decoded_at) * 1000, 3
                    ),
                    "width": int(bgr.shape[1]),
                    "height": int(bgr.shape[0]),
                }
            )
    return frame_index


def summarize(samples: list[dict[str, Any]], duration: float) -> dict[str, Any]:
    if len(samples) < 2:
        raise ProbeError(f"POC 在 {duration:.1f}s 内只解码 {len(samples)} 帧，无法统计帧间隔")

    pts_deltas = [
        (b["pts_us"] - a["pts_us"]) / 1000
        for a, b in zip(samples, samples[1:])
        if a["pts_us"] >= 0 and b["pts_us"] >= 0 and b["pts_us"] > a["pts_us"]
    ]
    receive_deltas = [
        (b["received_at"] - a["received_at"]) * 1000
        for a, b in zip(samples, samples[1:])
    ]
    decode_ms = [item["decode_to_bgr_ms"] for item in samples]

    def percentile(values: list[float], p: float) -> float:
        ordered = sorted(values)
        pos = min(len(ordered) - 1, int(len(ordered) * p))
        return round(ordered[pos], 3)

    def summary(values: list[float]) -> dict[str, float | None]:
        if not values:
            return {"p50": None, "p95": None, "max": None}
        return {
            "p50": percentile(values, 0.50),
            "p95": percentile(values, 0.95),
            "max": round(max(values), 3),
        }

    first_pts = samples[0]["pts_us"]
    last_pts = samples[-1]["pts_us"]
    pts_duration_s = (last_pts - first_pts) / 1_000_000
    receive_duration_s = samples[-1]["received_at"] - samples[0]["received_at"]
    return {
        "duration_s": round(duration, 3),
        "decoded_frames": len(samples),
        "pts_fps": round((len(pts_deltas) / pts_duration_s), 3)
        if pts_duration_s > 0
        else None,
        "receive_fps": round((len(receive_deltas) / receive_duration_s), 3)
        if receive_duration_s > 0
        else None,
        "pts_interval_ms": summary(pts_deltas),
        "receive_interval_ms": summary(receive_deltas),
        "decode_to_bgr_ms": summary(decode_ms),
        "first_pts_us": first_pts,
        "last_pts_us": last_pts,
    }


def start_server(
    serial: str,
    adb_path: str,
    server_path: Path,
    scid: str,
    max_fps: int,
    max_size: int,
    bitrate: int,
) -> tuple[subprocess.Popen[bytes], deque[str]]:
    """Push and start scrcpy-server 3.3 with video-only low-latency options."""
    remote = "/data/local/tmp/scrcpy-frame-probe-server.jar"
    push = adb_run(adb_path, serial, ["push", str(server_path), remote])
    if push.returncode != 0:
        raise ProbeError(push.stderr.decode("utf-8", "replace").strip() or "adb push 失败")

    args = [
        adb_path,
        "-s",
        serial,
        "shell",
        "CLASSPATH=" + remote,
        "app_process",
        "/",
        "com.genymobile.scrcpy.Server",
        SCRCPY_VERSION,
        f"scid={scid}",
        "log_level=info",
        "video=true",
        "audio=false",
        "control=false",
        "cleanup=true",
        f"video_bit_rate={bitrate}",
        f"max_size={max_size}",
        f"max_fps={max_fps}",
        "video_codec=h264",
        "send_frame_meta=true",
        "send_codec_meta=true",
        "send_dummy_byte=true",
    ]
    logs: deque[str] = deque(maxlen=80)
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def drain() -> None:
        if proc.stdout is None:
            return
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

    scid = secrets.token_hex(4)
    socket_name = SOCKET_PREFIX + scid
    local_port = choose_local_port()
    listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen.bind(("127.0.0.1", local_port))
    listen.listen(1)
    listen.settimeout(1)
    remote = f"localabstract:{socket_name}"
    local = f"tcp:{local_port}"
    reverse = adb_run(adb_path, serial, ["reverse", remote, local])
    if reverse.returncode != 0:
        listen.close()
        raise ProbeError(reverse.stderr.decode("utf-8", "replace").strip() or "adb reverse 失败")

    proc: subprocess.Popen[bytes] | None = None
    sock: socket.socket | None = None
    logs: deque[str] = deque()
    try:
        proc, logs = start_server(
            serial, adb_path, server_path, scid,
            args.max_fps, args.max_size, args.bitrate,
        )
        deadline = time.monotonic() + 10
        while True:
            try:
                sock, _ = listen.accept()
                break
            except socket.timeout:
                if time.monotonic() >= deadline:
                    raise ProbeError(f"scrcpy 视频连接超时：{' | '.join(logs)[-1000:]}")
                if proc.poll() is not None:
                    raise ProbeError(f"scrcpy-server 提前退出：{' | '.join(logs)[-1000:]}")

        sock.settimeout(None)
        device_name, width, height = read_stream_header(sock)
        print(f"device: {device_name} serial={serial} stream={width}x{height}")

        try:
            import av
        except ImportError as exc:
            raise ProbeError("缺少 PyAV，请安装 scripts/requirements-scrcpy-frame.txt") from exc
        codec = av.CodecContext.create("h264", "r")
        samples: list[dict[str, Any]] = []
        frame_index = 0
        start = time.perf_counter()
        end = start + args.duration
        while time.perf_counter() < end:
            received_at = time.perf_counter()
            pts_us, payload, _key = read_packet(sock)
            frame_index = process_packet(
                codec, payload, pts_us, frame_index, samples, received_at
            )

        # Flush delayed decoder frames. PTS statistics only include live frames.
        for frame in codec.decode(None):
            frame.to_ndarray(format="bgr24")

        result = summarize(samples, time.perf_counter() - start)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        listen.close()
        if sock:
            sock.close()
        subprocess.run(
            [adb_path, "-s", serial, "reverse", "--remove", remote],
            capture_output=True,
            check=False,
        )
        subprocess.run(
            [adb_path, "-s", serial, "shell", "rm", "-f", "/data/local/tmp/scrcpy-frame-probe-server.jar"],
            capture_output=True,
            check=False,
        )
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="scrcpy 3.3 video frame cadence POC")
    parser.add_argument("--serial", help="Android serial; required when multiple devices exist")
    parser.add_argument("--adb", help="adb executable; defaults to ADB, project adb/, or PATH")
    parser.add_argument("--server", default="scrcpy/scrcpy-server", help="scrcpy-server 3.3 path")
    parser.add_argument("--duration", type=float, default=10.0, help="probe duration in seconds")
    parser.add_argument("--max-fps", type=int, default=60)
    parser.add_argument("--max-size", type=int, default=0)
    parser.add_argument("--bitrate", type=int, default=8000000)
    return parser.parse_args()


if __name__ == "__main__":
    try:
        raise SystemExit(run_probe(parse_args()))
    except ProbeError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        raise SystemExit(1)
