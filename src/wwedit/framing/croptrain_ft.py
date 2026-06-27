"""[E] 専用クロップモデル: DINOv2 部分fine-tune（実学習・床超え本命）。

frozen probe は1スケール固定抽出で size 分布を増やせず床(IoU≈0.62)で頭打ち。実学習なら
**scale/pan を GT 同変変換する aug**で「唯一効く信号=size(zoom)」を on-the-fly 増幅できる。

設計（8GB/Turing で安全に・マシンを落とさない）:
- DINOv2 ViT-S/14。**patch_embed/pos/前段は凍結**、**後段 unfreeze_blocks 本＋ヘッド**のみ学習。
  凍結部の入力は requires_grad=False なので autograd はグラフを張らず活性も保存しない＝省メモリ。
- 入力 252×448（18×32=576 patch）。AMP fp16。VRAM 上限は呼び出し側で
  ``set_per_process_memory_fraction`` を必ず設定（単一ジョブ・残存プロセス非kill）。
  → [[no-heavy-gpu-without-consent]]
- ヘッド: reg=(cx,cy,log s)。GT は正規化座標で正方＝3自由度。
- 評価: 収録単位 grouped CV・early stop（外側test不参照）・定数床と並記。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from wwedit.framing.cropmodel import box_to_params, params_to_box
from wwedit.framing.croptrain import grouped_folds, load_crop_items, load_crop_items_multi
from wwedit.framing.evaluate import iou

BACKBONE = "vit_small_patch14_dinov2.lvd142m"
INPUT_HW = (252, 448)  # 18x32 patch = 576 token（16:9）


def _aug_window(rng, box, *, fmin=0.85, fmax=1.0):
    """scale/pan の窓 (ox,oy,f) を返し、GT box を窓内正規化へ co-transform する。

    f=窓の一辺(正規化, ≤1で寄り)。GT中心が窓内に残る範囲で pan。寄りで box は size/f 倍に拡大。
    """
    f = rng.uniform(fmin, fmax)
    cx = (box[0] + box[2]) / 2
    cy = (box[1] + box[3]) / 2
    # 窓は GT 中心を含む。原点 ox,oy は [max(0,c-f), min(c,1-f)] でクランプ
    ox = rng.uniform(max(0.0, cx - f), min(cx, 1.0 - f)) if f < 1.0 else 0.0
    oy = rng.uniform(max(0.0, cy - f), min(cy, 1.0 - f)) if f < 1.0 else 0.0
    nb = [
        (box[0] - ox) / f, (box[1] - oy) / f,
        (box[2] - ox) / f, (box[3] - oy) / f,
    ]
    nb = [min(max(v, 0.0), 1.0) for v in nb]
    return (ox, oy, f), nb


try:
    from torch.utils.data import Dataset as _TorchDataset
except Exception:  # torch 未導入環境（CPUのみのテスト等）でも import 可能に
    _TorchDataset = object


class _CropFrameDS(_TorchDataset):
    """crop 学習用 Dataset（**モジュール最上位＝picklable**＝num_workers>0で並列decode可）。

    クロージャ実装だと Windows spawn で worker へ渡せず num_workers=0 固定になり、GPUが
    フルレスPNGのCPUデコード待ちで餓える（util数%）。ここを並列化して GPU を飽和させる。
    """

    def __init__(self, items, root, *, train, seed):
        self.items = items
        self.root = Path(root)
        self.train = train
        self.seed = seed
        self.rng = np.random.default_rng(seed)

    def __len__(self):
        return len(self.items)

    def __getitem__(self, i):
        import torch
        from PIL import Image

        H, W = INPUT_HW
        it = self.items[i]
        im = Image.open(self.root / it["image"]).convert("RGB")
        box = list(it["bbox"])
        if self.train:
            (ox, oy, f), box = _aug_window(self.rng, box)
            iw, ih = im.size
            im = im.crop((int(ox * iw), int(oy * ih),
                         int((ox + f) * iw), int((oy + f) * ih)))
        im = im.resize((W, H), Image.BICUBIC)
        arr = np.asarray(im, dtype=np.float32) / 255.0
        if self.train:
            # photometric: 輝度/コントラスト/γ + gauss ノイズ（フリップ禁止）
            g = self.rng.uniform(0.8, 1.25)
            b = self.rng.uniform(-0.06, 0.06)
            c = self.rng.uniform(0.85, 1.15)
            arr = np.clip((arr**g) * c + b, 0, 1)
            arr = np.clip(arr + self.rng.normal(0, 0.01, arr.shape).astype(np.float32), 0, 1)
        t = torch.from_numpy(arr).permute(2, 0, 1)
        cx, cy, ls = box_to_params(box)
        return t, torch.tensor([cx, cy, ls], dtype=torch.float32)


def _worker_init(worker_id: int) -> None:
    """各 DataLoader worker の aug rng を worker_id で脱相関させる（並列でも多様なaug）。"""
    info = _torch_worker_info()
    if info is not None:
        info.dataset.rng = np.random.default_rng(info.dataset.seed + 1 + worker_id)


def _torch_worker_info():
    import torch

    return torch.utils.data.get_worker_info()


def _make_dataset(items, root, *, train, seed):
    return _CropFrameDS(items, root, train=train, seed=seed)


def _loader(ds, *, batch, shuffle, num_workers):
    """並列decodeで GPU を飽和させる DataLoader。num_workers>0 で persistent＋prefetch。"""
    from torch.utils.data import DataLoader

    kw = {}
    if num_workers > 0:
        kw = {"persistent_workers": True, "prefetch_factor": 4,
              "worker_init_fn": _worker_init}
    return DataLoader(ds, batch_size=batch, shuffle=shuffle, num_workers=num_workers,
                      pin_memory=True, drop_last=False, **kw)


def _build_model(unfreeze_blocks: int, device):
    import timm
    import torch
    from torch import nn

    bb = timm.create_model(BACKBONE, pretrained=True, num_classes=0, dynamic_img_size=True)
    for p in bb.parameters():
        p.requires_grad_(False)
    n = len(bb.blocks)
    for blk in bb.blocks[n - unfreeze_blocks:]:
        for p in blk.parameters():
            p.requires_grad_(True)
    for p in bb.norm.parameters():
        p.requires_grad_(True)
    cfg = timm.data.resolve_model_data_config(bb)
    mean = torch.tensor(cfg["mean"]).view(1, 3, 1, 1)
    std = torch.tensor(cfg["std"]).view(1, 3, 1, 1)

    class Model(nn.Module):
        def __init__(self):
            super().__init__()
            self.bb = bb
            self.register_buffer("mean", mean)
            self.register_buffer("std", std)
            d = bb.embed_dim
            self.head = nn.Sequential(
                nn.LayerNorm(d), nn.Linear(d, 256), nn.GELU(),
                nn.Dropout(0.3), nn.Linear(256, 3),
            )

        def forward(self, x):
            x = (x - self.mean) / self.std
            feats = self.bb.forward_features(x)  # [B, 1+N, d]
            cls = feats[:, 0]
            return self.head(cls)

    return Model().to(device)


def _train_fold(tr_items, va_items, root, *, device, epochs, batch, unfreeze, seed,
                lr_bb, lr_head, patience, num_workers=6):
    import torch

    tr_ds = _make_dataset(tr_items, root, train=True, seed=seed)
    va_ds = _make_dataset(va_items, root, train=False, seed=0)
    tr_ld = _loader(tr_ds, batch=batch, shuffle=True, num_workers=num_workers)
    va_ld = _loader(va_ds, batch=batch, shuffle=False, num_workers=num_workers)

    torch.manual_seed(seed)
    model = _build_model(unfreeze, device)
    p_bb = [p for p in model.bb.parameters() if p.requires_grad]
    p_head = list(model.head.parameters())
    opt = torch.optim.AdamW(
        [{"params": p_bb, "lr": lr_bb}, {"params": p_head, "lr": lr_head}], weight_decay=1e-2
    )
    scaler = torch.cuda.amp.GradScaler()
    lossf = torch.nn.SmoothL1Loss()

    # target 標準化（train から）
    P = np.array([box_to_params(it["bbox"]) for it in tr_items], dtype=np.float32)
    pmu = torch.tensor(P.mean(0), device=device)
    psd = torch.tensor(P.std(0) + 1e-6, device=device)
    gt_va = np.array([it["bbox"] for it in va_items], dtype=np.float32)

    best_iou, best_pred, bad = -1.0, None, 0
    for _ep in range(epochs):
        model.train()
        for x, y in tr_ld:
            x, y = x.to(device), y.to(device)
            opt.zero_grad()
            with torch.cuda.amp.autocast():
                out = model(x)
                loss = lossf(out, (y - pmu) / psd)
            scaler.scale(loss).backward()
            scaler.step(opt)
            scaler.update()
        # val
        model.eval()
        preds = []
        with torch.no_grad(), torch.cuda.amp.autocast():
            for x, _ in va_ld:
                out = model(x.to(device)).float() * psd + pmu
                preds.append(out.cpu().numpy())
        pv = np.concatenate(preds)
        ious = [iou(params_to_box(*pv[j]), list(gt_va[j])) for j in range(len(va_items))]
        m = float(np.mean(ious))
        if m > best_iou:
            best_iou, best_pred, bad = m, pv, 0
        else:
            bad += 1
            if bad >= patience:
                break
    del model
    torch.cuda.empty_cache()
    return best_iou, best_pred, gt_va


def train_final(
    root: str | Path = "data/framing_anno_full",
    *,
    epochs: int = 25,
    batch: int = 48,
    unfreeze: int = 2,
    lr_bb: float = 2e-4,
    lr_head: float = 1e-3,
    seed: int = 0,
    mem_fraction: float = 0.6,
    extra_roots: list[str | Path] | None = None,
    num_workers: int = 6,
):
    """全 crop 項目で本番モデルを学習し (model, pmu, psd, device) を返す（val無し・固定epoch）。

    extra_roots を渡すと anno_full に手修正 corrections を合算して継続学習する。
    num_workers>0 で並列decode＝GPUを飽和（CPUデコード待ちの餓えを解消）。
    """
    import torch

    device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cuda":
        torch.cuda.set_per_process_memory_fraction(mem_fraction, 0)
        torch.cuda.reset_peak_memory_stats(0)

    items = load_crop_items_multi([root, *extra_roots]) if extra_roots else load_crop_items(root)
    ds = _make_dataset(items, root, train=True, seed=seed)
    ld = _loader(ds, batch=batch, shuffle=True, num_workers=num_workers)
    torch.manual_seed(seed)
    model = _build_model(unfreeze, device)
    p_bb = [p for p in model.bb.parameters() if p.requires_grad]
    opt = torch.optim.AdamW(
        [{"params": p_bb, "lr": lr_bb}, {"params": model.head.parameters(), "lr": lr_head}],
        weight_decay=1e-2,
    )
    scaler = torch.cuda.amp.GradScaler()
    lossf = torch.nn.SmoothL1Loss()
    P = np.array([box_to_params(it["bbox"]) for it in items], dtype=np.float32)
    pmu = torch.tensor(P.mean(0), device=device)
    psd = torch.tensor(P.std(0) + 1e-6, device=device)
    import time

    t0 = time.time()
    for ep in range(epochs):
        model.train()
        for x, y in ld:
            x, y = x.to(device), y.to(device)
            opt.zero_grad()
            with torch.cuda.amp.autocast():
                loss = lossf(model(x), (y.to(device) - pmu) / psd)
            scaler.scale(loss).backward()
            scaler.step(opt)
            scaler.update()
        if ep == 0:
            print(f"[train_final] 1ep {time.time() - t0:.1f}s "
                  f"(bs={batch}, nw={num_workers}, n={len(items)})", flush=True)
    model.eval()
    peak = torch.cuda.max_memory_allocated(0) / 1024**3 if device == "cuda" else 0.0
    print(f"[train_final] {len(items)}件 {epochs}ep {time.time() - t0:.1f}s "
          f"peakVRAM={peak:.2f}GB", flush=True)
    return model, pmu, psd, device


def save_crop_model(model, pmu, psd, path: str | Path, *, unfreeze: int = 2) -> Path:
    """学習済みモデルを load_crop_model 互換形式で保存する（state_dict＋標準化＋unfreeze）。"""
    import torch

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "unfreeze": unfreeze,
            "state_dict": model.state_dict(),
            "pmu": pmu.detach().cpu(),
            "psd": psd.detach().cpu(),
            "backbone": BACKBONE,
            "input_hw": list(INPUT_HW),
        },
        path,
    )
    return path


def predict_items(model, pmu, psd, items, root, device, *, batch: int = 32):
    """各 item（全フレーム・aug無し）の予測 bbox を返す。"""
    import torch
    from PIL import Image

    H, W = INPUT_HW
    boxes = []
    model.eval()
    for k in range(0, len(items), batch):
        chunk = items[k : k + batch]
        xs = []
        for it in chunk:
            im = Image.open(Path(root) / it["image"]).convert("RGB").resize((W, H), Image.BICUBIC)
            xs.append(torch.from_numpy(np.asarray(im, np.float32) / 255).permute(2, 0, 1))
        x = torch.stack(xs).to(device)
        with torch.no_grad(), torch.cuda.amp.autocast():
            out = model(x).float() * psd + pmu
        for row in out.cpu().numpy():
            boxes.append(params_to_box(*row))
    return boxes


def load_crop_model(path: str | Path, device: str = "cpu"):
    """保存済みモデル(crop_model.pt)を読み、(model, pmu, psd) を返す。再学習しない。"""
    import torch

    ck = torch.load(path, map_location=device)
    model = _build_model(ck.get("unfreeze", 2), device)
    model.load_state_dict(ck["state_dict"])
    model.eval()
    return model, ck["pmu"].to(device), ck["psd"].to(device)


def predict_image_boxes(model, pmu, psd, paths, device, *, batch: int = 16):
    """画像パス列 → 正規化 bbox [x0,y0,x1,y1] 列（aug無し・autocast無し＝CPUでも可）。"""
    import torch
    from PIL import Image

    H, W = INPUT_HW
    pm, ps = pmu.cpu().numpy(), psd.cpu().numpy()
    out = []
    model.eval()
    for k in range(0, len(paths), batch):
        xs = []
        for p in paths[k : k + batch]:
            im = Image.open(p).convert("RGB").resize((W, H), Image.BICUBIC)
            xs.append(torch.from_numpy(np.asarray(im, np.float32) / 255).permute(2, 0, 1))
        with torch.no_grad():
            o = model(torch.stack(xs).to(device)).float().cpu().numpy() * ps + pm
        out += [params_to_box(*row) for row in o]
    return out


def apply_model_to_edl(
    edl_path: str | Path,
    model_path: str | Path,
    *,
    device: str = "cpu",
    static_only: bool = True,
) -> int:
    """学習済みモデルで各 framing 区間の代表フレームを推論し framing.bbox(px x,y,w,h)へ書き戻す。

    GPU 不使用既定（device=cpu）＝安全。loading/pending は対象外（static のみ）。書込区間数を返す。
    """
    import tempfile

    from wwedit.edl.schema import load_edl, save_edl
    from wwedit.framing.dataset import _extract_frame

    edl = load_edl(edl_path)
    regs = [r for r in edl.framing if (not static_only or r.kind == "static")]
    if not regs:
        return 0
    model, pmu, psd = load_crop_model(model_path, device)
    tmp = Path(tempfile.mkdtemp())
    paths, valid = [], []
    for i, r in enumerate(regs):
        png = tmp / f"{i}.png"
        if _extract_frame(edl.source.video_path, (r.start + r.end) / 2, png):
            paths.append(png)
            valid.append(r)
    boxes = predict_image_boxes(model, pmu, psd, paths, device)
    W, H = edl.source.width, edl.source.height
    for r, nb in zip(valid, boxes, strict=True):
        r.bbox = (round(nb[0] * W), round(nb[1] * H),
                  round((nb[2] - nb[0]) * W), round((nb[3] - nb[1]) * H))
    save_edl(edl, edl_path)
    return len(valid)


def run_cv_ft(
    root: str | Path = "data/framing_anno_full",
    *,
    k: int = 5,
    epochs: int = 40,
    batch: int = 32,
    unfreeze: int = 2,
    lr_bb: float = 2e-4,
    lr_head: float = 1e-3,
    patience: int = 8,
    mem_fraction: float = 0.6,
    extra_roots: list[str | Path] | None = None,
    num_workers: int = 6,
) -> dict:
    """DINOv2 部分fine-tune の収録単位 grouped CV。VRAM 上限を必ず設定（安全）。

    extra_roots（手修正 corrections 等）を渡すと合算して検証。corrections は専用 timeline
    グループになり grouped CV で別 fold に隔離される＝リーク無しで汎化改善を測れる。
    num_workers>0 で並列decode＝GPU飽和。
    """
    import torch

    device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cuda":
        torch.cuda.set_per_process_memory_fraction(mem_fraction, 0)

    items = load_crop_items_multi([root, *extra_roots]) if extra_roots else load_crop_items(root)
    fold = grouped_folds(items, k)
    model_ious, const_ious, per_fold = [], [], []
    for f in range(k):
        va = [items[i] for i in range(len(items)) if fold[i] == f]
        tr = [items[i] for i in range(len(items)) if fold[i] != f]
        if not va or not tr:
            continue
        _, pv, gt_va = _train_fold(
            tr, va, root, device=device, epochs=epochs, batch=batch,
            unfreeze=unfreeze, seed=1000 + f, lr_bb=lr_bb, lr_head=lr_head, patience=patience,
            num_workers=num_workers,
        )
        cmean = np.array([box_to_params(it["bbox"]) for it in tr], dtype=np.float32).mean(0)
        mi = [iou(params_to_box(*pv[j]), list(gt_va[j])) for j in range(len(va))]
        ci = [iou(params_to_box(*cmean), list(gt_va[j])) for j in range(len(va))]
        model_ious += mi
        const_ious += ci
        per_fold.append({"fold": f, "n_val": len(va),
                         "model_iou": float(np.mean(mi)), "const_iou": float(np.mean(ci))})
        peak = torch.cuda.max_memory_allocated(0) / 1024**3 if device == "cuda" else 0.0
        print(f"[fold {f}] model={np.mean(mi):.4f} const={np.mean(ci):.4f} "
              f"peakVRAM={peak:.2f}GB", flush=True)

    return {
        "n": len(items), "k": k, "unfreeze": unfreeze, "input": list(INPUT_HW),
        "model_mean_iou": float(np.mean(model_ious)),
        "model_median_iou": float(np.median(model_ious)),
        "const_mean_iou": float(np.mean(const_ious)),
        "model_fold_mean": float(np.mean([p["model_iou"] for p in per_fold])),
        "model_fold_std": float(np.std([p["model_iou"] for p in per_fold])),
        "over_0.5": int(np.sum(np.array(model_ious) > 0.5)),
        "over_0.7": int(np.sum(np.array(model_ious) > 0.7)),
        "per_fold": per_fold,
    }
