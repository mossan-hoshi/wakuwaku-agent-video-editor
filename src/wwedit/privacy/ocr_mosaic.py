"""画面OCRで見つかった秘匿語/NGワードに、自動でモザイク重ねを付ける。

**方針（ユーザー確定・2026-07-26）: カットせずモザイクで隠す。**
本編を切ると話の流れが飛ぶので、画面に秘匿語（`.env` の ``WWEDIT_MASK_TERMS`` /
``WWEDIT_CUT_NGWORDS``）が写った箇所は、**その周辺を大きめに覆うモザイク overlay** を
`EDL.overlays` へ足して隠す（非破壊・G2 の編集ツールで位置/サイズを手直しできる）。

- **OCR は自前で走らせない**。`ocr.screen_scan` の共有キャッシュ（framing 区間ごとの代表
  フレームをフル画面OCRした結果）を読むだけ＝章の固有名補正と**同じ1回の推論**を使い回す
  （[[cache-model-forward-not-resweep]]）。
- 座標は **ソースフレーム基準の正規化(0..1)**＝`Overlay` の確定仕様。合成側 `place_overlays`
  が framing crop に応じて出力へ写像するので、ここでは素材座標のまま置けばよい。
- OCR は**フル画面**に掛ける（crop で見えなくなる想定に頼らない＝G2 で crop を広げても
  隠し漏れが起きない安全側）。
- 語そのものは秘匿情報なので、ログ・返り値・EDL のどこにも出さない（件数だけ報告）。

重い OCR/フレーム抽出は注入可能にしてテスト分離（[[cache-model-forward-not-resweep]]）。
"""

from __future__ import annotations

from pathlib import Path

from wwedit.edl.schema import Edl, Overlay
from wwedit.ocr.screen_scan import FrameOcr

__all__ = [
    "DEFAULT_MARGIN",
    "DEFAULT_MIN_FRAC",
    "DEFAULT_MAX_GAP",
    "DEFAULT_PAD",
    "load_screen_terms",
    "expand_box",
    "union_box",
    "group_hits",
    "hits_to_overlays",
    "mosaics_from_frames",
    "scan_ng_mosaics",
]

DEFAULT_MARGIN = 0.8     # 検出boxの寸法に対する四方の余裕（0.8＝かなり大きめ）
DEFAULT_MIN_FRAC = 0.06  # 最小サイズ（フレーム比）。細い文字列でも十分な面積で隠す
DEFAULT_MAX_GAP = 45.0   # 同じ場所の再出現をひと続きとみなす時間差（区間1枚サンプル前提）
DEFAULT_PAD = 3.0        # 前後の余白（サンプル時刻の前後も隠す）

PixelBox = tuple[int, int, int, int]  # (x0, y0, x1, y1)


def load_screen_terms(env_file: str = ".env") -> list[str]:
    """画面から隠すべき語を取得（``WWEDIT_MASK_TERMS`` ∪ ``WWEDIT_CUT_NGWORDS``）。

    マスク語は元々「画面内OCRで見つかったらぼかす語」、NGワードは発話カット用だが
    **画面に出ていても隠したい**ので同じ扱いにする。未設定なら空＝何もしない（安全側）。
    """
    from wwedit.cut.ngwords import NGWORDS_ENV
    from wwedit.privacy.masking import load_mask_terms

    terms = list(load_mask_terms(env_file=env_file))
    for t in load_mask_terms(env_var=NGWORDS_ENV, env_file=env_file):
        if t not in terms:
            terms.append(t)
    return terms


def expand_box(
    box: PixelBox,
    width: int,
    height: int,
    *,
    margin: float = DEFAULT_MARGIN,
    min_frac: float = DEFAULT_MIN_FRAC,
) -> PixelBox:
    """検出boxを四方へ広げる（ざっくり大きめに隠す）。フレーム内へクランプ。

    ``margin`` は box の幅/高さに対する比率で四方に足す。さらに ``min_frac``（フレーム比）を
    下回る辺は中心を保ったまま最小サイズまで広げる（1行の細い文字列でも面積を確保）。
    """
    x0, y0, x1, y1 = (float(v) for v in box)
    bw, bh = max(1.0, x1 - x0), max(1.0, y1 - y0)
    x0 -= bw * margin
    x1 += bw * margin
    y0 -= bh * margin
    y1 += bh * margin
    # 最小サイズ（中心固定で広げる）
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    min_w, min_h = width * min_frac, height * min_frac
    if x1 - x0 < min_w:
        x0, x1 = cx - min_w / 2, cx + min_w / 2
    if y1 - y0 < min_h:
        y0, y1 = cy - min_h / 2, cy + min_h / 2
    # フレーム内へクランプ（欠けさせない＝隠し漏れを作らない側に倒す）
    x0 = max(0.0, min(x0, width - 1.0))
    y0 = max(0.0, min(y0, height - 1.0))
    x1 = max(x0 + 1.0, min(x1, float(width)))
    y1 = max(y0 + 1.0, min(y1, float(height)))
    return (int(x0), int(y0), int(x1), int(y1))


def union_box(boxes: list[PixelBox]) -> PixelBox:
    """複数boxを包む最小の矩形。"""
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[2] for b in boxes)
    y1 = max(b[3] for b in boxes)
    return (x0, y0, x1, y1)


def _overlaps(a: PixelBox, b: PixelBox) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def span_for_time(
    t: float, spans: list[tuple[float, float]], *, pad: float
) -> tuple[float, float]:
    """時刻 t を含む区間（framing区間）を返す。無ければ ``t±pad``。

    区間内は同じ画面が写り続けるので、代表フレームで1回当たったら**その区間まるごと**隠す
    （サンプル時刻の前後だけだと隠し漏れる）。
    """
    for s, e in spans:
        if s <= t < e:
            return (s, e)
    return (t - pad, t + pad)


def group_spans(
    items: list[tuple[float, float, PixelBox]], *, max_gap: float
) -> list[tuple[float, float, PixelBox]]:
    """時間的に近く空間的に重なる (start, end, box) をまとめる（連続スライドを1本に）。"""
    groups: list[tuple[float, float, PixelBox]] = []
    for s, e, box in sorted(items, key=lambda it: it[0]):
        for i, (gs, ge, gbox) in enumerate(groups):
            if s - ge <= max_gap and _overlaps(box, gbox):
                groups[i] = (gs, max(ge, e), union_box([gbox, box]))
                break
        else:
            groups.append((s, e, box))
    return groups


def group_hits(
    hits: list[tuple[float, PixelBox]], *, max_gap: float
) -> list[tuple[float, float, PixelBox]]:
    """時刻順のヒットを、時間的に近く空間的に重なるものへまとめる。

    同じ語が同じ場所に出続けるスライドを1本の overlay に畳む。返り値は
    ``(first_time, last_time, union_box)``。時間の余白(pad)は呼び出し側で付ける。
    """
    groups: list[tuple[float, float, PixelBox]] = []
    for t, box in sorted(hits, key=lambda h: h[0]):
        for i, (gs, ge, gbox) in enumerate(groups):
            if t - ge <= max_gap and _overlaps(box, gbox):
                groups[i] = (gs, t, union_box([gbox, box]))
                break
        else:
            groups.append((t, t, box))
    return groups


def hits_to_overlays(
    groups: list[tuple[float, float, PixelBox]],
    width: int,
    height: int,
    *,
    pad: float,
    duration_s: float = 0.0,
    strength: float = 28.0,
    id_prefix: str = "ngmask",
) -> list[Overlay]:
    """グループ化済みヒットを、モザイク overlay（ソース基準の正規化座標）へ変換する。"""
    out: list[Overlay] = []
    for i, (gs, ge, box) in enumerate(groups):
        start = max(0.0, gs - pad)
        end = ge + pad
        if duration_s:
            end = min(end, duration_s)
        x0, y0, x1, y1 = box
        out.append(
            Overlay(
                id=f"{id_prefix}{i}",
                kind="mosaic",
                start=start,
                end=end,
                x=x0 / width,
                y=y0 / height,
                w=(x1 - x0) / width,
                h=(y1 - y0) / height,
                mosaic_type="pixelate",
                shape="rect",
                strength=strength,
            )
        )
    return out


def mosaics_from_frames(
    frames: list[FrameOcr],
    width: int,
    height: int,
    *,
    terms: list[str] | None = None,
    margin: float = DEFAULT_MARGIN,
    min_frac: float = DEFAULT_MIN_FRAC,
    strength: float = 28.0,
    max_gap: float = DEFAULT_MAX_GAP,
    pad: float = DEFAULT_PAD,
    duration_s: float = 0.0,
    spans: list[tuple[float, float]] | None = None,
) -> list[Overlay]:
    """OCR済みフレーム群（共有キャッシュ）から、秘匿語/NG語を覆うモザイク overlay を作る。

    **ここに推論は無い**（マッチングと矩形計算だけ）。返り値に語は含まない。
    ``spans``（framing区間）を渡すと、ヒットした代表フレームの**区間まるごと**を覆う。
    """
    words = [w for w in (terms if terms is not None else load_screen_terms()) if w.strip()]
    if not words or not frames:
        return []

    from wwedit.privacy.masking import find_mask_regions

    items: list[tuple[float, float, PixelBox]] = []
    for f in frames:
        for b in find_mask_regions(f.boxes, words):
            box = expand_box(b, width, height, margin=margin, min_frac=min_frac)
            s, e = span_for_time(f.time_s, spans or [], pad=pad)
            items.append((s, e, box))

    groups = group_spans(items, max_gap=max_gap)
    return hits_to_overlays(
        groups, width, height, pad=0.0, duration_s=duration_s, strength=strength
    )


def scan_ng_mosaics(
    edl: Edl,
    video_path: str | Path | None = None,
    *,
    cache_path: str | Path | None = None,
    refresh: bool = False,
    terms: list[str] | None = None,
    margin: float = DEFAULT_MARGIN,
    min_frac: float = DEFAULT_MIN_FRAC,
    strength: float = 28.0,
    max_gap: float = DEFAULT_MAX_GAP,
    pad: float = DEFAULT_PAD,
    **scan_kwargs,
) -> list[Overlay]:
    """共有OCRキャッシュ（無ければ1回だけ生成）から、モザイク overlay を返す。

    語が未設定なら**OCRにも行かない**（走査コストを一切払わない）。
    """
    words = [w for w in (terms if terms is not None else load_screen_terms()) if w.strip()]
    if not words:
        return []

    from wwedit.ocr.screen_scan import CACHE_NAME, ensure_screen_ocr

    cache = cache_path or (Path(edl.recording_dir) / CACHE_NAME)
    frames = ensure_screen_ocr(
        edl, cache, refresh=refresh, video_path=video_path, **scan_kwargs
    )
    return mosaics_from_frames(
        frames,
        edl.source.width,
        edl.source.height,
        terms=words,
        margin=margin,
        min_frac=min_frac,
        strength=strength,
        max_gap=max_gap,
        pad=pad,
        duration_s=edl.source.duration_s,
        spans=[(r.start, r.end) for r in edl.framing],
    )
