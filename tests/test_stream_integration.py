"""Focused tests for Session's optional video-channel guards."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402


def test_stream_scene_rejects_template_resolution_mismatch(monkeypatch):
    session = server.Session()
    session._marker_src_w = 1080
    session._marker_src_h = 2400
    frame = SimpleNamespace(
        image=np.zeros((720, 324, 3), dtype=np.uint8),
        width=324,
        height=720,
        received_at=10.0,
    )
    session._stream = SimpleNamespace(is_available=True, latest_frame=frame)
    monkeypatch.setattr(server.time, "monotonic", lambda: 10.05)
    assert session._stream_scene() is None


def test_stream_scene_accepts_matching_fresh_frame(monkeypatch):
    session = server.Session()
    session._marker_src_w = 324
    session._marker_src_h = 720
    frame = SimpleNamespace(
        image=np.zeros((720, 324, 3), dtype=np.uint8),
        width=324,
        height=720,
        received_at=10.0,
    )
    session._stream = SimpleNamespace(is_available=True, latest_frame=frame)
    monkeypatch.setattr(server.time, "monotonic", lambda: 10.05)
    assert session._stream_scene() is frame


def test_stream_scene_rejects_stale_frame(monkeypatch):
    session = server.Session()
    session._marker_src_w = 324
    session._marker_src_h = 720
    frame = SimpleNamespace(
        image=np.zeros((720, 324, 3), dtype=np.uint8),
        width=324,
        height=720,
        received_at=10.0,
    )
    session._stream = SimpleNamespace(is_available=True, latest_frame=frame)
    monkeypatch.setattr(server.time, "monotonic", lambda: 11.1)
    assert session._stream_scene() is None


def test_select_stops_old_stream(monkeypatch):
    session = server.Session()

    class FakeStream:
        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    old = FakeStream()
    session._stream = old
    session._serial = "old"
    session._platform = "android"
    monkeypatch.setattr(server, "ScrcpyStream", None)
    session.select("new", "ios")
    assert old.stopped is True
    assert session._stream is None


def test_stream_status_without_stream():
    session = server.Session()
    assert session.stream_status() == {
        "available": False,
        "configured": False,
        "error": None,
    }


def test_stream_scene_rejects_unmatched_skip_template(monkeypatch):
    session = server.Session()
    session._marker_src_w = 324
    session._marker_src_h = 720
    frame = SimpleNamespace(
        image=np.zeros((720, 324, 3), dtype=np.uint8),
        width=324,
        height=720,
        received_at=10.0,
    )
    session._stream = SimpleNamespace(is_available=True, latest_frame=frame)
    session._skip_templates = [{"id": 1, "src_w": 1080, "src_h": 2400}]
    monkeypatch.setattr(server.time, "monotonic", lambda: 10.05)
    assert session._stream_scene(require_skips=True) is None
