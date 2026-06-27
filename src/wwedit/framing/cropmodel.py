"""[E] 専用クロップモデル: 箱パラメタ化と軽量空間ヘッド。

GT 観察（実データ）: 16:9 フレーム内の 16:9 クロップは**正規化座標で正方**（幅=高さ）。
よって箱は **中心(cx,cy)＋一辺 s の3自由度**へ縮約できる。Deep Research の「center+log-scale 回帰」
＝この分解。中心はほぼ一定・size が主変動なので、4座標直回帰より分散効率が良い。

``box_to_params``/``params_to_box`` は純関数（torch 不要・テスト可）。``CropHead`` は dense patch
特徴 [B,N,C] を attention pooling して (cx,cy,log s) を出す小ヘッド（frozen backbone の下流）。
"""

from __future__ import annotations

import math

Bbox = list[float]


def box_to_params(bbox: Bbox) -> tuple[float, float, float]:
    """正規化 bbox [x0,y0,x1,y1] → (cx, cy, log_s)。s=一辺(=幅と高さの平均)。"""
    x0, y0, x1, y1 = bbox
    s = max(1e-4, ((x1 - x0) + (y1 - y0)) / 2)
    return (x0 + x1) / 2, (y0 + y1) / 2, math.log(s)


def params_to_box(cx: float, cy: float, log_s: float) -> Bbox:
    """(cx, cy, log_s) → 正方 bbox [x0,y0,x1,y1]、[0,1] にクランプ。"""
    s = math.exp(log_s)
    h = s / 2
    return [
        min(max(cx - h, 0.0), 1.0),
        min(max(cy - h, 0.0), 1.0),
        min(max(cx + h, 0.0), 1.0),
        min(max(cy + h, 0.0), 1.0),
    ]


def make_head(dim: int, n_patch: int, *, hidden: int = 256, dropout: float = 0.3):
    """attention pooling + 回帰MLP の小ヘッドを返す（torch を遅延 import）。"""
    import torch
    from torch import nn

    class CropHead(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.norm = nn.LayerNorm(dim)
            # patch 位置を保持する学習可能 positional embedding（size 推定に空間配置が要る）
            self.pos = nn.Parameter(torch.zeros(n_patch, dim))
            self.q = nn.Parameter(torch.randn(dim) * dim**-0.5)
            self.kproj = nn.Linear(dim, dim)
            self.vproj = nn.Linear(dim, dim)
            self.mlp = nn.Sequential(
                nn.LayerNorm(dim),
                nn.Linear(dim, hidden),
                nn.GELU(),
                nn.Dropout(dropout),
                nn.Linear(hidden, 3),
            )

        def forward(self, feats):  # feats: [B, N, dim]
            f = self.norm(feats) + self.pos
            k = self.kproj(f)
            v = self.vproj(f)
            att = (k @ self.q) * dim**-0.5  # [B, N]
            w = att.softmax(dim=-1).unsqueeze(-1)  # [B, N, 1]
            pooled = (w * v).sum(dim=1)  # [B, dim]
            return self.mlp(pooled)  # [B, 3] = (cx,cy,log_s) 標準化空間

    return CropHead()
