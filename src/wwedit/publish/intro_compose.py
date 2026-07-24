"""[G] イントロ仕上げ合成（決定的CLI部品）。

DomoAI のリップシンク動画(720p)を **FullHD(1920×1080)** へ配置し、左上にロゴ＋キャラ名、
**ピンク二重枠字幕(台本全文・`subtitle/ass.py`)**、ジングル(-20dB)を重ね1本の完成イントロにする。
台本やジングル選曲（ランダム/季節）は呼び出し側（intro-builder スキル）の判断。ここは合成のみ。
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from wwedit.common.media import ffmpeg_path
from wwedit.edl.schema import Subtitle
from wwedit.subtitle.ass import build_ass

LOGO = Path(__file__).resolve().parents[3] / "assets" / "logo" / "nobetube_logo.png"
_MEIRYO = r"C:\Windows\Fonts\meiryob.ttc"


def _duration(path: str | Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
         "default=nw=1:nk=1", str(path)], capture_output=True, text=True)
    return float(r.stdout.strip() or 0.0)


# 助詞・読点（ここの**直後**で折ると自然＝語中で切らない）。「の」も最頻出なので含める。
_BREAK_AFTER = "をてにはがでともやへの、"
# 折った後の行がこれ未満になる位置では折らない（「…続報で / す。」のような孤立を防ぐ）。
_MIN_TAIL = 3


def _atomic_spans(s: str) -> list[tuple[int, int]]:
    """英数字の連続（ComfyUI / MCP / 2026 等）＝**分割してはいけない塊**の [start, end) 一覧。"""
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(s):
        if s[i].isascii() and s[i].isalnum():
            j = i
            while j < len(s) and s[j].isascii() and (s[j].isalnum() or s[j] in "-._"):
                j += 1
            spans.append((i, j))
            i = j
        else:
            i += 1
    return spans


def _breakable(s: str, i: int, spans: list[tuple[int, int]]) -> bool:
    """位置 i の**直後**で折ってよいか（英数字トークンの内部なら不可）。"""
    return not any(a <= i < b - 1 for a, b in spans)


def _split_long(s: str, max_line: int) -> list[str]:
    """長い文を、中央付近の助詞/読点の直後で折る（語中で切らない）。再帰で max_line 以下に。

    英数字トークン（ComfyUI 等）は割らず、折った残りが極端に短くなる位置も避ける。
    """
    if len(s) <= max_line:
        return [s]
    mid = len(s) // 2
    spans = _atomic_spans(s)

    def ok(i: int) -> bool:
        return _breakable(s, i, spans) and len(s) - (i + 1) >= _MIN_TAIL

    cand = [i for i, ch in enumerate(s[:-1]) if ch in _BREAK_AFTER and ok(i)]
    if not cand:  # 助詞が無ければ、トークンを割らない位置のうち中央に最も近い所で折る
        cand = [i for i in range(len(s) - 1) if ok(i)]
    pos = min(cand, key=lambda i: abs(i - mid)) if cand else len(s) - 1
    return _split_long(s[:pos + 1], max_line) + _split_long(s[pos + 1:], max_line)


def wrap_script(text: str, *, max_line: int = 16) -> str:
    """台本を**文(。！？)→助詞境界**で改行（語中で切らない）。ASS では \\N に変換。"""
    text = text.strip()
    sents, cur = [], ""
    for ch in text:
        cur += ch
        if ch in "。！？":
            sents.append(cur)
            cur = ""
    if cur:
        sents.append(cur)
    lines: list[str] = []
    for s in sents:
        lines += _split_long(s, max_line)
    return "\n".join(line for line in lines if line)


def _badge(name: str, logo_path: Path, out_png: Path, *, size: int = 132) -> Path:
    """ロゴ(**元色のまま**)＋本名フルネーム(縁取りのみ)の右上バッジ（**パネル無し**・透過PNG）。

    ロゴはカラフルなので再着色せずそのまま。名前は太い縁取りで背景に依らず視認。中身は中央寄せ。
    ffmpeg drawtext のパスescapeを避けるため PIL で作る。
    """
    from PIL import Image, ImageDraw, ImageFont

    pad, gap, name_h, font_px, ow = 6, 4, 56, 40, 3
    font = ImageFont.truetype(_MEIRYO, font_px)
    name_w = int(ImageDraw.Draw(Image.new("RGBA", (4, 4))).textlength(name, font=font))
    content_w = max(size, name_w)
    canvas = Image.new("RGBA", (content_w + pad * 2, size + gap + name_h + pad * 2), (0, 0, 0, 0))
    if logo_path.exists():
        logo = Image.open(logo_path).convert("RGBA").resize((size, size), Image.LANCZOS)
        canvas.paste(logo, (pad + (content_w - size) // 2, pad), logo)  # 元色のまま中央寄せ
    draw = ImageDraw.Draw(canvas)
    tx, ty = pad + (content_w - name_w) // 2, pad + size + gap
    for dx in range(-ow, ow + 1):
        for dy in range(-ow, ow + 1):
            if dx * dx + dy * dy <= ow * ow:
                draw.text((tx + dx, ty + dy), name, font=font, fill=(20, 20, 30, 255))
    draw.text((tx, ty), name, font=font, fill=(255, 255, 255, 255))
    out_png.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_png)
    return out_png


def script_to_subtitles(script: str, total_dur: float, *, lines_per_cue: int = 2):
    """台本を **lines_per_cue 行ずつのキュー**に分割し、文字数比で尺配分した Subtitle 列を返す。

    = 同時表示は2行まで・読む順に切り替わる（字幕の切替タイミングは2行テキスト基準）。
    """
    lines = wrap_script(script).split("\n")
    chunks = ["\n".join(lines[i:i + lines_per_cue]) for i in range(0, len(lines), lines_per_cue)]
    weights = [max(1, len(c.replace("\n", ""))) for c in chunks]
    tot = sum(weights) or 1
    subs, t = [], 0.0
    for i, (c, w) in enumerate(zip(chunks, weights, strict=True)):
        end = total_dur if i == len(chunks) - 1 else round(t + total_dur * w / tot, 3)
        subs.append(Subtitle(start=round(t, 3), end=end, text=c, style="intro"))
        t = end
    return subs


def compose_intro(
    intro_video: str | Path,
    script: str,
    out_path: str | Path,
    *,
    name: str = "文月 乃亜",
    jingle: str | Path | None = None,
    jingle_db: float = -20.0,
    logo_path: str | Path = LOGO,
    out_w: int = 1920,
    out_h: int = 1080,
) -> Path:
    """リップシンク動画＋台本＋ロゴ/名＋ジングル → FullHD 完成イントロ mp4。"""
    # ffmpeg は cwd=work（ass相対参照のため）で動くので入力は絶対パス化する。
    intro_video = Path(intro_video).resolve()
    jingle = Path(jingle).resolve() if jingle else None
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    dur = _duration(intro_video)
    work = Path(tempfile.mkdtemp())

    # ピンク二重枠字幕(intro)＝**2行ずつのキューに分割**して時間配分（同時表示は2行）。
    subs = script_to_subtitles(script, dur or 9.0)
    ass = work / "intro.ass"
    ass.write_text(build_ass(subs, color_map={}, play_w=out_w, play_h=out_h), encoding="utf-8")
    badge = _badge(name, Path(logo_path), work / "badge.png")

    cmd = [ffmpeg_path(), "-y", "-i", str(intro_video)]
    jingle_idx = None
    if jingle:
        cmd += ["-stream_loop", "-1", "-i", str(jingle)]
        jingle_idx = 1
    badge_idx = 2 if jingle else 1
    cmd += ["-i", str(badge)]

    vol = 10 ** (jingle_db / 20.0)
    fil = [
        f"[0:v]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
        f"crop={out_w}:{out_h}[bg]",
        f"[bg][{badge_idx}:v]overlay=W-w-28:24[vb]",  # 右上（W=本体幅, w=バッジ幅）
        f"[vb]ass={ass.name}[vout]",
    ]
    if jingle_idx is not None:
        fil.append(f"[{jingle_idx}:a]volume={vol:.4f},atrim=0:{dur}[jg]")
        fil.append("[0:a][jg]amix=inputs=2:duration=first:normalize=0[aout]")
        amap = "[aout]"
    else:
        amap = "[0:a]"
    cmd += ["-filter_complex", ";".join(fil), "-map", "[vout]", "-map", amap,
            "-t", f"{dur:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-pix_fmt", "yuv420p", str(out_path)]
    proc = subprocess.run(cmd, cwd=str(work), capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").splitlines()[-15:])
        raise RuntimeError(f"イントロ合成失敗:\n{tail}")
    return out_path
