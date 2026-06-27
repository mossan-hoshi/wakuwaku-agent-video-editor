"""[E] 専用クロップモデル: 収録単位 grouped CV 学習・評価。

frozen DINOv2 dense 特徴（事前キャッシュ）の上に軽量ヘッドだけ学習する Phase 0 実験。
**ゲート基準（Deep Research）**: 入力非依存の定数（GT平均箱）= 床、ここで mean IoU 0.62 が頭打ち。
dense 特徴＋空間ヘッドがこれを明確に超えれば「crop は学習可」が実証される。

リーク防止が最重要: 単一クリエイターの録画は時間相関が強く、**同一収録(timeline)のフレームは
必ず同 fold**（grouped CV）。さもなくば楽観バイアスで床超えを誤検出する。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from wwedit.framing.cropfeat import (
    DEFAULT_BACKBONE,
    DEFAULT_GRID,
    extract_features,
    load_features,
)
from wwedit.framing.cropmodel import box_to_params, make_head, params_to_box
from wwedit.framing.evaluate import iou

DEFAULT_CACHE = "data/framing_anno_full/_feat_cache"


def load_crop_items(root: str | Path) -> list[dict]:
    """corrected & not rejected & not no_crop & フレーム実在 の crop 学習項目。"""
    root = Path(root)
    data = json.loads((root / "dataset.json").read_text(encoding="utf-8"))
    out = []
    for x in data:
        if not (x.get("corrected") and not x.get("rejected")):
            continue
        if x.get("no_crop"):
            continue
        if (x["bbox"][2] - x["bbox"][0]) <= 0:  # 退化アノテ除外
            continue
        if not (root / x["image"]).exists():
            continue
        out.append(x)
    return out


def load_crop_items_multi(roots: list[str | Path]) -> list[dict]:
    """複数 root の crop 学習項目を結合する（継続学習で anno_full ＋ corrections を合算）。

    各 item の ``image`` を **絶対パス**へ書き換える＝後段は `Path(<任意root>)/image` で
    そのまま解決できる（絶対パス連結は pathlib が右側を採用するため root 非依存）。
    重複 id は後勝ち（corrections の再収穫を反映）。
    """
    merged: dict[str, dict] = {}
    for root in roots:
        root = Path(root)
        for x in load_crop_items(root):
            y = dict(x)
            y["image"] = str((root / x["image"]).resolve())
            merged[y["id"]] = y
    return list(merged.values())


def grouped_folds(items: list[dict], k: int = 5) -> list[int]:
    """各 item の fold 番号(0..k-1)を返す。**収録(timeline)単位**で割当（決定的）。

    収録を sha1 でソートし件数バランスを取りつつ各 fold へ貪欲割当（リーク防止＋fold 均等）。
    """
    from collections import defaultdict

    by_rec: dict[str, list[int]] = defaultdict(list)
    for i, it in enumerate(items):
        by_rec[it["timeline"]].append(i)
    # 件数降順 + 名前ハッシュで決定的に並べ、件数最小の fold へ貪欲投入（均等化）
    recs = sorted(
        by_rec, key=lambda r: (-len(by_rec[r]), hashlib.sha1(r.encode()).hexdigest())
    )
    load = [0] * k
    fold = [0] * len(items)
    for r in recs:
        f = min(range(k), key=lambda j: load[j])
        for i in by_rec[r]:
            fold[i] = f
        load[f] += len(by_rec[r])
    return fold


def _train_head(
    feats_tr: np.ndarray,
    params_tr: np.ndarray,
    feats_va: np.ndarray,
    *,
    epochs: int,
    lr: float,
    wd: float,
    seed: int,
    device: str,
):
    """標準化した (cx,cy,log_s) を SmoothL1 で回帰。val の生パラメタ予測を返す。"""
    import torch

    torch.manual_seed(seed)
    mu = params_tr.mean(0)
    sd = params_tr.std(0) + 1e-6
    yt = torch.tensor((params_tr - mu) / sd, dtype=torch.float32, device=device)
    xt = torch.tensor(feats_tr, dtype=torch.float32, device=device)
    xv = torch.tensor(feats_va, dtype=torch.float32, device=device)

    n_patch, dim = feats_tr.shape[1], feats_tr.shape[2]
    head = make_head(dim, n_patch).to(device)
    opt = torch.optim.AdamW(head.parameters(), lr=lr, weight_decay=wd)
    lossf = torch.nn.SmoothL1Loss()
    head.train()
    bs = 64
    n = xt.shape[0]
    for _ in range(epochs):
        perm = torch.randperm(n, device=device)
        for k in range(0, n, bs):
            idx = perm[k : k + bs]
            opt.zero_grad()
            out = head(xt[idx])
            loss = lossf(out, yt[idx])
            loss.backward()
            opt.step()
    head.eval()
    with torch.no_grad():
        pv = head(xv).cpu().numpy() * sd + mu  # 生パラメタへ戻す
    return pv, mu, sd


def run_cv(
    root: str | Path = "data/framing_anno_full",
    cache_dir: str | Path = DEFAULT_CACHE,
    *,
    backbone: str = DEFAULT_BACKBONE,
    grid: tuple[int, int] = DEFAULT_GRID,
    k: int = 5,
    epochs: int = 250,
    lr: float = 1e-3,
    wd: float = 1e-4,
    device: str | None = None,
) -> dict:
    """収録単位 grouped k-fold CV。mean IoU・床(定数)・fold 別を返す。"""
    import torch

    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    items = load_crop_items(root)
    ids = [x["id"] for x in items]
    paths = [str(Path(root) / x["image"]) for x in items]
    extract_features(ids, paths, cache_dir, backbone=backbone, grid=grid, device=device)

    feats = load_features(cache_dir, ids)  # [M, N, C]
    gt = np.array([x["bbox"] for x in items], dtype=np.float32)
    params = np.array([box_to_params(x["bbox"]) for x in items], dtype=np.float32)
    fold = grouped_folds(items, k)

    model_ious: list[float] = []
    const_ious: list[float] = []
    per_fold = []
    for f in range(k):
        va = [i for i in range(len(items)) if fold[i] == f]
        tr = [i for i in range(len(items)) if fold[i] != f]
        if not va or not tr:
            continue
        pv, _, _ = _train_head(
            feats[tr], params[tr], feats[va],
            epochs=epochs, lr=lr, wd=wd, seed=1000 + f, device=device,
        )
        # 定数床: train 平均パラメタを全 val に当てる
        cmean = params[tr].mean(0)
        m_i, c_i = [], []
        for j, vi in enumerate(va):
            mbox = params_to_box(*pv[j])
            cbox = params_to_box(*cmean)
            m_i.append(iou(mbox, list(gt[vi])))
            c_i.append(iou(cbox, list(gt[vi])))
        model_ious += m_i
        const_ious += c_i
        per_fold.append(
            {"fold": f, "n_val": len(va), "model_iou": float(np.mean(m_i)),
             "const_iou": float(np.mean(c_i))}
        )

    return {
        "n": len(items),
        "k": k,
        "backbone": backbone,
        "grid": list(grid),
        "model_mean_iou": float(np.mean(model_ious)),
        "model_median_iou": float(np.median(model_ious)),
        "const_mean_iou": float(np.mean(const_ious)),
        "model_fold_mean": float(np.mean([p["model_iou"] for p in per_fold])),
        "model_fold_std": float(np.std([p["model_iou"] for p in per_fold])),
        "over_0.5": int(np.sum(np.array(model_ious) > 0.5)),
        "over_0.7": int(np.sum(np.array(model_ious) > 0.7)),
        "per_fold": per_fold,
    }
