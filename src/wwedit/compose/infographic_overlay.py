"""[I] 本編冒頭の要約インフォグラフィックを**画面固定レイヤー**として重ねる。

置き場所は「上部UI（チャプターリボン）・ちびキャラ・字幕に**重ならない**安全枠」で、
そこへ横長画像を contain（縦横比を保って内接）で収めて中央に置く。ちびキャラと同じく
**フレーミング crop の影響を受けない画面座標**なので、`EDL.overlays`（ソースフレーム基準の
ユーザー重ね）とは別系統にしてある。

レイヤー順は モザイクより上／ちびキャラより下。図解が万一はみ出しても、ちびキャラ・字幕・
リボンが上に描かれて隠れない（サイズ計算のずれに対する保険）。

時刻は**本編の出力タイムライン**（イントロは別ファイルとして前に連結されるので無関係）。
"""

from __future__ import annotations

from pathlib import Path

from wwedit.edl.schema import Edl, InfographicConfig

__all__ = ["safe_box", "fit_contain", "infographic_placement", "build_infographic_chain"]


def safe_box(
    cfg: InfographicConfig, out_w: int = 1920, out_h: int = 1080,
) -> tuple[int, int, int, int]:
    """図解を置いてよい矩形 ``(x, y, w, h)`` を返す（出力ピクセル）。

    予約値は 1080p 基準なので、他解像度では高さ比で素直にスケールする。
    """
    k = out_h / 1080.0
    top = int(round(cfg.top_reserve_px * k))
    bottom = int(round(cfg.bottom_reserve_px * k))
    side = int(round(cfg.side_margin_px * k))
    w = max(16, out_w - side * 2)
    h = max(16, out_h - top - bottom)
    return side, top, w, h


def fit_contain(
    img_w: int, img_h: int, box: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    """``box`` に内接する最大サイズへ縮小し、中央に置いた ``(x, y, w, h)`` を返す。

    拡大はしない（生成画像は 2K なので通常は縮小）。偶数へ丸める（yuv420 対策）。
    """
    bx, by, bw, bh = box
    if img_w <= 0 or img_h <= 0:
        raise ValueError(f"画像サイズが不正: {img_w}x{img_h}")
    scale = min(bw / img_w, bh / img_h, 1.0)
    w = max(2, int(img_w * scale) // 2 * 2)
    h = max(2, int(img_h * scale) // 2 * 2)
    return bx + (bw - w) // 2, by + (bh - h) // 2, w, h


def infographic_placement(
    edl: Edl, *, out_w: int = 1920, out_h: int = 1080,
) -> tuple[int, int, int, int] | None:
    """EDL の設定から実際の配置 ``(x, y, w, h)`` を求める（無効/画像なしは None）。"""
    cfg = edl.infographic
    if not cfg or not cfg.enabled or not cfg.path:
        return None
    path = Path(cfg.path)
    if not path.exists():
        raise FileNotFoundError(f"インフォグラフィック画像が無い: {path}")
    from PIL import Image

    with Image.open(path) as im:
        iw, ih = im.size
    return fit_contain(iw, ih, safe_box(cfg, out_w, out_h))


def build_infographic_chain(
    cfg: InfographicConfig, placement: tuple[int, int, int, int],
    input_idx: int, prev_label: str, out_label: str,
) -> list[str]:
    """filtergraph の chain 断片を返す（``-loop 1 -i <png>`` を入力に足した後で呼ぶ）。

    表示区間は ``[start_s, start_s+duration_s)``。``fade_s`` があればアルファをフェードさせる
    （``format=rgba`` 済みの入力に ``fade=alpha=1``）。
    """
    x, y, w, h = placement
    st = max(0.0, float(cfg.start_s))
    dur = max(0.01, float(cfg.duration_s))
    fade = max(0.0, min(float(cfg.fade_s), dur / 2))
    chain = f"[{input_idx}:v]scale={w}:{h},format=rgba"
    if fade > 0:
        # `-loop 1` の静止画入力も pts は 0 始まりで実時間と同じ進み方をするので、
        # フェード時刻は**出力タイムラインの絶対秒**でそのまま書ける。
        chain += (f",fade=t=in:st={st:g}:d={fade:g}:alpha=1"
                  f",fade=t=out:st={st + dur - fade:g}:d={fade:g}:alpha=1")
    chain += "[igi]"
    return [
        chain,
        f"[{prev_label}][igi]overlay={x}:{y}:eof_action=pass:"
        f"enable='between(t,{st:.3f},{st + dur:.3f})'[{out_label}]",
    ]
