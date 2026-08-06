"""scrcpy video-frame cadence and OpenCV matcher POC."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

try:
    from scripts.scrcpy_stream import (
        BufferedStream,
        MarkerMatcher,
        ProbeError,
        process_packet,
        read_packet,
        read_session,
        read_stream_header,
        read_video_packet,
    )
except ModuleNotFoundError:
    # Direct execution as ``python scripts/scrcpy_frame_probe.py`` puts the
    # scripts directory, rather than the project root, on sys.path.
    from scrcpy_stream import (
        BufferedStream,
        MarkerMatcher,
        ProbeError,
        process_packet,
        read_packet,
        read_session,
        read_stream_header,
        read_video_packet,
    )

DEFAULT_SCRCPY_VERSION = "4.0"
READ_POLL_INTERVAL = 0.25
STREAM_HEADER_TIMEOUT = 5.0
SOCKET_PREFIX = "scrcpy_"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


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

    pts_samples = [s for s in samples if s["pts_us"] >= 0]
    pts_duration_s = (pts_samples[-1]["pts_us"] - pts_samples[0]["pts_us"]) / 1_000_000 if len(pts_samples) >= 2 else 0.0
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
        "first_pts_us": pts_samples[0]["pts_us"] if pts_samples else None,
        "last_pts_us": pts_samples[-1]["pts_us"] if pts_samples else None,
    }


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


def start_server(serial: str, adb_path: str, server_path: Path, scid: str, max_fps: int, max_size: int, bitrate: int, version: str) -> tuple[subprocess.Popen[bytes], deque[str]]:
    remote = "/data/local/tmp/scrcpy-frame-probe-server.jar"
    push = adb_run(adb_path, serial, ["push", str(server_path), remote])
    if push.returncode != 0:
        raise ProbeError(push.stderr.decode("utf-8", "replace").strip() or "adb push 失败")
    args = [adb_path, "-s", serial, "shell", "CLASSPATH=" + remote, "app_process", "/", "com.genymobile.scrcpy.Server", version, f"scid={scid}", "log_level=info", "video=true", "audio=false", "control=false", "cleanup=true", f"video_bit_rate={bitrate}", f"max_size={max_size}", f"max_fps={max_fps}", "video_codec=h264", "send_frame_meta=true", "send_dummy_byte=true"]
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
        stream = BufferedStream(sock)
        sock.settimeout(STREAM_HEADER_TIMEOUT)
        try:
            device_name, _codec, _ = read_stream_header(stream)
            width, height = read_session(stream)
        except socket.timeout:
            raise ProbeError("等待 scrcpy 流头超时")
        print(f"device: {device_name} serial={serial} stream={width}x{height}")
        import av
        codec = av.CodecContext.create("h264", "r")
        matcher = MarkerMatcher.from_file(args.template, args.template_cx, args.template_cy, args.template_padding, args.template_threshold) if args.template else None
        samples: list[dict[str, Any]] = []; frame_index = 0; start = time.perf_counter()
        stopped_on_hit = False
        sock.settimeout(READ_POLL_INTERVAL)
        while time.perf_counter() < start + args.duration:
            try:
                received_at = time.perf_counter()
                pts_us, payload, _key, session = read_video_packet(stream)
            except socket.timeout:
                continue
            if session is not None:
                width, height = session; continue
            frame_index = process_packet(codec, payload, pts_us, frame_index, samples, received_at, matcher)
            if matcher and samples and samples[-1].get("match", {}).get("hit"):
                print(json.dumps({"marker_hit": samples[-1]["match"], "frame": samples[-1]}, ensure_ascii=False))
                if args.stop_on_hit:
                    stopped_on_hit = True
                    break
        if stopped_on_hit and len(samples) < 2:
            return 0
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
    for stream in (sys.stdout, sys.stderr):
        try: stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError): pass
    try: raise SystemExit(run_probe(parse_args()))
    except ProbeError as exc: print(f"[FAIL] {exc}", file=sys.stderr); raise SystemExit(1)
