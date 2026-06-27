"""[E] フレーミングモデル評価ハーネス（床ベースライン + 予測器の IoU 評価）。

定数床（GT平均箱・入力非依存）や CV/OmniParser ヒューリスティックの IoU を測る共通土台。
専用クロップモデルの本学習・収録単位 grouped CV は `croptrain.py` 側（床超えの実証用）。
ここの `iou` は両者で共用する。

bbox は元フレーム上の正規化クロップ矩形 [x0,y0,x1,y1]（0..1）。16:9固定のため、16:9フレーム
内の16:9クロップは正規化座標では**正方形**（幅=高さ）になる。no_crop（クロップ無し）の正解は
全画面 [0,0,1,1]。

cv2/numpy は ``predict_content_bbox`` 内で遅延importする（CLIロードを壊さないため）。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

Bbox = list[float]


def iou(a: Bbox, b: Bbox) -> float:
    """2つの正規化bbox [x0,y0,x1,y1] の IoU。退化矩形(面積0)は union=0→0.0。"""
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def load_gt(dataset_dir: str | Path) -> list[dict]:
    """dataset.json から corrected（アノテ済み）項目のみ返す。読むだけ・書き換えない。"""
    data = json.loads((Path(dataset_dir) / "dataset.json").read_text(encoding="utf-8"))
    return [x for x in data if x.get("corrected")]


def gt_bbox(item: dict) -> Bbox:
    """項目の正解bbox。no_crop は全画面 [0,0,1,1]。"""
    return [0.0, 0.0, 1.0, 1.0] if item.get("no_crop") else list(item["bbox"])


def _stats(vals: list[float]) -> dict[str, float]:
    if not vals:
        return {"mean": 0.0, "std": 0.0, "min": 0.0, "max": 0.0}
    n = len(vals)
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / n
    return {"mean": mean, "std": var**0.5, "min": min(vals), "max": max(vals)}


def analyze_gt(gt: list[dict]) -> dict:
    """GTの分布（中心x/y・幅・no_crop率）。中心/幅はクロップ有り項目のみで集計。"""
    n = len(gt)
    nocrop = [x for x in gt if x.get("no_crop")]
    cropped = [x for x in gt if not x.get("no_crop")]
    cxs, cys, ws = [], [], []
    for x in cropped:
        b = x["bbox"]
        w = b[2] - b[0]
        if w <= 0:  # 退化アノテ（点）は中心/幅統計から除外
            continue
        cxs.append((b[0] + b[2]) / 2)
        cys.append((b[1] + b[3]) / 2)
        ws.append(w)
    return {
        "n": n,
        "no_crop": len(nocrop),
        "no_crop_rate": len(nocrop) / n if n else 0.0,
        "cropped": len(cropped),
        "center_x": _stats(cxs),
        "center_y": _stats(cys),
        "width": _stats(ws),
    }


def mean_bbox_predictor(gt: list[dict]) -> Callable[[str | Path], Bbox]:
    """GT平均bboxを常に返す予測器（=入力非依存の床ベースライン）。

    no_crop も含めた全 corrected の正解bboxの平均。学習モデルが最低限超えるべき水準。
    """
    boxes = [gt_bbox(x) for x in gt]
    n = len(boxes) or 1
    mean = [sum(b[i] for b in boxes) / n for i in range(4)]

    def predict(_image_path: str | Path) -> Bbox:
        return list(mean)

    return predict


def omni_bbox_predictor(
    gt: list[dict],
    dataset_dir: str | Path,
    *,
    cache_path: str | Path | None = None,
    thr: float = 0.95,
    conf: float = 0.1,
) -> Callable[[str | Path], Bbox]:
    """OmniParser要素検出ベースのハイブリッド予測器。

    プラン[3]の知見の実装:
    - **no_crop/crop の判定**に要素の空間的広がり(span)を使う。span が狭い→already-framed→
      no_crop([0,0,1,1])、span が全画面充填→ズーム前提の素材→crop。
    - **crop先の位置**は要素分布から特定不能（編集者の意図＝学習モデル待ち。要素重心/密度
      ピーク窓いずれも床の定数mean_cropに劣ると実測）。当面は床＝GTクロップ平均bboxへ。

    実測（GT81件・床0.607）: mean IoU 0.705 / no_crop 0.915 / cropped 0.606。
    LOOCV 0.714（汎化で悪化せず＝過学習なし）、thr感度0.80〜0.97で0.68〜0.72のプラトー。
    検出は事前キャッシュ（`omniparser.build_cache`）を読む。キャッシュに無いidは実推論。
    """
    from wwedit.framing.omniparser import DEFAULT_CACHE, detect_elements, load_cache

    cache_path = cache_path or DEFAULT_CACHE
    try:
        cache = load_cache(cache_path)
    except FileNotFoundError:
        cache = {}

    cropped = [x["bbox"] for x in gt if not x.get("no_crop") and (x["bbox"][2] - x["bbox"][0]) > 0]
    n = len(cropped) or 1
    mean_crop = [sum(b[i] for b in cropped) / n for i in range(4)]

    def _span_area(boxes: list[list[float]]) -> float:
        b = [x for x in boxes if x[4] >= conf]
        if not b:
            return 0.0
        x0, y0 = min(x[0] for x in b), min(x[1] for x in b)
        x1, y1 = max(x[2] for x in b), max(x[3] for x in b)
        return (x1 - x0) * (y1 - y0)

    def predict(image_path: str | Path) -> Bbox:
        cid = Path(image_path).stem
        boxes = cache.get(cid)
        if boxes is None:  # キャッシュ未生成のフレームは実推論
            boxes = detect_elements(image_path)
        return [0.0, 0.0, 1.0, 1.0] if _span_area(boxes) < thr else list(mean_crop)

    return predict


def predict_content_bbox(image_path: str | Path) -> Bbox:
    """エッジ密度で内容領域を推定し、16:9（正規化で正方）クロップbboxを返す。

    Sobelでエッジマグニチュード→行/列ごとのエッジ質量の累積分布で中央80%を内容範囲とする。
    背景(均一)よりスライド/画面領域がエッジ濃い、という前提のヒューリスティック。
    """
    import cv2
    import numpy as np

    img = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return [0.0, 0.0, 1.0, 1.0]
    h, w = img.shape[:2]
    gx = cv2.Sobel(img, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(img, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.abs(gx) + np.abs(gy)

    def central_range(mass: np.ndarray, lo: float = 0.1, hi: float = 0.9) -> tuple[float, float]:
        total = float(mass.sum())
        if total <= 0:
            return 0.0, 1.0
        cum = np.cumsum(mass) / total
        i0 = int(np.searchsorted(cum, lo))
        i1 = int(np.searchsorted(cum, hi))
        n = len(mass)
        return i0 / n, min(1.0, (i1 + 1) / n)

    cx0, cx1 = central_range(mag.sum(axis=0))  # 列方向（x範囲）
    cy0, cy1 = central_range(mag.sum(axis=1))  # 行方向（y範囲）

    # 16:9（=正規化で正方）へ整形。中心を保ち、辺は長い方に合わせる。
    cx, cy = (cx0 + cx1) / 2, (cy0 + cy1) / 2
    side = max(cx1 - cx0, cy1 - cy0)
    half = side / 2
    return [
        max(0.0, cx - half),
        max(0.0, cy - half),
        min(1.0, cx + half),
        min(1.0, cy + half),
    ]


def evaluate(
    predict_fn: Callable[[str | Path], Bbox], gt: list[dict], dataset_dir: str | Path
) -> dict:
    """予測器をGTで評価。各項目の IoU を集計（mean/median, >0.5, >0.7 件数）。"""
    root = Path(dataset_dir)
    ious: list[float] = []
    for item in gt:
        pred = predict_fn(root / item["image"])
        ious.append(iou(pred, gt_bbox(item)))
    ious.sort()
    n = len(ious)
    median = ious[n // 2] if n else 0.0
    return {
        "n": n,
        "mean_iou": sum(ious) / n if n else 0.0,
        "median_iou": median,
        "over_0.5": sum(1 for v in ious if v > 0.5),
        "over_0.7": sum(1 for v in ious if v > 0.7),
    }
