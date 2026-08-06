"""keep区間を**フレーミング境界でも割る**（STATUS §17.9 の修正）。

割らないと 1 keep区間につき bbox が中点の1つしか当たらない。ワープ後(方式B)の EDL は
keep区間が**全長で1個**なので、crop も 画面内NGワードのモザイクも丸ごと効かなくなっていた
（2026-08-06 実測: 方式A keep 214区間/bbox 203 に対し 方式B は keep 1/bbox 0）。
"""
from __future__ import annotations

from wwedit.compose.ffmpeg_compose import (
    FRAMING_MIN_PIECE_S,
    build_filter_script_framed,
    framed_pieces,
    framing_bounds,
    split_range_at_bounds,
)
from wwedit.compose.overlay import output_crop_segments
from wwedit.edl.schema import Edl, FramingRegion, Segment, SourceMedia, TimeRange


def _edl(framing: list[tuple[float, float, tuple | None]], *, dur: float = 100.0) -> Edl:
    return Edl(
        recording_dir="2026-08-03",
        source=SourceMedia(video_path="v.mp4", duration_s=dur, width=1920, height=1080),
        segments=[Segment(id="s", start=0.0, end=dur)],
        framing=[FramingRegion(start=s, end=e, bbox=b) for s, e, b in framing],
    )


def test_bounds_are_sorted_and_deduped():
    edl = _edl([(0.0, 10.0, (0, 0, 100, 100)), (10.0, 20.0, (5, 5, 100, 100))])
    assert framing_bounds(edl) == [0.0, 10.0, 20.0]


def test_no_framing_means_no_split():
    edl = _edl([])
    r = TimeRange(start=0.0, end=50.0)
    assert framed_pieces(edl, [r]) == [(r, 0.0)]


def test_a_single_keep_range_is_split_per_framing_region():
    """ワープ後の EDL（keep が全長1個）でも、フレーミングの数だけ小片ができる。"""
    edl = _edl([(0.0, 30.0, (0, 0, 800, 600)),
                (30.0, 60.0, (100, 100, 800, 600)),
                (60.0, 100.0, None)])
    pieces = framed_pieces(edl, [TimeRange(start=0.0, end=100.0)])
    assert [(round(r.start, 3), round(r.end, 3)) for r, _ in pieces] == [
        (0.0, 30.0), (30.0, 60.0), (60.0, 100.0)]


def test_split_pieces_cover_the_range_without_gaps_or_overlaps():
    edl = _edl([(0.0, 30.0, (0, 0, 800, 600)), (30.0, 100.0, (1, 1, 800, 600))])
    r = TimeRange(start=5.0, end=95.0)
    pieces = [p for p, _ in framed_pieces(edl, [r])]
    assert pieces[0].start == r.start
    assert pieces[-1].end == r.end
    for a, b in zip(pieces, pieces[1:], strict=False):
        assert a.end == b.start


def test_total_duration_is_unchanged_by_splitting():
    edl = _edl([(0.0, 7.0, (0, 0, 800, 600)), (7.0, 100.0, None)])
    ranges = [TimeRange(start=0.0, end=20.0), TimeRange(start=40.0, end=90.0)]
    before = sum(r.duration for r in ranges)
    after = sum(r.duration for r, _ in framed_pieces(edl, ranges))
    assert abs(before - after) < 1e-9


def test_boundaries_outside_the_range_are_ignored():
    edl = _edl([(0.0, 10.0, (0, 0, 800, 600)), (10.0, 100.0, None)])
    r = TimeRange(start=20.0, end=40.0)
    assert framed_pieces(edl, [r]) == [(r, 0.0)]


def test_tiny_pieces_are_not_created():
    """1フレーム未満の小片は concat で消えるので、境界が端に近すぎたら割らない。"""
    subs = split_range_at_bounds(TimeRange(start=10.0, end=50.0), [10.05])
    assert len(subs) == 1


def test_close_boundaries_are_thinned():
    bounds = [10.0, 10.01, 10.02, 30.0]
    subs = split_range_at_bounds(TimeRange(start=0.0, end=50.0), bounds)
    assert [round(s.end, 3) for s in subs] == [10.0, 30.0, 50.0]
    for s in subs:
        assert s.duration >= FRAMING_MIN_PIECE_S


def test_the_freeze_extra_stays_on_the_last_sub_piece():
    from wwedit.edl.schema import Freeze

    edl = _edl([(0.0, 30.0, (0, 0, 800, 600)), (30.0, 100.0, None)])
    edl.freezes = [Freeze(at=50.0, extra=2.0)]
    pieces = framed_pieces(edl, [TimeRange(start=0.0, end=100.0)], tuple(edl.freezes))
    extras = {round(r.end, 3): x for r, x in pieces}
    assert extras[50.0] == 2.0        # フリーズ位置の小片だけが延長を持つ
    assert extras[30.0] == 0.0


def test_every_framing_bbox_reaches_the_filter_script():
    """割った結果、各フレーミングの bbox が実際に crop として出る。"""
    edl = _edl([(0.0, 30.0, (10, 20, 800, 600)),
                (30.0, 60.0, (30, 40, 900, 700)),
                (60.0, 100.0, (50, 60, 1000, 800))])
    script = build_filter_script_framed(edl, [TimeRange(start=0.0, end=100.0)])
    for crop in ("crop=800:600:10:20", "crop=900:700:30:40", "crop=1000:800:50:60"):
        assert crop in script


def test_crop_segments_match_the_filter_script_split():
    """モザイク/重ねの配置区間と、crop concat の区間分割が一致する。"""
    edl = _edl([(0.0, 30.0, (10, 20, 800, 600)), (30.0, 100.0, (30, 40, 900, 700))])
    ranges = [TimeRange(start=0.0, end=100.0)]
    segs = output_crop_segments(edl, ranges)
    assert [(s, e, b) for s, e, b in segs] == [
        (0.0, 30.0, (10, 20, 800, 600)), (30.0, 100.0, (30, 40, 900, 700))]


def test_adjacent_identical_bboxes_are_still_folded():
    edl = _edl([(0.0, 30.0, (10, 20, 800, 600)), (30.0, 100.0, (10, 20, 800, 600))])
    segs = output_crop_segments(edl, [TimeRange(start=0.0, end=100.0)])
    assert len(segs) == 1
