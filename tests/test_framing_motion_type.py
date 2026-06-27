"""framing.motion_type（動き種別: 画面切替 vs コンテンツ内動画）のテスト。

純粋コア spread_from_flow と、pending 区間分類のロジック（フローはモンキーパッチ）を検証。
"""

from __future__ import annotations

import numpy as np

from wwedit.edl.schema import FramingRegion
from wwedit.framing import motion_type
from wwedit.framing.motion_type import (
    SWITCH_SPREAD_THR,
    classify_pending_region,
    spread_from_flow,
)


def test_spread_zero_when_no_motion():
    assert spread_from_flow(np.zeros((64, 64), dtype="float32"), mag_thr=2.0) == 0.0


def test_spread_one_when_whole_frame_moves():
    mag = np.full((64, 64), 5.0, dtype="float32")  # 全面が大きく動く
    assert spread_from_flow(mag, grid=16, mag_thr=2.0) == 1.0


def test_spread_localized_motion_is_small():
    mag = np.zeros((64, 64), dtype="float32")
    mag[:8, :8] = 10.0  # 左上1セルだけ動く
    s = spread_from_flow(mag, grid=8, mag_thr=2.0)
    assert 0.0 < s <= 1.0 / 8  # ごく一部のみ


def test_spread_handles_degenerate_input():
    assert spread_from_flow(np.array([], dtype="float32")) == 0.0
    assert spread_from_flow(np.zeros((0, 0), dtype="float32")) == 0.0


def test_classify_switch_sets_loading(monkeypatch):
    monkeypatch.setattr(motion_type, "region_motion_spread", lambda *a, **k: 0.9)
    r = FramingRegion(start=0.0, end=2.0, kind="pending")
    classify_pending_region("dummy.mp4", r)
    assert r.kind == "loading" and r.warning == ""


def test_classify_content_video_keeps_pending_with_warning(monkeypatch):
    monkeypatch.setattr(motion_type, "region_motion_spread", lambda *a, **k: 0.1)
    r = FramingRegion(start=0.0, end=2.0, kind="pending")
    classify_pending_region("dummy.mp4", r)
    assert r.kind == "pending"
    assert "コンテンツ内動画" in r.warning


def test_classify_ignores_non_pending(monkeypatch):
    called = False

    def _spy(*a, **k):
        nonlocal called
        called = True
        return 0.9

    monkeypatch.setattr(motion_type, "region_motion_spread", _spy)
    r = FramingRegion(start=0.0, end=2.0, kind="static")
    classify_pending_region("dummy.mp4", r)
    assert r.kind == "static" and called is False


def test_threshold_boundary(monkeypatch):
    monkeypatch.setattr(motion_type, "region_motion_spread", lambda *a, **k: SWITCH_SPREAD_THR)
    r = FramingRegion(start=0.0, end=2.0, kind="pending")
    classify_pending_region("dummy.mp4", r)
    assert r.kind == "loading"  # 閾値ちょうどは切替側
