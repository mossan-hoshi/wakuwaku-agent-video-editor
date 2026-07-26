"""画面OCR共有キャッシュ（1回の推論を用途で使い回す）のテスト。"""

from __future__ import annotations

from wwedit.edl.schema import Edl, FramingRegion, SourceMedia
from wwedit.ocr.engine import OcrBox
from wwedit.ocr.screen_scan import (
    FrameOcr,
    boxes_within,
    ensure_screen_ocr,
    load_cache,
    sample_times,
    save_cache,
    scan_screen_ocr,
)


def _edl(regions: list[FramingRegion]) -> Edl:
    return Edl(
        recording_dir="rec",
        source=SourceMedia(video_path="v.mp4", width=1920, height=1080, fps=25),
        framing=regions,
    )


def test_sample_times_one_per_region() -> None:
    edl = _edl([
        FramingRegion(start=0, end=10, kind="static"),
        FramingRegion(start=10, end=20, kind="static"),
    ])
    ts = sample_times(edl, max_span=0)
    assert len(ts) == 2
    assert all(0 <= t <= 20 for t in ts)


def test_sample_times_adds_samples_for_long_regions() -> None:
    edl = _edl([FramingRegion(start=0, end=100, kind="static")])
    ts = sample_times(edl, max_span=30.0)
    # 代表1枚 ＋ 30s ごとの追加（30/60/90）
    assert len(ts) >= 4
    assert 30.0 in ts and 60.0 in ts and 90.0 in ts


def test_sample_times_skips_loading_regions() -> None:
    edl = _edl([FramingRegion(start=0, end=10, kind="loading")])
    assert sample_times(edl) == []


def test_boxes_within_filters_by_center() -> None:
    boxes = [
        OcrBox(text="in", box=(500, 500, 600, 540)),
        OcrBox(text="out", box=(10, 10, 60, 30)),
    ]
    got = boxes_within(boxes, (192, 108, 1536, 864))
    assert [b.text for b in got] == ["in"]
    # bbox None は全部通す
    assert len(boxes_within(boxes, None)) == 2


def test_scan_screen_ocr_calls_ocr_once_per_time() -> None:
    edl = _edl([
        FramingRegion(start=0, end=10, kind="static"),
        FramingRegion(start=10, end=20, kind="static"),
    ])
    calls = []

    def ocr(png):
        calls.append(png)
        return [OcrBox(text="hello", box=(0, 0, 10, 10))]

    frames = scan_screen_ocr(
        edl, "v.mp4", max_span=0, ocr_fn=ocr, extract_fn=lambda *a: True
    )
    assert len(frames) == 2
    assert len(calls) == 2  # 区間ごとに1回だけ


def test_cache_roundtrip(tmp_path) -> None:
    path = tmp_path / "screen_ocr.json"
    frames = [FrameOcr(time_s=1.5, boxes=[OcrBox(text="あ", box=(1, 2, 3, 4))])]
    save_cache(path, frames, video="v.mp4")
    got = load_cache(path)
    assert len(got) == 1
    assert got[0].time_s == 1.5
    assert got[0].boxes[0].text == "あ"
    assert got[0].boxes[0].box == (1, 2, 3, 4)


def test_load_cache_missing_is_empty(tmp_path) -> None:
    assert load_cache(tmp_path / "nope.json") == []


def test_ensure_screen_ocr_skips_inference_when_cached(tmp_path) -> None:
    path = tmp_path / "screen_ocr.json"
    save_cache(path, [FrameOcr(time_s=0.0, boxes=[])])
    calls = []

    def ocr(png):
        calls.append(png)
        return []

    edl = _edl([FramingRegion(start=0, end=10, kind="static")])
    ensure_screen_ocr(edl, path, ocr_fn=ocr, extract_fn=lambda *a: True)
    assert not calls  # 推論を回さない


def test_ensure_screen_ocr_refresh_reruns_and_saves(tmp_path) -> None:
    path = tmp_path / "screen_ocr.json"
    save_cache(path, [FrameOcr(time_s=0.0, boxes=[])])
    calls = []

    def ocr(png):
        calls.append(png)
        return [OcrBox(text="new", box=(0, 0, 5, 5))]

    edl = _edl([FramingRegion(start=0, end=10, kind="static")])
    got = ensure_screen_ocr(
        edl, path, refresh=True, max_span=0, ocr_fn=ocr, extract_fn=lambda *a: True
    )
    assert len(calls) == 1
    assert got[0].boxes[0].text == "new"
    assert load_cache(path)[0].boxes[0].text == "new"  # 保存もされている
