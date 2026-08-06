"""イントロを本編の先頭に連結する（[G]→完成尺）。

**イントロと本編は規格が違う**（イントロ=リップシンク由来の 30fps/44.1kHz mono、
本編=収録由来の 25fps/48kHz stereo）。concat demuxer で ``-c copy`` する前に
**イントロ側を本編と同じ規格へ再エンコード**しないと繋がらない（STATUS §11.8）。

**章時刻はイントロ尺ぶん後ろへずれる**。ここを忘れると概要欄の章が全部ズレる
（#100 は7秒早いまま投稿した）。``shift_chapter_lines`` で必ずずらす。
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from wwedit.common.media import ffmpeg_error
from wwedit.compose.ffmpeg_compose import ffmpeg_path

__all__ = ["probe_av", "shift_chapter_lines", "prepend_intro"]

_LINE_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?\s+(.*)$")


def _ffprobe(args: list[str]) -> str:
    exe = str(Path(ffmpeg_path()).with_name("ffprobe"))
    proc = subprocess.run([exe, *args], capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe 失敗: {(proc.stderr or '').strip()[-300:]}")
    return (proc.stdout or "").strip()


def probe_av(path: str | Path) -> dict:
    """動画の規格（w/h/fps/音声sr/ch/尺）を返す。"""
    out = _ffprobe([
        "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate",
        "-of", "default=nw=1:nk=1", str(path),
    ]).splitlines()
    w, h, rate = int(out[0]), int(out[1]), out[2]
    num, _, den = rate.partition("/")
    fps = float(num) / float(den or 1)
    a = _ffprobe([
        "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=sample_rate,channels",
        "-of", "default=nw=1:nk=1", str(path),
    ]).splitlines()
    dur = float(_ffprobe([
        "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(path),
    ]) or 0.0)
    return {
        "width": w, "height": h, "fps": fps,
        "sample_rate": int(a[0]) if a else 48000,
        "channels": int(a[1]) if len(a) > 1 else 2,
        "duration": dur,
    }


def shift_chapter_lines(lines: list[str], offset_s: float) -> list[str]:
    """``MM:SS ラベル`` 行の時刻を ``offset_s`` 秒だけ後ろへずらす。

    **切り捨て**でずらす（マーカーが章頭のわずか手前＝アイキャッチのタイトルカードが見える）。
    先頭行は ``00:00`` に固定する（YouTube は先頭章が 00:00 でないと章を1つも出さない）。
    """
    out: list[str] = []
    off = int(offset_s)  # 切り捨て
    for i, line in enumerate(lines):
        m = _LINE_RE.match(line.strip())
        if not m:
            out.append(line)
            continue
        h, mm, ss, label = m.groups()
        total = (int(h) * 3600 + int(mm) * 60 + int(ss)) if ss else (int(h) * 60 + int(mm))
        total = 0 if i == 0 else total + off
        out.append(f"{total // 60:02d}:{total % 60:02d} {label}")
    return out


def prepend_intro(
    intro: str | Path,
    main: str | Path,
    out_path: str | Path,
    *,
    crf: int = 20,
    preset: str = "medium",
) -> tuple[Path, float]:
    """イントロを本編の先頭へ連結する。返り値 ``(出力, イントロ実尺秒)``。

    イントロを**本編の規格へ再エンコード**してから concat demuxer で ``-c copy``。
    イントロは10秒程度なので再エンコードは安い。
    """
    intro, main = Path(intro).resolve(), Path(main).resolve()
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    spec = probe_av(main)
    intro_dur = probe_av(intro)["duration"]
    work = Path(tempfile.mkdtemp())
    conv = work / "intro_conformed.mp4"

    fps = int(round(spec["fps"])) or 25
    vf = f"fps={fps},scale={spec['width']}:{spec['height']},setsar=1"
    cmd = [
        ffmpeg_path(), "-y", "-i", str(intro),
        "-vf", vf,
        "-c:v", "libx264", "-profile:v", "high", "-level", "4.0",
        "-pix_fmt", "yuv420p", "-crf", str(crf), "-preset", preset,
        "-c:a", "aac", "-ar", str(spec["sample_rate"]),
        "-ac", str(spec["channels"]), "-b:a", "192k",
        str(conv),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        tail = ffmpeg_error(proc.stderr)
        raise RuntimeError(f"イントロの規格合わせに失敗:\n{tail}")

    lst = work / "concat.txt"
    lst.write_text(
        "".join(f"file '{p.as_posix()}'\n" for p in (conv, main)), encoding="utf-8"
    )
    cmd = [
        ffmpeg_path(), "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
        "-c", "copy", "-movflags", "+faststart", str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        tail = ffmpeg_error(proc.stderr)
        raise RuntimeError(f"連結に失敗:\n{tail}")
    return out_path, intro_dur
