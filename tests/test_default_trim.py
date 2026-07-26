"""全画面(no_crop)区間への既定トリム（上下左右1割）のテスト。"""

from __future__ import annotations

from wwedit.edl.schema import Edl, FramingRegion, SourceMedia
from wwedit.framing.default_trim import apply_default_trim, inset_bbox


def _edl(regions: list[FramingRegion], w: int = 1920, h: int = 1080) -> Edl:
    return Edl(
        recording_dir="rec",
        source=SourceMedia(video_path="v.mp4", width=w, height=h, fps=25),
        framing=regions,
    )


def test_inset_bbox_keeps_aspect_and_centers() -> None:
    x, y, w, h = inset_bbox(1920, 1080, 0.1)
    assert (x, y, w, h) == (192, 108, 1536, 864)
    # 16:9 が保たれる（幅も高さも同率で縮むため）
    assert abs(w / h - 1920 / 1080) < 1e-6
    # 中央に寄っている（左右・上下の残りが等しい）
    assert x == 1920 - (x + w)
    assert y == 1080 - (y + h)


def test_inset_bbox_clamps_extreme_inset() -> None:
    # 0.5 以上を渡しても潰れた枠を作らない
    x, y, w, h = inset_bbox(1920, 1080, 0.9)
    assert w >= 1 and h >= 1
    assert x + w <= 1920 and y + h <= 1080


def test_apply_default_trim_fills_only_missing_bbox() -> None:
    edl = _edl([
        FramingRegion(start=0, end=10, kind="static", bbox=None),
        FramingRegion(start=10, end=20, kind="static", bbox=(100, 100, 800, 450)),
        FramingRegion(start=20, end=30, kind="pending", bbox=None),
    ])
    n = apply_default_trim(edl)
    assert n == 2  # 既存 bbox の区間は触らない
    assert edl.framing[0].bbox == (192, 108, 1536, 864)
    assert edl.framing[1].bbox == (100, 100, 800, 450)
    assert edl.framing[2].bbox == (192, 108, 1536, 864)


def test_apply_default_trim_leaves_no_fullscreen_region() -> None:
    edl = _edl([FramingRegion(start=0, end=10, kind="static", bbox=None) for _ in range(5)])
    apply_default_trim(edl)
    assert all(r.bbox is not None for r in edl.framing)


def test_apply_default_trim_skips_loading_regions() -> None:
    # loading は生成画面なので crop 対象外（既定 kinds に含めない）
    edl = _edl([FramingRegion(start=0, end=5, kind="loading", bbox=None)])
    assert apply_default_trim(edl) == 0
    assert edl.framing[0].bbox is None


def test_apply_default_trim_is_idempotent() -> None:
    edl = _edl([FramingRegion(start=0, end=10, kind="static", bbox=None)])
    assert apply_default_trim(edl) == 1
    assert apply_default_trim(edl) == 0  # 2回目は書き込むものが無い
