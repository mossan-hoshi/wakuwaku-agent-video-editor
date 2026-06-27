from pathlib import Path

import pytest

from wwedit.drp.reader import (
    DEFAULT_DRP,
    _day_from_path,
    _hex_double,
    final_timeline_for_day,
    remap_path,
)


def test_hex_double_framerate():
    # MediaFrameRate (先頭8バイト little-endian double) = 25.0
    assert _hex_double("00000000000039400000000000000000") == pytest.approx(25.0)


def test_remap_path():
    assert remap_path("K:/Users/x/v.mp4") == "D:/Users/x/v.mp4"
    assert remap_path("D:/already.mp4") == "D:/already.mp4"


def test_day_from_path():
    assert _day_from_path(r"D:\...\2026-06-04\video1.mp4") == "2026-06-04"
    assert _day_from_path(r"K:\...\20251108_saburo\a.m4a") == "2025-11-08"


@pytest.mark.skipif(not Path(DEFAULT_DRP).exists(), reason="正解 .drp が無い環境")
@pytest.mark.parametrize(
    "day,expect_clips", [("2026-06-04", 116), ("2026-06-01", 133), ("2026-05-28", 80)]
)
def test_final_timeline_matches_recut(day, expect_clips):
    tl = final_timeline_for_day(day)
    assert tl is not None
    assert len(tl.video_clips) == expect_clips
