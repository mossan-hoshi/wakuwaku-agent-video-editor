from pathlib import Path

import pytest

from wwedit.compose.fcpxml import KeepRange, keep_ranges_to_segments, read_keep_ranges

REAL_FCPXML = Path("D:/Users/sackn/Videos/wakuwaku/2026-06-04/video1992213112.fcpxml")


def test_keep_ranges_to_segments_fills_silence():
    ranges = [KeepRange(0.0, 2.0), KeepRange(5.0, 6.0)]
    segs = keep_ranges_to_segments(ranges, source_duration_s=10.0)
    # kept[0..2], silence[2..5], kept[5..6], silence[6..10]
    assert [(s.start, s.end, s.invalid) for s in segs] == [
        (0.0, 2.0, False),
        (2.0, 5.0, True),
        (5.0, 6.0, False),
        (6.0, 10.0, True),
    ]
    assert segs[1].reason == "silence"


@pytest.mark.skipif(not REAL_FCPXML.exists(), reason="実収録fcpxmlが無い環境")
def test_read_real_fcpxml():
    ranges = read_keep_ranges(REAL_FCPXML)
    assert len(ranges) > 50
    # 先頭クリップ: start=108/25=4.32s, duration=48/25=1.92s
    assert ranges[0].start == pytest.approx(4.32, abs=1e-6)
    assert ranges[0].duration == pytest.approx(1.92, abs=1e-6)
    # 昇順
    assert all(ranges[i].start <= ranges[i + 1].start for i in range(len(ranges) - 1))
