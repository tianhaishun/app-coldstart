"""Unit tests for the video-to-ADB fallback helpers."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import _match_template_in_scene, _scene_for_template  # noqa: E402


def test_match_template_returns_zero_when_template_larger_than_scene():
    pytest.importorskip("cv2", reason="OpenCV is required for fallback image tests")
    scene = np.zeros((20, 20, 3), dtype=np.uint8)
    template = np.zeros((40, 40, 3), dtype=np.uint8)
    assert _match_template_in_scene(scene, template, 0.5, 0.5, 20) == 0.0


def test_scene_for_template_scales_whole_scene():
    pytest.importorskip("cv2", reason="OpenCV is required for fallback image tests")
    scene = np.zeros((720, 324, 3), dtype=np.uint8)
    resized = _scene_for_template(scene, 1080, 2400)
    assert resized.shape == (2400, 1080, 3)


def test_scene_for_template_keeps_matching_scene_identity():
    scene = np.zeros((720, 324, 3), dtype=np.uint8)
    assert _scene_for_template(scene, 324, 720) is scene


def test_scene_for_template_keeps_legacy_unknown_source():
    scene = np.zeros((720, 324, 3), dtype=np.uint8)
    assert _scene_for_template(scene, 0, 0) is scene
