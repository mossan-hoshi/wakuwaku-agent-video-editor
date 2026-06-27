"""[E] フレーミング本番予測器（no_crop 判定 + 暫定固定 crop 箱）。

現状（2026-06-27 訂正・STATUS §2 が正）:
- **no_crop / crop の判定は学習可**。OmniParser 要素検出の空間的広がり(span)が信号で、
  span が小さい＝already-framed＝no_crop、span が全画面充填＝crop（実測 IoU0.705）。
- **crop 枠は専用モデル学習が本筋**（`croptrain.py`）。frozen 埋め込み×単一フレームの弱 probe が
  定数床(IoU≈0.62)で頭打ちしたのは global pool が箱の位置/サイズの空間情報を潰すため。
  dense patch 特徴＋空間ヘッドで床超えを検証中。**ここの固定箱は完成までの暫定**。

重い OmniParser 推論はキャッシュ前提（[[cache-model-forward-not-resweep]]）。pure 関数
（span_area/decide_bbox/bbox_norm_to_pixels）は detector を注入すれば GPU 無しでテスト可。
"""

from __future__ import annotations

from pathlib import Path

Bbox = list[float]

# クリーンラベル平均の暫定固定 crop ボックス(正規化16:9, center≈(0.50,0.53), side≈0.679)。
# 専用モデル(croptrain.py)が床(対crop IoU≈0.62)を超え次第、学習結果へ差し替える。
FIXED_CROP_BBOX: Bbox = [0.16, 0.1932, 0.8391, 0.8723]
NO_CROP_BBOX: Bbox = [0.0, 0.0, 1.0, 1.0]

# 要素 span 面積がこの値未満なら no_crop。conf 未満の検出は span 計算から除外。
# (thr/conf は GT81件で調整した1パラメータ＝軽い過学習注意・アノテ増で再調整可)
SPAN_NO_CROP_THR = 0.95
ELEMENT_CONF = 0.1

__all__ = [
    "FIXED_CROP_BBOX",
    "NO_CROP_BBOX",
    "span_area",
    "decide_bbox",
    "predict_framing",
    "bbox_norm_to_pixels",
]


def span_area(boxes: list[list[float]], conf: float = ELEMENT_CONF) -> float:
    """検出要素 [x0,y0,x1,y1,conf] 群の外接矩形の面積（正規化, 0..1）。conf 未満は無視。"""
    b = [x for x in boxes if len(x) >= 5 and x[4] >= conf]
    if not b:
        return 0.0
    x0 = min(x[0] for x in b)
    y0 = min(x[1] for x in b)
    x1 = max(x[2] for x in b)
    y1 = max(x[3] for x in b)
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def decide_bbox(
    boxes: list[list[float]],
    *,
    thr: float = SPAN_NO_CROP_THR,
    conf: float = ELEMENT_CONF,
) -> tuple[bool, Bbox]:
    """要素検出 boxes から (no_crop, 正規化bbox) を決める。span 小→no_crop, else 固定箱。"""
    if span_area(boxes, conf) < thr:
        return True, list(NO_CROP_BBOX)
    return False, list(FIXED_CROP_BBOX)


def predict_framing(
    image_path: str | Path,
    *,
    detector=None,
    thr: float = SPAN_NO_CROP_THR,
    conf: float = ELEMENT_CONF,
) -> tuple[bool, Bbox]:
    """画像1枚 → (no_crop, 正規化bbox)。

    detector(image_path) -> [[x0,y0,x1,y1,conf], ...] を注入可（省略時は OmniParser 実推論）。
    """
    if detector is None:
        from wwedit.framing.omniparser import detect_elements

        detector = detect_elements
    return decide_bbox(detector(image_path), thr=thr, conf=conf)


def bbox_norm_to_pixels(
    bbox: Bbox, width: int, height: int
) -> tuple[int, int, int, int]:
    """正規化 [x0,y0,x1,y1] → ピクセル (x, y, w, h)（EDL FramingRegion.bbox 形式）。"""
    x0 = round(bbox[0] * width)
    y0 = round(bbox[1] * height)
    x1 = round(bbox[2] * width)
    y1 = round(bbox[3] * height)
    return (x0, y0, max(0, x1 - x0), max(0, y1 - y0))
