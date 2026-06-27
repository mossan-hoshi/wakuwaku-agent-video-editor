"""framing.predict（本番予測器: no_crop判定 + 固定crop箱）のテスト。"""

from __future__ import annotations

from wwedit.framing.predict import (
    FIXED_CROP_BBOX,
    NO_CROP_BBOX,
    bbox_norm_to_pixels,
    decide_bbox,
    predict_framing,
    span_area,
)


def test_span_area_empty_and_conf_filter():
    assert span_area([]) == 0.0
    # conf 未満の検出は無視 → span 0
    assert span_area([[0.0, 0.0, 1.0, 1.0, 0.01]], conf=0.1) == 0.0


def test_span_area_bounding_box():
    boxes = [[0.1, 0.2, 0.3, 0.4, 0.9], [0.5, 0.1, 0.9, 0.6, 0.9]]
    # 外接 = [0.1,0.1,0.9,0.6] → 面積 0.8*0.5
    assert abs(span_area(boxes) - 0.8 * 0.5) < 1e-9


def test_decide_bbox_no_crop_when_span_small():
    # 小さくまとまった要素群 = already-framed = no_crop
    boxes = [[0.4, 0.4, 0.5, 0.5, 0.9]]
    no_crop, bbox = decide_bbox(boxes, thr=0.95)
    assert no_crop is True
    assert bbox == NO_CROP_BBOX


def test_decide_bbox_crop_when_span_fills_frame():
    # 全画面に広がる要素群 = crop 素材 → 固定箱
    boxes = [[0.0, 0.0, 0.05, 0.05, 0.9], [0.95, 0.95, 1.0, 1.0, 0.9]]
    no_crop, bbox = decide_bbox(boxes, thr=0.95)
    assert no_crop is False
    assert bbox == FIXED_CROP_BBOX


def test_predict_framing_accepts_injected_detector():
    # detector を注入すれば GPU 無しで動く
    full = [[0.0, 0.0, 1.0, 1.0, 0.9]]
    no_crop, bbox = predict_framing("dummy.png", detector=lambda _p: full)
    assert no_crop is False and bbox == FIXED_CROP_BBOX

    tight = [[0.45, 0.45, 0.55, 0.55, 0.9]]
    no_crop, bbox = predict_framing("dummy.png", detector=lambda _p: tight)
    assert no_crop is True and bbox == NO_CROP_BBOX


def test_bbox_norm_to_pixels():
    # 固定箱を 1920x1080 へ
    x, y, w, h = bbox_norm_to_pixels(FIXED_CROP_BBOX, 1920, 1080)
    assert (x, y) == (round(0.16 * 1920), round(0.1932 * 1080))
    assert w == round(0.8391 * 1920) - round(0.16 * 1920)
    assert h == round(0.8723 * 1080) - round(0.1932 * 1080)
    # no_crop は全画面
    assert bbox_norm_to_pixels(NO_CROP_BBOX, 1920, 1080) == (0, 0, 1920, 1080)
