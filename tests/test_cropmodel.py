"""[E] 専用クロップモデルの純関数テスト（torch/timm 不要）。"""

from __future__ import annotations

import math

import numpy as np

from wwedit.framing.cropfeat import grid_to_size, load_features
from wwedit.framing.cropmodel import box_to_params, params_to_box
from wwedit.framing.croptrain import grouped_folds


def test_box_param_roundtrip_square():
    # 正規化16:9クロップ=正方。中心(0.5,0.52)・一辺0.48
    box = [0.26, 0.28, 0.74, 0.76]
    cx, cy, log_s = box_to_params(box)
    assert cx == 0.5 and cy == 0.52
    assert math.isclose(math.exp(log_s), 0.48, rel_tol=1e-6)
    back = params_to_box(cx, cy, log_s)
    assert all(math.isclose(a, b, abs_tol=1e-6) for a, b in zip(back, box, strict=True))


def test_params_to_box_clamps_to_unit():
    # 画面外へはみ出す中心・大サイズは [0,1] にクランプ
    box = params_to_box(0.9, 0.9, math.log(0.8))
    assert box[0] >= 0.0 and box[1] >= 0.0 and box[2] <= 1.0 and box[3] <= 1.0


def test_grouped_folds_no_recording_leak():
    # 同一収録(timeline)の項目は必ず同 fold（リーク防止）
    items = [{"timeline": f"rec{i % 6}"} for i in range(60)]
    fold = grouped_folds(items, k=5)
    rec_fold: dict[str, int] = {}
    for it, f in zip(items, fold, strict=True):
        r = it["timeline"]
        if r in rec_fold:
            assert rec_fold[r] == f, "同一収録が複数 fold に分かれた"
        rec_fold[r] = f
    assert set(fold) <= set(range(5))


def test_grouped_folds_deterministic():
    items = [{"timeline": f"rec{i % 7}"} for i in range(70)]
    assert grouped_folds(items, k=5) == grouped_folds(items, k=5)


def test_aug_window_identity_when_full():
    # f=1.0（fmin=fmax=1）なら原点0・box不変
    from wwedit.framing.croptrain_ft import _aug_window

    rng = np.random.default_rng(0)
    box = [0.3, 0.32, 0.7, 0.72]
    (ox, oy, f), nb = _aug_window(rng, box, fmin=1.0, fmax=1.0)
    assert (ox, oy, f) == (0.0, 0.0, 1.0)
    assert all(abs(a - b) < 1e-9 for a, b in zip(nb, box, strict=True))


def test_aug_window_zoom_enlarges_and_stays_in_unit():
    # f<1（寄り）で box は size/f 倍に拡大。常に[0,1]・中心は窓内に残る
    from wwedit.framing.croptrain_ft import _aug_window

    rng = np.random.default_rng(1)
    box = [0.4, 0.42, 0.6, 0.62]  # 一辺0.2, 中心(0.5,0.52)
    for _ in range(50):
        (ox, oy, f), nb = _aug_window(rng, box, fmin=0.7, fmax=0.95)
        assert all(0.0 <= v <= 1.0 for v in nb)
        # 寄りなので拡大方向（クランプ前提で size は元以上）
        assert (nb[2] - nb[0]) >= (box[2] - box[0]) - 1e-9


def test_grid_to_size_patch_multiple():
    assert grid_to_size((28, 50)) == (28 * 14, 50 * 14)


def test_load_features_stacks_in_id_order(tmp_path):
    # キャッシュ npy を id 順に積む（fp16保存→float32読込）
    np.save(tmp_path / "a.npy", np.full((4, 3), 1.0, dtype=np.float16))
    np.save(tmp_path / "b.npy", np.full((4, 3), 2.0, dtype=np.float16))
    out = load_features(tmp_path, ["b", "a"])
    assert out.shape == (2, 4, 3) and out.dtype == np.float32
    assert out[0].mean() == 2.0 and out[1].mean() == 1.0
