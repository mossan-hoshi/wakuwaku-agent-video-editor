"""全画面(no_crop)区間へ既定トリミングを与える。

**方針（ユーザー確定・2026-07-26）: 出力に「素の全画面」を出さない。**
crop モデルが bbox を付けられなかった区間（`bbox is None` ＝ 全画面扱い）は、そのままだと
ブラウザchrome/デスクトップ余白まで写る。そこで **上下左右を一律 1割インセット**した
既定枠（中央 80%×80%）を EDL に書き込み、全区間が必ず crop を持つ状態にする。

16:9 の縦横比は保たれる（幅も高さも同率で縮めるため）。EDL に**明示的に書き戻す**ので、
編集ツールの「調整」トラックに枠として現れ、G2 で人手修正でき、そのまま学習データにもなる。
"""

from __future__ import annotations

from wwedit.edl.schema import Edl

__all__ = ["DEFAULT_INSET", "inset_bbox", "apply_default_trim"]

DEFAULT_INSET = 0.1  # 上下左右それぞれ 1割


def inset_bbox(width: int, height: int, inset: float = DEFAULT_INSET) -> tuple[int, int, int, int]:
    """フレーム全体から上下左右 ``inset`` 割を落とした bbox (x, y, w, h) を返す。

    ``inset`` は片側の比率（0.1 なら左右で計2割落ちて幅は8割）。縦横同率なので 16:9 は保たれる。
    ``inset`` は [0, 0.45] にクランプ（潰れた枠を作らない）。
    """
    f = max(0.0, min(float(inset), 0.45))
    x = int(round(width * f))
    y = int(round(height * f))
    w = max(1, int(round(width * (1.0 - 2 * f))))
    h = max(1, int(round(height * (1.0 - 2 * f))))
    return (x, y, w, h)


def apply_default_trim(
    edl: Edl,
    *,
    inset: float = DEFAULT_INSET,
    kinds: tuple[str, ...] | None = None,
) -> int:
    """bbox 未設定の framing 区間へ既定トリム枠を書き込み、書き込んだ区間数を返す。

    既に bbox のある区間（モデル推論済み・人手修正済み）は**触らない**。
    ``kinds`` を渡すとその種別のみ対象（既定は loading 以外の全種別＝static/pending）。
    """
    w, h = edl.source.width, edl.source.height
    if not w or not h:
        return 0
    box = inset_bbox(w, h, inset)
    target = kinds if kinds is not None else ("static", "pending")
    n = 0
    for r in edl.framing:
        if r.kind not in target or r.bbox is not None:
            continue
        r.bbox = box
        n += 1
    return n
