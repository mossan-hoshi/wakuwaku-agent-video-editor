"""[E] OmniParser V2 アイコン検出器(YOLOv8)で GUI 要素 bbox を取る（プラン[3]の検出側）。

「非interactable領域の補集合＝コンテンツ領域」推論のうち、interactable 要素(chrome=タブ/
アドレスバー/ツールバー/タスクバー等)の検出を担う。content vs chrome の明示分類器は無いので、
要素の空間分布から「周辺chrome帯を除いた中央の大領域」をコンテンツ領域と推定する後処理は
``evaluate`` 側に置く。

重い推論は1回だけ（メモリ: cache-model-forward-not-resweep）。検出結果を JSON にキャッシュし、
後処理(コンテンツ領域推定)のパラメータ掃引では再推論しない。GPU(CUDA)で実行。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

WEIGHTS = Path("models/omniparser/icon_detect/model.pt")
DEFAULT_CACHE = Path("data/framing_ds/omni_boxes.json")


@lru_cache(maxsize=1)
def _model():
    from ultralytics import YOLO

    return YOLO(str(WEIGHTS))


def detect_elements(
    image_path: str | Path, conf: float = 0.05, imgsz: int = 1280
) -> list[list[float]]:
    """1フレームの GUI 要素 bbox を正規化 [x0,y0,x1,y1,conf] のリストで返す（GPU）。"""
    import torch

    dev = 0 if torch.cuda.is_available() else "cpu"
    r = _model().predict(
        str(image_path), conf=conf, imgsz=imgsz, device=dev, verbose=False
    )[0]
    h, w = r.orig_shape
    out: list[list[float]] = []
    for b, c in zip(r.boxes.xyxy.tolist(), r.boxes.conf.tolist(), strict=False):
        out.append([b[0] / w, b[1] / h, b[2] / w, b[3] / h, float(c)])
    return out


def build_cache(
    gt: list[dict],
    dataset_dir: str | Path,
    out_path: str | Path = DEFAULT_CACHE,
    conf: float = 0.05,
    imgsz: int = 1280,
) -> dict[str, list[list[float]]]:
    """全フレームを1回検出し {id: [[x0,y0,x1,y1,conf],...]} を JSON 保存（重い推論はここだけ）。"""
    root = Path(dataset_dir)
    cache: dict[str, list[list[float]]] = {}
    for it in gt:
        cache[it["id"]] = detect_elements(root / it["image"], conf=conf, imgsz=imgsz)
    Path(out_path).write_text(json.dumps(cache), encoding="utf-8")
    return cache


def load_cache(path: str | Path = DEFAULT_CACHE) -> dict[str, list[list[float]]]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
