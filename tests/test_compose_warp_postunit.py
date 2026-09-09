"""[S2] ワープ後の EDL でも投稿単位が同じ場所を指すこと。

`warp_edl` は utterances/chapters/framing/overlays を新しい出力秒へ写すが、
`post_units` を写し忘れると**ソース秒のまま**残る。ワープ後の EDL は
segments が `[0, out_total]` の1本なので、`post_unit_ranges` の span 判定が
ソース秒と混ざって**別の場所を切り出す**（前半のつもりが後半の頭まで含む等）。
"""

from __future__ import annotations

import pytest

from wwedit.compose.timewarp import Warp, WarpSeg
from wwedit.compose.warp_apply import warp_edl
from wwedit.edl.postunit import post_unit_chapter_lines, post_unit_ranges
from wwedit.edl.schema import Chapter, Edl, PostUnit, Segment, SourceMedia, TimeRange


class _Probe:
    duration_s = 50.0


@pytest.fixture
def edl() -> Edl:
    """ソース 0..100 のうち 10..20 と 30..45 を残す2投稿ぶんの EDL。"""
    return Edl(
        recording_dir="data/x",
        source=SourceMedia(video_path="src.mp4", duration_s=100.0, fps=25),
        segments=[
            Segment(id="a", start=0.0, end=10.0, invalid=True),
            Segment(id="b", start=10.0, end=20.0),
            Segment(id="c", start=20.0, end=30.0, invalid=True),
            Segment(id="d", start=30.0, end=45.0),
        ],
        chapters=[Chapter(start_at=10.0), Chapter(start_at=30.0)],
        post_units=[
            PostUnit(id="p0", ranges=[TimeRange(start=10.0, end=20.0)], chapter_ids=[0]),
            PostUnit(id="p1", ranges=[TimeRange(start=30.0, end=45.0)], chapter_ids=[1]),
        ],
    )


def _warped(edl: Edl, monkeypatch, tmp_path) -> Edl:
    monkeypatch.setattr("wwedit.compose.warp_apply.probe", lambda _p: _Probe())
    # out 0..25（10..20 → 0..10 / 30..45 → 10..25）を半分の尺へ畳む
    warp = Warp([WarpSeg(0.0, 25.0, 12.5)])
    return warp_edl(
        edl, warp, tmp_path / "warped.mp4",
        ranges=edl.kept_ranges(), freezes=(), voice_paths={}, desktop_paths={},
        subtitles=[], report_rows=[],
    )


def test_post_units_are_mapped_onto_the_warped_timeline(edl, monkeypatch, tmp_path):
    new = _warped(edl, monkeypatch, tmp_path)
    assert [(r.start, r.end) for u in new.post_units for r in u.ranges] == [
        (0.0, 5.0),    # 元 10..20（out 0..10）が半速で 0..5
        (5.0, 12.5),   # 元 30..45（out 10..25）が 5..12.5
    ]


def test_each_post_unit_still_selects_its_own_half(edl, monkeypatch, tmp_path):
    """写せていないとソース秒の span がワープ後の全長を飲み込み、両単位が同じ絵になる。"""
    new = _warped(edl, monkeypatch, tmp_path)
    assert [(r.start, r.end) for r in post_unit_ranges(new, 0)] == [(0.0, 5.0)]
    assert [(r.start, r.end) for r in post_unit_ranges(new, 1)] == [(5.0, 12.5)]


def test_each_post_unit_keeps_exactly_its_own_chapter(edl, monkeypatch, tmp_path):
    new = _warped(edl, monkeypatch, tmp_path)
    assert len(post_unit_chapter_lines(new, 0)) == 1
    assert len(post_unit_chapter_lines(new, 1)) == 1
