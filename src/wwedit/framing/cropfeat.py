"""[E] 専用クロップモデル: frozen backbone の dense patch 特徴の抽出とキャッシュ。

Deep Research の結論（要点・我々のデータで再評価済）:
- frozen 埋め込みが IoU 0.62 で頭打ちした主因は **global pool が箱の位置/サイズに要る
  空間情報を潰す**こと。→ CLS でなく **dense patch 特徴マップ**を空間保持して軽量ヘッドへ。
- backbone forward は重い→**1回だけ回して特徴を ``id.npy`` にキャッシュ**
  （[[cache-model-forward-not-resweep]]）。以降の CV/ヘッド学習は実質ヘッドだけ＝数分。8GB は fp16。

backbone は差し替え可能（既定=DINOv2 ViT-S/14, Apache-2.0, ゲート無し）。DINOv3 へは名前だけ変える。
画像は 16:9 のまま patch 倍数へストレッチ（正規化 bbox はスケール不変なので幾何は保たれる）。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

# DINOv2 ViT-S/14: 1 prefix(cls) + N patch tokens, embed 384。dynamic_img_size で 16:9 入力可。
DEFAULT_BACKBONE = "vit_small_patch14_dinov2.lvd142m"
PATCH = 14
# patch グリッド (rows, cols)。16:9≈1.778。28x50=1400 patch（cols/rows=1.786）。
DEFAULT_GRID = (28, 50)
NUM_PREFIX = 1


def grid_to_size(grid: tuple[int, int]) -> tuple[int, int]:
    """patch グリッド (R, C) → 入力ピクセル (H, W)。"""
    return grid[0] * PATCH, grid[1] * PATCH


def _load_backbone(name: str, device: str):
    import timm
    import torch

    model = (
        timm.create_model(name, pretrained=True, num_classes=0, dynamic_img_size=True)
        .eval()
        .to(device)
    )
    cfg = timm.data.resolve_model_data_config(model)
    mean = torch.tensor(cfg["mean"], device=device).view(1, 3, 1, 1)
    std = torch.tensor(cfg["std"], device=device).view(1, 3, 1, 1)
    return model, mean, std


def _to_tensor(img_path: str | Path, size: tuple[int, int]):
    """画像を (H,W) へストレッチして float tensor [3,H,W] (0..1)。"""
    import numpy as _np
    import torch
    from PIL import Image

    h, w = size
    im = Image.open(img_path).convert("RGB").resize((w, h), Image.BICUBIC)
    arr = _np.asarray(im, dtype=_np.float32) / 255.0
    return torch.from_numpy(arr).permute(2, 0, 1)


def extract_features(
    ids: list[str],
    image_paths: list[str | Path],
    cache_dir: str | Path,
    *,
    backbone: str = DEFAULT_BACKBONE,
    grid: tuple[int, int] = DEFAULT_GRID,
    device: str | None = None,
    batch: int = 8,
    overwrite: bool = False,
) -> Path:
    """各画像の dense patch 特徴 [N, C](fp16) を ``cache_dir/<id>.npy`` に保存する。

    既存のキャッシュはスキップ（resume 可）。grid とバックボーン名は ``meta.json`` に記録し、
    不一致なら学習側で検出できるようにする。返り値はキャッシュディレクトリ。
    """
    import torch

    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    size = grid_to_size(grid)
    n_patch = grid[0] * grid[1]

    todo = [
        (i, p)
        for i, p in zip(ids, image_paths, strict=True)
        if overwrite or not (cache_dir / f"{i}.npy").exists()
    ]
    if todo:
        device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        model, mean, std = _load_backbone(backbone, device)
        for k in range(0, len(todo), batch):
            chunk = todo[k : k + batch]
            x = torch.stack([_to_tensor(p, size) for _, p in chunk]).to(device)
            x = (x - mean) / std
            with torch.no_grad():
                feats = model.forward_features(x)  # [B, prefix+N, C]
            patch = feats[:, NUM_PREFIX : NUM_PREFIX + n_patch].float().cpu().numpy()
            for (cid, _), f in zip(chunk, patch, strict=True):
                np.save(cache_dir / f"{cid}.npy", f.astype(np.float16))

    (cache_dir / "meta.json").write_text(
        json.dumps({"backbone": backbone, "grid": list(grid), "num_patch": n_patch}, indent=2),
        encoding="utf-8",
    )
    return cache_dir


def load_features(cache_dir: str | Path, ids: list[str]) -> np.ndarray:
    """キャッシュから [len(ids), N, C](float32) を積んで返す。"""
    cache_dir = Path(cache_dir)
    arrs = [np.load(cache_dir / f"{i}.npy").astype(np.float32) for i in ids]
    return np.stack(arrs)
