"""画面OCR由来の自動モザイク重ね（秘匿語/NGワード）のテスト。

OCR/フレーム抽出は注入して置き換えるので GPU/IO 無しで回る。
"""

from __future__ import annotations

from wwedit.edl.schema import Edl, FramingRegion, Segment, SourceMedia
from wwedit.ocr.screen_scan import FrameOcr
from wwedit.privacy.ocr_mosaic import (
    expand_box,
    group_hits,
    group_spans,
    hits_to_overlays,
    mosaics_from_frames,
    scan_ng_mosaics,
    span_for_time,
    union_box,
)

W, H = 1920, 1080


class _Box:
    """OcrBox 互換（text と box を持てばよい）。"""

    def __init__(self, text: str, box: tuple[int, int, int, int]) -> None:
        self.text = text
        self.box = box


def test_expand_box_grows_and_clamps() -> None:
    out = expand_box((900, 500, 1000, 530), W, H, margin=0.8, min_frac=0.06)
    x0, y0, x1, y1 = out
    assert x1 - x0 > 100 and y1 - y0 > 30  # 元より広い
    assert y1 - y0 >= H * 0.06 - 1  # 細い行も最小サイズまで太る
    assert 0 <= x0 and 0 <= y0 and x1 <= W and y1 <= H  # フレーム内


def test_expand_box_at_frame_edge_stays_inside() -> None:
    out = expand_box((0, 0, 40, 20), W, H)
    x0, y0, x1, y1 = out
    assert x0 >= 0 and y0 >= 0 and x1 <= W and y1 <= H
    assert x1 > x0 and y1 > y0


def test_union_box() -> None:
    assert union_box([(10, 20, 30, 40), (25, 5, 60, 35)]) == (10, 5, 60, 40)


def test_group_hits_merges_same_place_over_time() -> None:
    hits = [(0.0, (100, 100, 200, 200)), (2.0, (110, 105, 210, 205))]
    groups = group_hits(hits, max_gap=3.0)
    assert len(groups) == 1
    assert groups[0][0] == 0.0 and groups[0][1] == 2.0
    assert groups[0][2] == (100, 100, 210, 205)  # union


def test_group_hits_separates_distant_places() -> None:
    hits = [(0.0, (100, 100, 200, 200)), (0.0, (1500, 800, 1700, 900))]
    assert len(group_hits(hits, max_gap=3.0)) == 2


def test_group_hits_separates_distant_times() -> None:
    hits = [(0.0, (100, 100, 200, 200)), (60.0, (100, 100, 200, 200))]
    assert len(group_hits(hits, max_gap=3.0)) == 2


def test_hits_to_overlays_normalizes_to_source_frame() -> None:
    ovs = hits_to_overlays([(10.0, 20.0, (192, 108, 960, 540))], W, H, pad=2.0)
    assert len(ovs) == 1
    o = ovs[0]
    assert o.kind == "mosaic"
    assert o.start == 8.0 and o.end == 22.0  # 前後に pad
    assert abs(o.x - 0.1) < 1e-6 and abs(o.y - 0.1) < 1e-6
    assert abs(o.w - 0.4) < 1e-6 and abs(o.h - 0.4) < 1e-6


def test_hits_to_overlays_clamps_to_source_duration() -> None:
    ovs = hits_to_overlays(
        [(0.5, 100.0, (0, 0, 100, 100))], W, H, pad=5.0, duration_s=101.0
    )
    assert ovs[0].start == 0.0 and ovs[0].end == 101.0


def test_span_for_time_uses_containing_region() -> None:
    spans = [(0.0, 10.0), (10.0, 60.0)]
    # 区間内で当たったら、その区間まるごと（サンプル±padではない）
    assert span_for_time(30.0, spans, pad=3.0) == (10.0, 60.0)
    # どの区間にも入らなければ ±pad にフォールバック
    assert span_for_time(99.0, spans, pad=3.0) == (96.0, 102.0)


def test_group_spans_merges_overlapping_place_and_time() -> None:
    items = [
        (0.0, 10.0, (100, 100, 200, 200)),
        (10.0, 20.0, (110, 110, 210, 210)),
        (500.0, 510.0, (100, 100, 200, 200)),  # 時間が離れている → 別
    ]
    groups = group_spans(items, max_gap=45.0)
    assert len(groups) == 2
    assert groups[0][0] == 0.0 and groups[0][1] == 20.0


def _edl() -> Edl:
    return Edl(
        recording_dir="rec",
        source=SourceMedia(
            video_path="v.mp4", width=W, height=H, fps=25, duration_s=120.0
        ),
        segments=[Segment(id="s0", start=0.0, end=120.0)],
        framing=[
            FramingRegion(start=0.0, end=60.0, kind="static"),
            FramingRegion(start=60.0, end=120.0, kind="static"),
        ],
    )


def test_mosaics_from_frames_covers_whole_region() -> None:
    frames = [FrameOcr(time_s=30.0, boxes=[_Box("秘密プロジェクト", (800, 400, 1100, 440))])]
    ovs = mosaics_from_frames(
        frames, W, H, terms=["秘密プロジェクト"], spans=[(0.0, 60.0), (60.0, 120.0)]
    )
    assert len(ovs) == 1
    # 代表フレーム1枚のヒットでも、その区間まるごと隠す（隠し漏れを作らない）
    assert ovs[0].start == 0.0 and ovs[0].end == 60.0


def test_mosaics_from_frames_ignores_non_matching_text() -> None:
    frames = [FrameOcr(time_s=5.0, boxes=[_Box("公開OK", (10, 10, 90, 30))])]
    assert mosaics_from_frames(frames, W, H, terms=["秘密プロジェクト"]) == []


def test_mosaics_from_frames_matches_normalized_text() -> None:
    # OCR が空白を挟んでも・大小文字が違っても当たる（find_mask_regions の正規化）
    frames = [FrameOcr(time_s=5.0, boxes=[_Box("Secret  Code", (100, 100, 400, 140))])]
    assert len(mosaics_from_frames(frames, W, H, terms=["secretcode"])) == 1


def test_scan_ng_mosaics_no_terms_skips_ocr_entirely() -> None:
    called = []

    def ocr(_img):
        called.append(1)
        return []

    assert scan_ng_mosaics(_edl(), terms=[], ocr_fn=ocr, extract_fn=lambda *a: True) == []
    assert not called  # OCR まで到達しない（走査コストを一切払わない）


def test_scan_ng_mosaics_reuses_cache_without_reinference(tmp_path) -> None:
    from wwedit.ocr.screen_scan import save_cache

    cache = tmp_path / "screen_ocr.json"
    save_cache(cache, [FrameOcr(time_s=30.0, boxes=[_Box("秘密", (800, 400, 1100, 440))])])
    called = []

    def ocr(_img):
        called.append(1)
        return []

    ovs = scan_ng_mosaics(
        _edl(), cache_path=cache, terms=["秘密"], ocr_fn=ocr, extract_fn=lambda *a: True
    )
    assert len(ovs) == 1
    assert not called  # キャッシュ再利用＝推論を回さない
