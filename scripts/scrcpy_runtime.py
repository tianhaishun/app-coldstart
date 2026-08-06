"""Managed scrcpy 4.x video stream runtime for the production integration phase.

The module owns adb reverse, the scrcpy-server process, socket lifetime, and a
single decoding worker. It does not decide when a cold start succeeds; callers
consume the latest decoded frame and apply their own state machine.
"""

from __future__ import annotations

import secrets
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

try:
    from scripts.scrcpy_stream import (
        BufferedStream,
        DecodedFrame,
        ProbeError,
        decode_payload_frames,
        read_session,
        read_stream_header,
        read_video_packet,
    )
except ModuleNotFoundError:
    from scrcpy_stream import (
        BufferedStream,
        DecodedFrame,
        ProbeError,
        decode_payload_frames,
        read_session,
        read_stream_header,
        read_video_packet,
    )


DEFAULT_SCRCPY_VERSION = "4.0"
DEFAULT_READ_TIMEOUT = 0.25
DEFAULT_CONNECT_TIMEOUT = 10.0
SOCKET_PREFIX = "scrcpy_"
REMOTE_SERVER = "/data/local/tmp/cst-scrcpy-server.jar"


@dataclass(frozen=True)
class ScrcpyStreamConfig:
    """Immutable settings for one Android video stream."""

    serial: str
    adb_path: str
    server_path: Path
    scrcpy_version: str = DEFAULT_SCRCPY_VERSION
    max_fps: int = 60
    max_size: int = 720
    bitrate: int = 8_000_000
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    read_timeout: float = DEFAULT_READ_TIMEOUT


class ScrcpyStream:
    """Own one scrcpy server and expose its latest decoded frame.

    ``start`` blocks only until the socket handshake succeeds or the connection
    timeout expires. Frame decoding then happens on a daemon worker. The
    callback is invoked from that worker and must be short; callers should copy
    only the state they need and avoid blocking adb operations there.
    """

    def __init__(
        self,
        config: ScrcpyStreamConfig,
        *,
        on_frame: Callable[[DecodedFrame], None] | None = None,
        decoder_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.config = config
        self.on_frame = on_frame
        self._decoder_factory = decoder_factory
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._connected = False
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._listen: socket.socket | None = None
        self._sock: socket.socket | None = None
        self._remote = REMOTE_SERVER
        self._reverse_ready = False
        self._error: str | None = None
        self._device_name = ""
        self._width = 0
        self._height = 0
        self._latest_frame: DecodedFrame | None = None
        self._frame_count = 0
        self._last_frame_at = 0.0
        self._logs: deque[str] = deque(maxlen=80)

    @property
    def latest_frame(self) -> DecodedFrame | None:
        with self._lock:
            return self._latest_frame

    @property
    def error(self) -> str | None:
        with self._lock:
            return self._error

    @property
    def is_available(self) -> bool:
        with self._lock:
            return bool(
                self._ready_event.is_set()
                and self._error is None
                and self._thread is not None
                and self._thread.is_alive()
            )

    def status(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            return {
                "available": self.is_available,
                "connected": self._connected,
                "serial": self.config.serial,
                "device": self._device_name,
                "width": self._width,
                "height": self._height,
                "frames": self._frame_count,
                "last_frame_age_ms": round((now - self._last_frame_at) * 1000, 1) if self._last_frame_at else None,
                "error": self._error,
                "logs": list(self._logs),
            }

    def start(self, timeout: float | None = None) -> None:
        """Start the stream and wait for the scrcpy handshake."""

        with self._lock:
            if self.is_available:
                return
            if not self.config.server_path.is_file():
                raise ProbeError(f"scrcpy-server 不存在：{self.config.server_path}")
            self._reset_state_locked()
            self._prepare_listener_locked()
            try:
                self._reverse()
                self._start_server_process()
                if self._stop_event.is_set():
                    raise ProbeError("scrcpy 视频流已停止")
                self._thread = threading.Thread(target=self._worker, name="scrcpy-stream", daemon=True)
                self._thread.start()
            except Exception:
                self._cleanup_transport()
                raise

        wait_for = self.config.connect_timeout if timeout is None else timeout
        deadline = time.monotonic() + wait_for
        while not self._ready_event.is_set():
            if self._stop_event.wait(min(0.1, max(0.0, deadline - time.monotonic()))):
                raise ProbeError("scrcpy 视频流已停止")
            if time.monotonic() >= deadline:
                self.stop()
                raise ProbeError(f"scrcpy 视频连接超时：{self._recent_logs()}")
        error = self.error
        if error:
            self.stop()
            raise ProbeError(error)
        if self._stop_event.is_set():
            raise ProbeError("scrcpy 视频流已停止")

    def stop(self) -> None:
        """Stop the worker and remove the adb reverse/server resources."""

        with self._lock:
            self._stop_event.set()
            listen, sock, thread = self._listen, self._sock, self._thread
            proc = self._proc
            self._listen = None
            self._sock = None
            self._thread = None
            self._proc = None
        for value in (listen, sock):
            if value is not None:
                try:
                    value.close()
                except OSError:
                    pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._remove_reverse()
        self._remove_remote()
        with self._lock:
            self._ready_event.clear()
            self._connected = False
            self._reverse_ready = False

    def reconnect(self, timeout: float | None = None) -> None:
        """Explicitly tear down and start a fresh stream."""

        self.stop()
        self.start(timeout=timeout)

    def _reset_state_locked(self) -> None:
        self._stop_event.clear()
        self._ready_event.clear()
        self._connected = False
        self._error = None
        self._device_name = ""
        self._width = self._height = 0
        self._latest_frame = None
        self._frame_count = 0
        self._last_frame_at = 0.0
        self._logs.clear()

    def _prepare_listener_locked(self) -> None:
        listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listen.bind(("127.0.0.1", 0))
        listen.listen(1)
        listen.settimeout(0.25)
        self._listen = listen
        port = int(listen.getsockname()[1])
        self._socket_name = SOCKET_PREFIX + f"{secrets.randbits(31):08x}"
        self._local_port = port

    def _adb_run(self, args: list[str]) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                [self.config.adb_path, "-s", self.config.serial, *args],
                capture_output=True,
                check=False,
                timeout=10,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProbeError(f"adb 命令超时：{' '.join(args)}") from exc
        except OSError as exc:
            raise ProbeError(f"adb 命令失败：{exc}") from exc

    def _reverse(self) -> None:
        result = self._adb_run(["reverse", f"localabstract:{self._socket_name}", f"tcp:{self._local_port}"])
        if result.returncode != 0:
            raise ProbeError(result.stderr.decode("utf-8", "replace").strip() or "adb reverse 失败")
        self._reverse_ready = True

    def _start_server_process(self) -> None:
        push = self._adb_run(["push", str(self.config.server_path), self._remote])
        if push.returncode != 0:
            raise ProbeError(push.stderr.decode("utf-8", "replace").strip() or "adb push 失败")
        args = [
            self.config.adb_path,
            "-s",
            self.config.serial,
            "shell",
            "CLASSPATH=" + self._remote,
            "app_process",
            "/",
            "com.genymobile.scrcpy.Server",
            self.config.scrcpy_version,
            f"scid={self._socket_name.removeprefix(SOCKET_PREFIX)}",
            "log_level=info",
            "video=true",
            "audio=false",
            "control=false",
            "cleanup=true",
            f"video_bit_rate={self.config.bitrate}",
            f"max_size={self.config.max_size}",
            f"max_fps={self.config.max_fps}",
            "video_codec=h264",
            "send_frame_meta=true",
            "send_dummy_byte=true",
        ]
        self._proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        threading.Thread(target=self._drain_logs, name="scrcpy-stream-logs", daemon=True).start()

    def _drain_logs(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            with self._lock:
                self._logs.append(line.decode("utf-8", "replace").rstrip())

    def _worker(self) -> None:
        listen = self._listen
        if listen is None:
            self._fail("scrcpy listener 未初始化")
            return
        try:
            sock = self._accept(listen)
            if sock is None:
                return
            with self._lock:
                self._sock = sock
            stream = BufferedStream(sock)
            sock.settimeout(self.config.read_timeout)
            try:
                device_name, _codec, _ = read_stream_header(stream)
                width, height = read_session(stream)
            except socket.timeout:
                raise ProbeError("等待 scrcpy 流头超时")
            codec = self._make_decoder()
            with self._lock:
                self._device_name, self._width, self._height = device_name, width, height
                self._connected = True
                self._ready_event.set()
            while not self._stop_event.is_set():
                try:
                    received_at = time.monotonic()
                    pts_us, payload, key_frame, session = read_video_packet(stream)
                except socket.timeout:
                    continue
                if session is not None:
                    with self._lock:
                        self._width, self._height = session
                    continue
                for frame in decode_payload_frames(codec, payload, pts_us, received_at, key_frame):
                    with self._lock:
                        self._latest_frame = frame
                        self._frame_count += 1
                        self._last_frame_at = time.monotonic()
                    if self.on_frame is not None:
                        try:
                            self.on_frame(frame)
                        except Exception as exc:
                            self._logs.append(f"frame callback failed: {exc}")
        except (ProbeError, OSError, socket.timeout) as exc:
            if not self._stop_event.is_set():
                self._fail(str(exc))
        except Exception as exc:
            if not self._stop_event.is_set():
                self._fail(f"{type(exc).__name__}: {exc}")
        finally:
            self._ready_event.set()
            self._cleanup_after_worker(listen)

    def _cleanup_after_worker(self, listen: socket.socket | None) -> None:
        """Release transport resources when the worker exits unexpectedly."""

        with self._lock:
            sock = self._sock
            if sock is not None:
                self._sock = None
            proc = self._proc
            if proc is not None:
                self._proc = None
            self._connected = False
            if self._listen is listen:
                self._listen = None
        for value in (sock, listen):
            if value is not None:
                try:
                    value.close()
                except OSError:
                    pass
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._remove_reverse()
        self._remove_remote()

    def _accept(self, listen: socket.socket) -> socket.socket | None:
        deadline = time.monotonic() + self.config.connect_timeout
        while not self._stop_event.is_set() and time.monotonic() < deadline:
            try:
                sock, _ = listen.accept()
                return sock
            except socket.timeout:
                proc = self._proc
                if proc is not None and proc.poll() is not None:
                    raise ProbeError(f"scrcpy-server 提前退出：{self._recent_logs()}")
        if not self._stop_event.is_set():
            raise ProbeError(f"scrcpy 视频连接超时：{self._recent_logs()}")
        return None

    def _make_decoder(self) -> Any:
        if self._decoder_factory is not None:
            return self._decoder_factory("h264")
        import av
        return av.CodecContext.create("h264", "r")

    def _fail(self, message: str) -> None:
        with self._lock:
            self._connected = False
            self._error = message
            self._logs.append(message)
            self._ready_event.set()

    def _recent_logs(self) -> str:
        with self._lock:
            return " | ".join(self._logs)[-1000:]

    def _cleanup_transport(self) -> None:
        listen = self._listen
        self._listen = None
        if listen is not None:
            try:
                listen.close()
            except OSError:
                pass
        self._remove_reverse()
        self._remove_remote()

    def _remove_reverse(self) -> None:
        if not getattr(self, "_reverse_ready", False):
            return
        try:
            self._adb_run(["reverse", "--remove", f"localabstract:{self._socket_name}"])
        except (OSError, ProbeError):
            pass
        self._reverse_ready = False

    def _remove_remote(self) -> None:
        try:
            self._adb_run(["shell", "rm", "-f", self._remote])
        except (OSError, ProbeError):
            pass


__all__ = ["ScrcpyStream", "ScrcpyStreamConfig"]
