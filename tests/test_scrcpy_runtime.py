"""Lifecycle tests for the managed scrcpy stream runtime."""

from __future__ import annotations

import struct
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.scrcpy_runtime import ScrcpyStream, ScrcpyStreamConfig  # noqa: E402
from scripts.scrcpy_stream import H264_CODEC_ID  # noqa: E402


class FakeProcess:
    def __init__(self):
        self.stdout = None
        self._returncode = None
        self.terminated = False

    def poll(self):
        return self._returncode

    def terminate(self):
        self.terminated = True
        self._returncode = 0

    def wait(self, timeout=None):
        self._returncode = 0
        return 0

    def kill(self):
        self._returncode = -9


class FakePacket:
    pts = None
    dts = None


class FakeFrame:
    def to_ndarray(self, format="bgr24"):
        assert format == "bgr24"
        return np.zeros((3, 4, 3), dtype=np.uint8)


class FakeCodec:
    def parse(self, payload):
        assert payload == b"x"
        return [FakePacket()]

    def decode(self, packet):
        return [FakeFrame()]


def _handshake(sock):
    sock.sendall(b"Test phone".ljust(64, b"\x00"))
    sock.sendall(struct.pack(">I", H264_CODEC_ID))
    sock.sendall(struct.pack(">III", 0x80000000, 4, 3))
    sock.sendall(struct.pack(">QI", 123456, 1) + b"x")


def test_stream_starts_decodes_latest_frame_and_cleans_up(tmp_path):
    server_path = tmp_path / "scrcpy-server"
    server_path.write_bytes(b"server")
    fake_process = FakeProcess()
    frame_ready = threading.Event()
    frames = []

    class TestStream(ScrcpyStream):
        def _reverse(self):
            self._reverse_ready = True

        def _start_server_process(self):
            self._proc = fake_process

            def connect_and_send():
                deadline = time.monotonic() + 2
                while not hasattr(self, "_local_port") and time.monotonic() < deadline:
                    time.sleep(0.001)
                import socket
                with socket.create_connection(("127.0.0.1", self._local_port), timeout=2) as sock:
                    _handshake(sock)
                    time.sleep(0.1)

            threading.Thread(target=connect_and_send, daemon=True).start()

        def _adb_run(self, args):
            return type("Result", (), {"returncode": 0, "stderr": b"", "stdout": b""})()

    stream = TestStream(
        ScrcpyStreamConfig(
            serial="serial",
            adb_path="adb",
            server_path=server_path,
            connect_timeout=2,
            read_timeout=0.05,
        ),
        on_frame=lambda frame: (frames.append(frame), frame_ready.set()),
        decoder_factory=lambda codec: FakeCodec(),
    )

    try:
        stream.start()
        assert frame_ready.wait(1)
        status = stream.status()
        assert status["available"] is True
        assert status["connected"] is True
        assert status["frames"] == 1
        assert status["width"] == 4
        assert status["height"] == 3
        assert stream.latest_frame is frames[0]
        assert frames[0].pts_us == 123456
        assert frames[0].width == 4
        assert frames[0].height == 3
    finally:
        stream.stop()

    assert fake_process.terminated is True
    assert stream.status()["available"] is False
    assert stream.status()["connected"] is False


def test_stream_rejects_missing_server(tmp_path):
    stream = ScrcpyStream(
        ScrcpyStreamConfig(
            serial="serial",
            adb_path="adb",
            server_path=tmp_path / "missing-server",
        )
    )
    try:
        stream.start(timeout=0.01)
    except RuntimeError as exc:
        assert "scrcpy-server 不存在" in str(exc)
    else:
        raise AssertionError("missing scrcpy-server should fail before adb setup")
