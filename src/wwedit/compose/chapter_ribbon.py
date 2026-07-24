"""左上に張り付く2段リボン（収録日＋章名）の描画と、出力タイムライン上の章区間算出。

- リボン外形を先に濃紺でベタ塗り（下地）→ 青セルを重ねる方式で、セル間に背景が透ける
  隙間を作らない（明るい画面で継ぎ目に白画素が出る問題の対策）。3xスーパーサンプリングで
  縁/文字を滑らかにする。
- 章区間は ``_src_to_out`` でソース時刻→出力秒に変換し、各章 [out_start, 次章out_start] を
  埋める（最終章は total まで）。eyecatch/概要欄と同じ時刻系（非破壊・レンダ時のみ）。
"""
from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from wwedit.compose.ffmpeg_compose import _src_to_out
from wwedit.edl.schema import Edl, TimeRange

__all__ = [
    "render_ribbon_png", "chapter_ribbon_intervals", "format_rec_date",
    "resolve_speaker_schemes", "RIBBON_SCHEMES",
]

# ---- 見た目パラメータ（1920x1080基準・控えめサイズ）----
_H = 54            # リボン高さ
_CH = 16           # チェブロン(矢印)の水平深さ
_PADX = 16
_SS = 3            # スーパーサンプリング倍率

# 話者色分け: 字幕の話者色キー(blue/green/red/purple)に対応するリボン配色。
# 各スキーム = (暗セル, 明セル上, 明セル下)。blue は現行値（mossan-hoshi 用にそのまま）。
RIBBON_SCHEMES: dict[str, tuple] = {
    "blue":   ((10, 20, 45),  (58, 92, 156),  (32, 60, 112)),   # 寒色・現行（mossan-hoshi）
    "green":  ((8, 34, 20),   (46, 150, 86),  (24, 92, 52)),    # 寒色
    "red":    ((42, 12, 16),  (176, 74, 78),  (120, 38, 46)),   # 暖色
    "purple": ((30, 14, 42),  (140, 84, 168), (92, 48, 120)),   # 暖色寄り（Taniguchi）
}
_DEFAULT_SCHEME = RIBBON_SCHEMES["blue"]

_FONT_DIR = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"


def _yg(sz: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(_FONT_DIR / "YuGothM.ttc"), sz)


_MD = ImageDraw.Draw(Image.new("RGB", (10, 10)))


def _text_w(t: str, f: ImageFont.FreeTypeFont) -> int:
    b = _MD.textbbox((0, 0), t, font=f)
    return b[2] - b[0]


def _vgrad(w: int, h: int, top, bottom) -> Image.Image:
    g = Image.new("RGB", (1, h))
    for y in range(h):
        r = y / max(1, h - 1)
        g.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * r) for i in range(3)))
    return g.resize((w, h))


def _draw_ribbon(base: Image.Image, date_text: str, chap_text: str, scheme: tuple) -> None:
    navy, blue_t, blue_b = scheme
    fd_b, fc_b = _yg(23), _yg(25)
    chap_text = " " + chap_text   # > とタイトル左端の間隔を半角スペース1個分広げる
    dw = _PADX + _text_w(date_text, fd_b) + _PADX
    cw = _PADX + _text_w(chap_text, fc_b) + _PADX
    x1, x2 = dw, dw + cw
    tile_w, tile_h = x2 + _CH + 2, _H
    s = _SS
    W, Ht = tile_w * s, tile_h * s
    fd, fc = _yg(23 * s), _yg(25 * s)
    tile = Image.new("RGBA", (W, Ht), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    # ① 外形全体を暗セル色でベタ塗り（下地・隙間ゼロ）
    sil = [(0, 0), (x2 * s, 0), ((x2 + _CH) * s, Ht / 2), (x2 * s, Ht), (0, Ht)]
    d.polygon(sil, fill=tuple(navy) + (255,))
    # ② 明セルを暗セルの上に重ねる（左は「<」凹み＝暗セルの尖りを受ける／縁は必ず暗セルの上）
    blue_poly = [(x1 * s, 0), (x2 * s, 0), ((x2 + _CH) * s, Ht / 2), (x2 * s, Ht),
                 (x1 * s, Ht), ((x1 + _CH) * s, Ht / 2)]
    bmask = Image.new("L", (W, Ht), 0)
    ImageDraw.Draw(bmask).polygon(blue_poly, fill=255)
    grad = _vgrad(W, Ht, blue_t, blue_b).convert("RGBA")
    tile.paste(grad, (0, 0), bmask)
    # ③ テキスト（縦センタリング）
    d = ImageDraw.Draw(tile)
    bd = d.textbbox((0, 0), date_text, font=fd)
    ty_d = (Ht - (bd[3] - bd[1])) / 2 - bd[1]
    d.text((_PADX * s, ty_d), date_text, font=fd, fill=(235, 240, 248, 255))
    bc = d.textbbox((0, 0), chap_text, font=fc)
    ty_c = (Ht - (bc[3] - bc[1])) / 2 - bc[1]
    d.text(((x1 + _CH + 6) * s, ty_c), chap_text, font=fc, fill=(255, 255, 255, 255))
    # 縮小して基準解像度へ（滑らか）→ 左上(0,0)へ合成
    tile = tile.resize((tile_w, tile_h), Image.LANCZOS)
    base.alpha_composite(tile, (0, 0))


def render_ribbon_png(
    date_text: str, chap_text: str, out_path: str | Path,
    *, scheme: tuple | None = None, out_w: int = 1920, out_h: int = 1080,
) -> Path:
    """フルフレーム透過PNGの左上にリボンを描いて保存（overlay=0:0 で被せる用）。

    ``scheme``: (暗セル, 明セル上, 明セル下) の RGB三つ組。未指定は blue（現行）。
    """
    out_path = Path(out_path)
    base = Image.new("RGBA", (out_w, out_h), (0, 0, 0, 0))
    _draw_ribbon(base, date_text, chap_text, scheme or _DEFAULT_SCHEME)
    base.save(out_path)
    return out_path


def resolve_speaker_schemes(edl: Edl) -> dict[str, tuple]:
    """話者→リボン配色。字幕と同じ色キー（assign_speaker_colors＋EDL上書き）から導く。

    これで各話者のリボン色が**字幕の話者色と同系統**に揃う（mossan-hoshi=blue=現行 等）。
    """
    from wwedit.subtitle.ass import MAIN_PALETTE, assign_speaker_colors

    speakers = sorted({c.speaker for c in edl.chapters if c.speaker})
    if not speakers:
        return {}
    cmap = assign_speaker_colors(speakers, edl.recording_dir or "main")
    for sp, key in (edl.subtitle_speaker_colors or {}).items():
        if key in MAIN_PALETTE:
            cmap[sp] = MAIN_PALETTE[key]
    rev = {v: k for k, v in MAIN_PALETTE.items()}
    return {sp: RIBBON_SCHEMES.get(rev.get(cmap.get(sp, ""), "blue"), _DEFAULT_SCHEME)
            for sp in speakers}


def chapter_ribbon_intervals(
    edl: Edl, ranges: list[TimeRange] | None = None
) -> tuple[list[dict], float]:
    """各章を**出力タイムライン秒**で ``[out_start, out_end]`` に埋めて返す。

    返り値 ``(intervals, total)``。各要素 ``{"out_start","out_end","title","speaker"}``。
    最終章は total まで。**カットで尺ゼロに潰れた章（footageが残っていない章）は捨て、
    その出力位置で実際に流れている方の章を残す**（例: 冒頭がカットされた場合、先頭の
    intro章ではなく実際に映る次章がそこを担当）。先頭区間は 00:00 にスナップ。
    speaker が空の章は直前の章の speaker を引き継ぐ（色分けの連続性のため）。
    """
    rgs = ranges if ranges is not None else edl.kept_ranges()
    total = sum(r.duration for r in rgs)
    chs = sorted(edl.chapters, key=lambda c: c.start_at)
    # 各章の出力開始秒（0..total にクランプ）＋ speaker 前方補完
    pts: list[dict] = []
    last_sp = ""
    for c in chs:
        ot = min(max(_src_to_out(rgs, c.start_at), 0.0), total)
        sp = c.speaker or last_sp
        last_sp = sp
        pts.append({"out_start": ot, "title": c.chapter_title or f"チャプター{len(pts) + 1}",
                    "speaker": sp})
    # 連続する out_start から区間化。尺ゼロ（潰れた章）は捨て、後続の実映章を残す。
    out: list[dict] = []
    for j, b in enumerate(pts):
        oe = pts[j + 1]["out_start"] if j + 1 < len(pts) else total
        if oe - b["out_start"] <= 1e-3:
            continue
        out.append({"out_start": b["out_start"], "out_end": oe,
                    "title": b["title"], "speaker": b["speaker"]})
    if out and out[0]["out_start"] > 1e-6:
        out[0]["out_start"] = 0.0  # 先頭区間は 00:00 から
    return out, total


def format_rec_date(name_or_date: str) -> str:
    """文字列中の ``YYYY-MM-DD`` を拾って ``M/D収録`` を作る（無ければ「収録」）。

    ``name_or_date`` はフォルダ名 ``2026-07-16`` でも、日付を含むフルパス
    （``.../2026-07-16 18.00.21 [要録画🔴] ...``）でもよい（re.search で抽出）。
    """
    import re

    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", name_or_date or "")
    if not m:
        return "収録"
    return f"{int(m.group(2))}/{int(m.group(3))}収録"
