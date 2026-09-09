"""[E] 感情が切り替わった瞬間に、ちびキャラの脇へ出す**漫符**（マンガ記号）。

感情ごとに全キャラ共通。生成AIは使わず PIL で描く — 単色の記号は生成すると線がボケたり
色が濁ったりして、かえって安っぽくなるため。

「ダサくならない」を仕様として固定する:

- **単色＋白フチだけ**。グラデーション禁止・ドロップシャドウ禁止（キャラの縁が既に立って
  いるので影を足すと濁る）。
- 主役の記号は1つに絞り、残りは小さく散らす。
- **完全な左右対称を避ける**（各要素に傾きと位置のばらつきを固定値で入れる）。
- キャラ高さの半分程度と小さく、尺は 0.75 秒。余韻を残さない。
- **出はポップ、引きはフェード**（フェードインさせると鈍い）。

描画は4倍解像度で行ってから LANCZOS 縮小する。``ImageDraw`` の ``ellipse``/``line`` は
アンチエイリアスしないので、細い線が階段状になるとそれだけで安っぽく見える。

フレームは決定的に再生成できるので、リポジトリには置かず temp にキャッシュする
（``loading_loop_clip`` と同じ流儀）。デザインを変えたら ``FX_VERSION`` を上げる。
"""

from __future__ import annotations

import hashlib
import math
import tempfile
from pathlib import Path

__all__ = [
    "FX_VERSION", "FX_EMOTIONS", "FX_FPS", "FX_FRAMES", "FX_DURATION_S",
    "FX_MIN_GAP_S", "fx_frames", "fx_idle_frame", "fx_cache_dir", "envelope",
]

#: デザインを変えたらここを上げる（キャッシュのキーに入る）。
FX_VERSION = 4

#: normal は出さない（平常時に記号が出続けると鬱陶しい）。
FX_EMOTIONS = ("smile", "surprised", "troubled", "angry", "thinking")

FX_FPS = 24
FX_FRAMES = 18                     # 18 / 24fps = 0.75 秒
FX_DURATION_S = FX_FRAMES / FX_FPS

#: これより短い間隔で感情が切り替わっても、エフェクトは1回にまとめる。
FX_MIN_GAP_S = 0.8

#: 描画時の超解像倍率（アンチエイリアス目的）。
_SS = 4

#: 記号の塗り色。映像の明暗どちらでも読めるよう、字幕と同じ**二重枠**にする
#: （記号色 → 白の1次フチ → 同系の濃い外フチ）。単色1枚＋白フチだけだと、暗い映像の上で
#: 濃い記号が沈んで白フチしか見えなくなる（実測で surprised が読めなかった）。
_COLOR = {
    "smile": (255, 199, 46),
    "surprised": (255, 92, 92),
    "troubled": (108, 190, 224),
    "angry": (230, 57, 57),
    "thinking": (236, 238, 240),
}

#: 外フチ（同系色の濃い方）。
_EDGE = {
    "smile": (140, 96, 0),
    "surprised": (120, 20, 20),
    "troubled": (28, 84, 120),
    "angry": (110, 12, 12),
    "thinking": (90, 96, 104),
}


def envelope(i: int) -> tuple[float, float, float]:
    """フレーム ``i`` の ``(scale, alpha, dy)``。``dy`` は上方向のドリフト（比率）。

    f0-3   ポップイン（0 → 1.25・アルファ 0 → 1）
    f4-5   1.25 → 1.0 へ落ち着く
    f6-12  ホールド
    f13-17 フェードアウトしながら上へ流れる
    """
    if i <= 3:
        t = (i + 1) / 4.0
        return 1.25 * _ease_out(t), min(1.0, t * 1.6), 0.0
    if i <= 5:
        t = (i - 3) / 2.0
        return 1.25 - 0.25 * t, 1.0, 0.0
    if i <= 12:
        return 1.0, 1.0, 0.0
    t = (i - 12) / (FX_FRAMES - 1 - 12)
    return 1.0 + 0.05 * t, max(0.0, 1.0 - t), 0.08 * t


def _ease_out(t: float) -> float:
    return 1.0 - (1.0 - t) ** 3


def fx_cache_dir(emotion: str, size_px: int) -> Path:
    key = hashlib.sha1(
        f"{emotion}|{size_px}|{FX_VERSION}".encode()).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / "wwedit_chibi_fx" / f"{emotion}-{size_px}-{key}"


def fx_frames(emotion: str, size_px: int) -> list[Path]:
    """感情エフェクトの PNG シーケンスを返す（キャッシュ済みならそのまま）。

    全フレームは**同一サイズ**でなければならない（concat demuxer の制約）。
    """
    if emotion not in FX_EMOTIONS:
        raise ValueError(f"エフェクトを持たない感情: {emotion}")
    d = fx_cache_dir(emotion, size_px)
    paths = [d / f"f{i:02d}.png" for i in range(FX_FRAMES)]
    if all(p.exists() for p in paths):
        return paths

    from PIL import Image

    d.mkdir(parents=True, exist_ok=True)
    big = _render_symbol(emotion, size_px * _SS)
    for i, p in enumerate(paths):
        s, a, dy = envelope(i)
        frame = Image.new("RGBA", (size_px, size_px), (0, 0, 0, 0))
        if a > 0.004 and s > 0.02:
            w = max(2, int(round(size_px * s)))
            sym = big.resize((w, w), Image.LANCZOS)
            if a < 1.0:
                alpha = sym.split()[3].point(lambda v, a=a: int(v * a))
                sym.putalpha(alpha)
            off = (size_px - w) // 2
            frame.alpha_composite(sym, (off, off - int(size_px * dy)))
        frame.save(p)
    return paths


def fx_idle_frame(size_px: int) -> Path:
    """完全に透明なフレーム（エフェクトが出ていない区間の待機用）。"""
    from PIL import Image

    d = fx_cache_dir("_idle", size_px)
    p = d / "idle.png"
    if not p.exists():
        d.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", (size_px, size_px), (0, 0, 0, 0)).save(p)
    return p


# ── 記号の描画（すべて4倍解像度で描かれる）──────────────────

def _render_symbol(emotion: str, n: int):
    """記号を ``n×n`` の透明キャンバスへ描く（ポップ時 1.25 倍でも収まるよう 80% 内）。"""
    from PIL import Image, ImageFilter

    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    fill = _COLOR[emotion]
    {
        "smile": _draw_smile,
        "surprised": _draw_surprised,
        "troubled": _draw_troubled,
        "angry": _draw_angry,
        "thinking": _draw_thinking,
    }[emotion](img, n, fill)

    # 二重枠（字幕と同じ考え方）: 外側に同系の濃い色、その内側に白、いちばん上に記号。
    # これで明るい映像でも暗い映像でも輪郭が残る。
    e = max(1, n // 110)
    a = img.split()[3]
    halo_w = a.filter(ImageFilter.MaxFilter(e * 2 + 1))
    halo_d = a.filter(ImageFilter.MaxFilter(e * 4 + 1))
    out = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    out.paste(Image.new("RGBA", (n, n), _EDGE[emotion] + (255,)), (0, 0), halo_d)
    out.paste(Image.new("RGBA", (n, n), (255, 255, 255, 255)), (0, 0), halo_w)
    out.alpha_composite(img)
    return out


def _poly_star(cx: float, cy: float, r: float, *, points: int = 4,
               inner: float = 0.3, rot: float = 0.0):
    pts = []
    for i in range(points * 2):
        rad = r if i % 2 == 0 else r * inner
        a = math.radians(rot + i * (360.0 / (points * 2)))
        pts.append((cx + rad * math.cos(a), cy + rad * math.sin(a)))
    return pts


def _draw_smile(img, n: int, fill):
    """きらめき（4点星を3粒・大きさと角度をずらす）。"""
    from PIL import ImageDraw

    d = ImageDraw.Draw(img)
    for cx, cy, r, rot in ((0.44, 0.40, 0.30, -8), (0.70, 0.26, 0.16, 12),
                           (0.28, 0.66, 0.12, 5)):
        d.polygon(_poly_star(cx * n, cy * n, r * n, points=4, inner=0.26, rot=rot),
                  fill=fill + (255,))


def _draw_surprised(img, n: int, fill):
    """ビックリマーク1本＋放射線（線は8本・長さをばらす）。"""
    from PIL import ImageDraw

    d = ImageDraw.Draw(img)
    cx, top, bot = 0.5 * n, 0.26 * n, 0.62 * n
    wt, wb = 0.085 * n, 0.045 * n          # 上太く下細い
    d.polygon([(cx - wt / 2, top), (cx + wt / 2, top),
               (cx + wb / 2, bot), (cx - wb / 2, bot)], fill=fill + (255,))
    r = 0.037 * n
    d.ellipse((cx - r, 0.70 * n - r, cx + r, 0.70 * n + r), fill=fill + (255,))
    lens = (0.09, 0.06, 0.08, 0.05, 0.09, 0.06, 0.075, 0.055)
    for i, ln in enumerate(lens):
        a = math.radians(-90 + (i - 3.5) * 26)
        x0, y0 = cx + 0.20 * n * math.cos(a), 0.46 * n + 0.20 * n * math.sin(a)
        x1, y1 = (cx + (0.20 + ln) * n * math.cos(a),
                  0.46 * n + (0.20 + ln) * n * math.sin(a))
        d.line((x0, y0, x1, y1), fill=fill + (255,), width=max(2, int(n * 0.016)))


def _draw_troubled(img, n: int, fill):
    """汗の雫1粒＋げんなり縦線2本。"""
    from PIL import ImageDraw

    d = ImageDraw.Draw(img)
    # 雫を大小2粒。縦線（げんなり線）は「II」にしか見えず意味が伝わらなかったので出さない。
    for cx, cy, r in ((0.44, 0.56, 0.155), (0.72, 0.33, 0.085)):
        cx, cy, r = cx * n, cy * n, r * n
        d.ellipse((cx - r, cy - r * 0.92, cx + r, cy + r), fill=fill + (255,))
        d.polygon([(cx - r * 0.40, cy - r * 0.60), (cx + r * 0.40, cy - r * 0.60),
                   (cx + r * 0.05, cy - r * 2.0)], fill=fill + (255,))


def _draw_angry(img, n: int, fill):
    """怒りマーク（井桁）を大1・小2。角度をわずかにずらす。"""
    from PIL import ImageDraw

    d = ImageDraw.Draw(img)
    # ⚠️ 回転させない。4倍解像度で描いても ``rotate`` はアルファの輪郭を階段状にするので、
    # 直線の記号だけ目に見えてギザつく（実測）。非対称は3つの配置と大きさだけで出す。
    for cx, cy, size in ((0.46, 0.45, 0.36, ), (0.77, 0.23, 0.17), (0.25, 0.73, 0.13)):
        m = size * n
        w = max(2, int(m * 0.15))   # 太いと線同士がくっついて「田」に見える
        for off in (-0.21, 0.21):
            x = cx * n + off * m
            y = cy * n + off * m
            d.line((x, cy * n - m * 0.5, x, cy * n + m * 0.5), fill=fill + (255,), width=w)
            d.line((cx * n - m * 0.5, y, cx * n + m * 0.5, y), fill=fill + (255,), width=w)


def _draw_thinking(img, n: int, fill):
    """思考の丸3つ（右上へ大きくなる）。塗りは薄く、枠だけ色を残す。"""
    from PIL import ImageDraw

    d = ImageDraw.Draw(img)
    w = max(2, int(n * 0.022))
    for cx, cy, r in ((0.32, 0.71, 0.060), (0.49, 0.55, 0.095), (0.70, 0.35, 0.145)):
        d.ellipse((cx * n - r * n, cy * n - r * n, cx * n + r * n, cy * n + r * n),
                  fill=fill + (255,), outline=(120, 126, 134, 255), width=w)
