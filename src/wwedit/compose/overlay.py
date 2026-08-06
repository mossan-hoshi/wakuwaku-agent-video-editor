"""ユーザー配置オーバーレイ（画像/テキスト）の合成ヘルパ。

編集ツールで置いた ``EDL.overlays`` を焼き込む（[[nondestructive-overlay-compose]]）。
EDL はソース時刻のまま保持し、ここで**出力タイムライン時刻へ変換**するだけ（非破壊）。

- **テキスト**: 字幕と同じ ASS 2レイヤー方式で**二重縁取り**を再現する
  （内側から「色の文字 → 白1次枠 → 同色の外枠」＝[[subtitle-double-border-spec]]）。
  位置は ``\\an7``（左上基準）＋ ``\\pos`` で指定するので、字幕の下端配置とは独立に置ける。
- **画像**: 透過PNG等をそのまま overlay フィルタで重ねる（拡大率・不透明度つき）。

純関数（``overlays_to_output`` / ``build_overlay_ass``）はファイルI/O無しでテストできる。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from wwedit.compose.ffmpeg_compose import _src_to_out, bbox_at
from wwedit.edl.schema import Edl, Overlay, TimeRange
from wwedit.subtitle.ass import (
    MAIN_PALETTE,
    SHADOW,
    WHITE_RING,
    ass_escape,
    ass_time,
)

__all__ = [
    "resolve_color",
    "overlays_to_output",
    "build_overlay_ass",
    "image_overlays",
    "mosaic_overlays",
    "mosaic_region_px",
    "mosaic_effect_filter",
    "build_mosaic_chains",
    "Placed",
    "output_crop_segments",
    "place_overlays",
]

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


def resolve_color(color: str) -> str:
    """文字色指定を ASS 色（``&HAABBGGRR``）へ解決する。

    パレットキー（red/purple/blue/green＝字幕と同じ4色）か ``#RRGGBB`` を受ける。
    未知の指定は青にフォールバックする（合成を止めない）。
    """
    c = (color or "").strip()
    if c in MAIN_PALETTE:
        return MAIN_PALETTE[c]
    m = _HEX_RE.match(c)
    if m:
        r, g, b = (int(m.group(1)[i : i + 2], 16) for i in (0, 2, 4))
        return f"&H00{b:02X}{g:02X}{r:02X}"  # ASSは BGR 順
    return MAIN_PALETTE["blue"]


def overlays_to_output(
    overlays: list[Overlay], ranges: list[TimeRange], freezes=()
) -> list[Overlay]:
    """オーバーレイ(ソース時刻)を出力タイムライン時刻へ変換する。

    カット区間へ完全に潰れたもの（開始と終了が同じ出力時刻になる）は除外する。
    """
    out: list[Overlay] = []
    for o in sorted(overlays, key=lambda v: (v.start, v.id)):
        os_, oe = _src_to_out(ranges, o.start, freezes), _src_to_out(ranges, o.end, freezes)
        if oe - os_ <= 1e-3:
            continue
        out.append(o.model_copy(update={"start": os_, "end": oe}))
    return out


# 揃え → ASS Alignment（上端基準。7=左上/8=中央上/9=右上）
_ALIGN_AN = {"left": 7, "center": 8, "right": 9}
# 行送り = (size*GLYPH_EM + 2*枠) * line_spacing。GLYPH_EM は枠ゼロ時の素の行高（em倍）。
# 枠を 2倍(上下)加算するので、line_spacing=1.0 で**枠を織り込んで隣接行が接する寸前**になる。
GLYPH_EM = 1.02


def line_advance(size: int, border: float, spacing: float) -> float:
    """1.0倍で枠込みでも被らない行送り(px)。border=総枠太さ、spacing=ユーザー倍率。"""
    return (size * GLYPH_EM + 2.0 * max(0.0, border)) * max(0.05, spacing)


def _style_line(
    name: str, fill: str, outline_col: str, outline: float, font: str, size: int,
    align: int = 7,
) -> str:
    """overlay 用 style 行。位置は各イベントの ``\\pos`` 指定なのでマージンは 0。"""
    return (
        f"Style: {name},{font},{size},{fill},{fill},{outline_col},{SHADOW},"
        f"-1,0,0,0,100,100,0,0,1,{outline:g},0,{align},0,0,0,1"
    )


def build_overlay_ass(
    placed: list[Placed],
    *,
    play_w: int = 1920,
    play_h: int = 1080,
    white_ring: float = 5.0,
    outer_outline: float = 9.0,
) -> str:
    """テキストオーバーレイ（配置済み）から ASS を生成する。

    ``double_border=True`` は字幕と同一の**2レイヤー二重枠**
    （L0=同色の太い外枠 / L1=白の1次枠＋色の文字）。False なら L1 のみ（白枠だけ）。
    **複数行は行ごとに別イベント**にして ``py + i*size*LINE_BASE*line_spacing`` へ ``\\pos`` する
    （ASS 既定の行送りだと二重枠が上下で被るため、行間を自前で制御する）。
    横揃えは ``align``（左/中央/右）を ``\\an`` へ、枠の太さは各 Overlay の値を使う。
    **サイズと枠は crop 拡大率 ``Placed.mag`` を掛ける**（ソース基準の見かけを保つ）。
    """
    texts = [p for p in placed if p.o.kind == "text" and (p.o.text or "").strip()]

    def widths(p: Placed) -> tuple[float, float]:
        o = p.o
        wr = o.white_ring if o.white_ring is not None else white_ring
        oo = o.outer_outline if o.outer_outline is not None else outer_outline
        return max(0.0, float(wr) * p.mag), max(0.0, float(oo) * p.mag)

    def font_px(p: Placed) -> int:
        return max(1, int(round(float(p.o.size) * p.mag)))

    def align_an(o: Overlay) -> int:
        return _ALIGN_AN.get(getattr(o, "align", "left") or "left", 7)
    head = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 2",
        f"PlayResX: {play_w}",
        f"PlayResY: {play_h}",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
    ]
    # 同じ(色,フォント,サイズ,二重枠,枠太さ,揃え)の組み合わせごとに style を1つ作る
    combos: list[tuple] = []
    for p in texts:
        wr, oo = widths(p)
        key = (resolve_color(p.o.color), p.o.font, font_px(p), bool(p.o.double_border),
               wr, oo, align_an(p.o))
        if key not in combos:
            combos.append(key)
    sid = {k: f"o{i}" for i, k in enumerate(combos)}
    for (col, font, size, dbl, wr, oo, an), s in sid.items():
        if dbl:
            head.append(_style_line(f"{s}L0", col, col, oo, font, size, an))
        head.append(_style_line(f"{s}L1", col, WHITE_RING, wr, font, size, an))
    head += [
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    events: list[str] = []
    for p in texts:
        o = p.o
        wr, oo = widths(p)
        an = align_an(o)
        s = sid[(resolve_color(o.color), o.font, font_px(p), bool(o.double_border),
                 wr, oo, an)]
        st, en = ass_time(p.start), ass_time(p.end)
        px, py = int(round(p.x)), int(round(p.y))
        border = wr + (oo if o.double_border else 0.0)
        adv = line_advance(font_px(p), border, getattr(o, "line_spacing", 1.0) or 1.0)
        lines = ass_escape(o.text).split("\\N")   # 行ごとに別イベント＝行間を自前制御
        layers = (0, 1) if o.double_border else (1,)
        for i, ln in enumerate(lines):
            body = f"{{\\pos({px},{int(round(py + i * adv))})}}{ln}"
            for layer in layers:
                events.append(f"Dialogue: {layer},{st},{en},{s}L{layer},,0,0,0,,{body}")
    return "\n".join(head + events) + "\n"


def image_overlays(placed: list[Placed]) -> list[Placed]:
    """画像オーバーレイのうち、パス指定があるものだけを返す（配置済み前提）。"""
    return [p for p in placed if p.o.kind == "image" and (p.o.path or "").strip()]


def mosaic_overlays(placed: list[Placed]) -> list[Placed]:
    """モザイクオーバーレイを返す（配置済み前提）。"""
    return [p for p in placed if p.o.kind == "mosaic"]


def mosaic_region_px(p: Placed, out_w: int, out_h: int) -> tuple[int, int, int, int]:
    """モザイク領域を出力ピクセル (x, y, w, h) にする（フレーム内へクランプ・最小2px）。

    ソース基準の矩形を crop 写像した後の値なので、**画面外にはみ出した分は詰める**
    （crop で被写体が画面端に寄ったときに領域が欠けないよう、位置を内側へ寄せる）。
    """
    rw = max(2, min(out_w, int(round(p.w))))
    rh = max(2, min(out_h, int(round(p.h))))
    rx = max(0, min(out_w - rw, int(round(p.x))))
    ry = max(0, min(out_h - rh, int(round(p.y))))
    return rx, ry, rw, rh


def mosaic_effect_filter(o: Overlay, rw: int, rh: int, mag: float = 1.0) -> str:
    """モザイク方式に対応する ffmpeg フィルタ断片（crop 済み ``rw×rh`` 領域に適用する）。

    ``strength`` は**ソース基準**の粗さなので、crop 拡大率 ``mag`` を掛けて出力側の
    粗さに直す（crop で寄っても見た目のブロック/ぼけの大きさが変わらない）。
    """
    if o.mosaic_type == "gaussian":
        return f"gblur=sigma={max(0.5, float(o.strength) * mag):g}"
    # pixelate（既定）: 近傍縮小→**元サイズへ**近傍拡大で低解像度ブロック化。
    # 拡大先は必ず領域実寸(rw×rh)を明示する（iw:ih だと縮小後サイズのままになる）。
    blk = max(2, int(round(float(o.strength) * mag)))
    dw, dh = max(1, rw // blk), max(1, rh // blk)
    return f"scale={dw}:{dh}:flags=neighbor,scale={rw}:{rh}:flags=neighbor"


def build_mosaic_chains(
    mosaics: list[Placed], prev_label: str, out_w: int, out_h: int,
    *, mask_input_of: dict[int, int] | None = None,
) -> tuple[list[str], str]:
    """モザイク群を焼き込む filtergraph 断片と最終ラベルを返す（配置済み前提）。

    各モザイクは ``split`` で映像を複製し、領域を crop→効果適用→元位置へ overlay する
    （時刻 enable で表示区間だけ）。``shape="ellipse"`` は ``mask_input_of[k]``（k=この
    リスト内の添字）に渡されたグレースケール楕円マスク入力を alphamerge して楕円だけを
    ぼかす。**添字で引く**のは、同じ重ねでも crop 区間ごとに領域サイズが変わるため。
    返り値 ``(chains, final_label)``。mosaics が空なら ``([], prev_label)``。
    """
    mask_input_of = mask_input_of or {}
    chains: list[str] = []
    prev = prev_label
    for k, p in enumerate(mosaics):
        o = p.o
        rx, ry, rw, rh = mosaic_region_px(p, out_w, out_h)
        s, e = float(p.start), float(p.end)
        base, crop, mz = f"mb{k}", f"mc{k}", f"mz{k}"
        nxt = f"mout{k}"
        chains.append(f"[{prev}]split[{base}][{crop}]")
        eff = mosaic_effect_filter(o, rw, rh, p.mag)
        if o.shape == "ellipse" and k in mask_input_of:
            mi = mask_input_of[k]
            chains.append(f"[{crop}]crop={rw}:{rh}:{rx}:{ry},{eff},format=rgba[{mz}]")
            chains.append(f"[{mi}:v]format=gray,scale={rw}:{rh}[mmk{k}]")
            chains.append(f"[{mz}][mmk{k}]alphamerge[{mz}a]")
            top = f"{mz}a"
        else:
            chains.append(f"[{crop}]crop={rw}:{rh}:{rx}:{ry},{eff}[{mz}]")
            top = mz
        chains.append(
            f"[{base}][{top}]overlay={rx}:{ry}:eof_action=pass:"
            f"enable='between(t,{s:.3f},{e:.3f})'[{nxt}]"
        )
        prev = nxt
    return chains, prev


def edl_overlays_for_output(edl: Edl, ranges: list[TimeRange]) -> list[Overlay]:
    """EDL のオーバーレイを出力タイムラインへ変換して返す（合成側の入口）。"""
    return overlays_to_output(edl.overlays or [], ranges, tuple(edl.freezes or ()))


# ── ソース基準の座標 → クロップ後の出力座標への写像 ──────────────────────────
# ``Overlay.x/y/w/h`` は**ソースフレーム基準の正規化値**（編集ツールはソース映像の上に
# 置くので、素材の同じ場所に貼り付く＝モザイクが被写体を追従する）。合成の出力は
# フレーミング bbox で crop→拡大されるので、ここで写像する。crop は
# ``build_filter_script_framed`` と**同じ規則**（keep区間ごとに中点の bbox）なので、
# 1つの重ねが複数区間にまたがると区間ごとに位置・倍率が変わる → 区間ごとに分割する。


@dataclass(frozen=True)
class Placed:
    """1つの重ねを、ある crop 区間の**出力ピクセル**へ落とし込んだもの。

    ``o`` は元の Overlay（色/文字/形状などの属性はそのまま使う）。``start``/``end`` は
    出力時刻で、その区間だけ ``enable`` する。``mag`` は crop 拡大率で、画像の拡大率・
    文字サイズ・枠の太さに掛ける（ソース基準の見かけの大きさを保つため）。
    """

    o: Overlay
    start: float
    end: float
    x: float
    y: float
    w: float
    h: float
    mag: float


def output_crop_segments(
    edl: Edl, ranges: list[TimeRange]
) -> list[tuple[float, float, tuple[int, int, int, int] | None]]:
    """出力タイムライン上の ``(start, end, bbox)``。隣接する同一 bbox は1つに畳む。

    bbox の決め方は framed concat と**同一の区間分割**を使う（:func:`framed_pieces`＝
    フリーズ位置とフレーミング境界で割り、各小片の中点の bbox）。ここが framed concat と
    ずれると、モザイクや重ねが crop と違う倍率・位置で置かれる。
    """
    from wwedit.compose.ffmpeg_compose import framed_pieces

    segs: list[tuple[float, float, tuple[int, int, int, int] | None]] = []
    t = 0.0
    # フリーズは直前フレームの静止＝同じ crop が続くので、piece 尺に extra を足すだけでよい
    for r, extra in framed_pieces(edl, ranges, tuple(edl.freezes or ())):
        d = max(0.0, r.end - r.start) + extra
        if d <= 0:
            continue
        bb = bbox_at(edl, (r.start + r.end) / 2)
        if segs and segs[-1][2] == bb:
            segs[-1] = (segs[-1][0], t + d, bb)   # 同じ crop が続くならまとめる
        else:
            segs.append((t, t + d, bb))
        t += d
    return segs


def place_overlays(
    overlays: list[Overlay],
    segments: list[tuple[float, float, tuple[int, int, int, int] | None]],
    *,
    src_w: int,
    src_h: int,
    out_w: int = 1920,
    out_h: int = 1080,
) -> list[Placed]:
    """出力時刻済みの重ねを、crop 区間ごとに出力ピクセルへ写像する。

    crop 無し（bbox None）の区間では ``mag = out_w/src_w``＝単純なスケールになる。
    完全に画面外へ出た配置は落とす（描いても見えないうえフィルタが増えるだけ）。
    """
    placed: list[Placed] = []
    for o in overlays:
        for s0, s1, bb in segments:
            s, e = max(float(o.start), s0), min(float(o.end), s1)
            if e - s <= 1e-3:
                continue
            bx, by, bw, bh = bb if bb else (0, 0, src_w, src_h)
            mx, my = out_w / max(1, bw), out_h / max(1, bh)
            x = (o.x * src_w - bx) * mx
            y = (o.y * src_h - by) * my
            w = (o.w or 0.0) * src_w * mx
            h = (o.h or 0.0) * src_h * my
            if x >= out_w or y >= out_h or x + max(w, 1) <= 0 or y + max(h, 1) <= 0:
                continue  # 画面外
            placed.append(Placed(o=o, start=s, end=e, x=x, y=y, w=w, h=h, mag=mx))
    return placed
