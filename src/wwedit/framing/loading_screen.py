"""[E] 画面切替(loading)区間に差し込むローディング画面クリップ生成。

SDD [E]: 白背景＋中央に「のべつべ!」ロゴ＋下に「○○中...」。末尾の `.` は
`'' → '.' → '..' → '...'` をループ。○○(label)は会話の流れから自動設定（呼び出し側で渡す）。

ロゴ: assets/logo/nobetube_logo.png（透過RGBA）。フォント: メイリオ。
PIL でフレーム列を描画 → ffmpeg で mp4。pure 関数(dot_for_frame/layout_boxes)はテスト可。
"""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
from pathlib import Path

from wwedit.common.media import ffmpeg_path

__all__ = [
    "DOT_STATES", "dot_for_frame", "layout_boxes", "build_loading_screen",
    "loading_loop_clip",
]

# ループ1周（ドット4状態×0.5秒）。これを -stream_loop で任意長へ伸ばす。
LOOP_CYCLE_S = len(["", ".", "..", "..."]) * 0.5
_LOOP_CACHE_DIR = Path(tempfile.gettempdir()) / "wwedit_loading"

DOT_STATES = ["", ".", "..", "..."]
_REPO = Path(__file__).resolve().parents[3]
DEFAULT_LOGO = _REPO / "assets" / "logo" / "nobetube_logo.png"
DEFAULT_FONT = "C:/Windows/Fonts/meiryo.ttc"


def dot_for_frame(frame_idx: int, fps: int, period_s: float = 0.5) -> str:
    """フレーム番号 → 末尾ドット文字列（period_s ごとに 1段進む・4状態ループ）。"""
    step = int(frame_idx / max(1.0, fps * period_s))
    return DOT_STATES[step % len(DOT_STATES)]


def layout_boxes(
    w: int, h: int, logo_w: int, logo_h: int, *, logo_frac: float = 0.30
) -> tuple[tuple[int, int, int, int], tuple[int, int]]:
    """ロゴ配置矩形 (x,y,lw,lh) とテキスト基準点 (cx, ty) を返す。

    ロゴは画面幅の logo_frac に収め、やや上寄り中央。テキストはロゴ下・水平中央
    （下端に被らないよう全体を上寄せ）。
    """
    lw = int(w * logo_frac)
    lh = int(lw * logo_h / logo_w) if logo_w else lw
    lx = (w - lw) // 2
    ly = int(h * 0.20)
    ty = ly + lh + int(h * 0.05)
    return (lx, ly, lw, lh), (w // 2, ty)


def _render_frames(
    label: str, n_frames: int, fps: int, w: int, h: int, logo_path, font_path, tmp: Path
) -> None:
    from PIL import Image, ImageDraw, ImageFont

    logo = Image.open(logo_path).convert("RGBA")
    (lx, ly, lw, lh), (cx, ty) = layout_boxes(w, h, logo.width, logo.height)
    logo_resized = logo.resize((lw, lh), Image.LANCZOS)
    try:
        font = ImageFont.truetype(font_path, size=int(h * 0.06))
    except OSError:
        font = ImageFont.load_default()

    base = Image.new("RGB", (w, h), (255, 255, 255))
    base.paste(logo_resized, (lx, ly), logo_resized)

    # ドット状態ごとに1枚だけ描画して使い回す（label中 + dots は最大幅で中央寄せ固定）
    cache: dict[str, Image.Image] = {}
    for dots in DOT_STATES:
        img = base.copy()
        d = ImageDraw.Draw(img)
        text = f"{label}中{dots}"
        # 末尾ドットで中心がブレないよう、最大文字列の幅で中央を固定
        full = f"{label}中{DOT_STATES[-1]}"
        fl, ft, fr, fb = d.textbbox((0, 0), full, font=font)
        tx = cx - (fr - fl) // 2
        d.text((tx, ty), text, fill=(40, 40, 40), font=font)
        cache[dots] = img

    for i in range(n_frames):
        cache[dot_for_frame(i, fps)].save(tmp / f"f{i:05d}.png")


def build_loading_screen(
    label: str,
    duration_s: float,
    out_path: str | Path,
    *,
    width: int = 1920,
    height: int = 1080,
    fps: int = 10,
    logo_path: str | Path = DEFAULT_LOGO,
    font_path: str = DEFAULT_FONT,
) -> Path:
    """『label中...』のローディング画面 mp4 を生成して out_path に書き出す。"""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n_frames = max(1, int(round(duration_s * fps)))
    tmp = Path(tempfile.mkdtemp())
    _render_frames(label, n_frames, fps, width, height, logo_path, font_path, tmp)
    cmd = [
        ffmpeg_path(), "-y", "-framerate", str(fps),
        "-i", str(tmp / "f%05d.png"),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(fps),
        str(out_path),
    ]
    subprocess.run(cmd, capture_output=True)
    return out_path


def loading_loop_clip(
    label: str,
    *,
    width: int = 1920,
    height: int = 1080,
    fps: int = 10,
    logo_path: str | Path = DEFAULT_LOGO,
    font_path: str = DEFAULT_FONT,
) -> Path:
    """ラベルごとに**1周ループのローディング動画を1本だけ生成してキャッシュ**する。

    ループ動画なので、各 loading 区間ではこの1本を ``-stream_loop`` で任意長へ伸ばして使う
    （区間ごと・レンダリングごとの作り直しを避ける）。同じ label/解像度/fps は再利用。
    """
    key = hashlib.sha1(f"{label}|{width}x{height}|{fps}".encode()).hexdigest()[:16]
    _LOOP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out = _LOOP_CACHE_DIR / f"loop_{key}.mp4"
    if not out.exists():
        build_loading_screen(
            label, LOOP_CYCLE_S, out,
            width=width, height=height, fps=fps,
            logo_path=logo_path, font_path=font_path,
        )
    return out
