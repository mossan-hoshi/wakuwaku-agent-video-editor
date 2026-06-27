"""[E] フレーミング評価ハーネス（evaluate.py）の単体テスト。

重い推論（OmniParser/cv2）は呼ばない：omni予測器は検出キャッシュをファイル注入して検証する。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wwedit.framing.evaluate import (
    analyze_gt,
    evaluate,
    gt_bbox,
    iou,
    load_gt,
    mean_bbox_predictor,
    omni_bbox_predictor,
)


def test_iou_identical():
    assert iou([0.1, 0.1, 0.5, 0.5], [0.1, 0.1, 0.5, 0.5]) == pytest.approx(1.0)


def test_iou_disjoint():
    assert iou([0.0, 0.0, 0.2, 0.2], [0.5, 0.5, 0.9, 0.9]) == 0.0


def test_iou_half_overlap():
    # a=[0,0,2,2]面積4, b=[1,0,3,2]面積4, 交差=[1,0,2,2]面積2 → 2/(4+4-2)=1/3
    assert iou([0, 0, 2, 2], [1, 0, 3, 2]) == pytest.approx(1 / 3)


def test_iou_degenerate_zero_area():
    assert iou([0.5, 0.5, 0.5, 0.5], [0.0, 0.0, 1.0, 1.0]) == 0.0


def test_gt_bbox_no_crop():
    assert gt_bbox({"no_crop": True, "bbox": [0.2, 0.2, 0.4, 0.4]}) == [0.0, 0.0, 1.0, 1.0]


def test_gt_bbox_cropped_copies():
    item = {"no_crop": False, "bbox": [0.2, 0.2, 0.4, 0.4]}
    b = gt_bbox(item)
    assert b == [0.2, 0.2, 0.4, 0.4]
    b[0] = 9.9  # 返り値はコピー＝元を汚さない
    assert item["bbox"][0] == 0.2


def test_analyze_gt_counts_and_degenerate_excluded():
    gt = [
        {"no_crop": True, "bbox": [0, 0, 1, 1]},
        {"no_crop": False, "bbox": [0.2, 0.1, 0.8, 0.7]},  # w=0.6
        {"no_crop": False, "bbox": [0.5, 0.5, 0.5, 0.5]},  # 退化(w=0)→統計除外
    ]
    a = analyze_gt(gt)
    assert a["n"] == 3
    assert a["no_crop"] == 1
    assert a["cropped"] == 2
    assert a["no_crop_rate"] == pytest.approx(1 / 3)
    assert a["width"]["mean"] == pytest.approx(0.6)  # 退化を除外し1件のみ


def test_mean_bbox_predictor_ignores_image():
    gt = [
        {"no_crop": True, "bbox": [0, 0, 1, 1]},
        {"no_crop": False, "bbox": [0.0, 0.0, 0.4, 0.4]},
    ]
    pred = mean_bbox_predictor(gt)
    # 平均: no_crop=[0,0,1,1] と [0,0,.4,.4] の平均 = [0,0,0.7,0.7]
    assert pred("any/path.png") == pytest.approx([0.0, 0.0, 0.7, 0.7])


def _write_dataset(tmp_path: Path, items: list[dict]) -> Path:
    (tmp_path / "dataset.json").write_text(json.dumps(items), encoding="utf-8")
    return tmp_path


def test_load_gt_only_corrected(tmp_path):
    _write_dataset(
        tmp_path,
        [
            {"id": "a", "corrected": True, "no_crop": True, "bbox": [0, 0, 1, 1]},
            {"id": "b", "corrected": False, "no_crop": False, "bbox": [0, 0, 0.5, 0.5]},
        ],
    )
    gt = load_gt(tmp_path)
    assert [x["id"] for x in gt] == ["a"]


def test_omni_predictor_span_gate(tmp_path):
    items = [
        {"id": "nc", "corrected": True, "no_crop": True, "image": "frames/nc.png",
         "bbox": [0, 0, 1, 1]},
        {"id": "cr", "corrected": True, "no_crop": False, "image": "frames/cr.png",
         "bbox": [0.2, 0.2, 0.8, 0.8]},
    ]
    _write_dataset(tmp_path, items)
    cache = {
        # 広がり小(area=0.01<thr) → no_crop と判定されるべき
        "nc": [[0.45, 0.45, 0.55, 0.55, 0.9]],
        # 全画面充填(area≈1.0>=thr) → crop=mean_crop にフォールバック
        "cr": [[0.0, 0.0, 0.1, 0.1, 0.9], [0.9, 0.9, 1.0, 1.0, 0.9]],
    }
    cache_path = tmp_path / "omni.json"
    cache_path.write_text(json.dumps(cache), encoding="utf-8")

    gt = load_gt(tmp_path)
    pred = omni_bbox_predictor(gt, tmp_path, cache_path=cache_path, thr=0.95)
    assert pred(tmp_path / "frames/nc.png") == [0.0, 0.0, 1.0, 1.0]
    # cropped は1件のみ＝mean_crop はその bbox 自身
    assert pred(tmp_path / "frames/cr.png") == pytest.approx([0.2, 0.2, 0.8, 0.8])


def test_evaluate_metrics(tmp_path):
    items = [
        {"id": "a", "corrected": True, "no_crop": True, "image": "frames/a.png",
         "bbox": [0, 0, 1, 1]},
        {"id": "b", "corrected": True, "no_crop": False, "image": "frames/b.png",
         "bbox": [0.0, 0.0, 0.5, 0.5]},
    ]
    _write_dataset(tmp_path, items)
    gt = load_gt(tmp_path)
    # 常に[0,0,1,1]を返す予測器 → a:IoU1.0, b:IoU=0.25
    res = evaluate(lambda _p: [0.0, 0.0, 1.0, 1.0], gt, tmp_path)
    assert res["n"] == 2
    assert res["mean_iou"] == pytest.approx((1.0 + 0.25) / 2)
    assert res["over_0.5"] == 1
    assert res["over_0.7"] == 1
