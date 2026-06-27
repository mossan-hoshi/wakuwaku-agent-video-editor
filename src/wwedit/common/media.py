"""ffmpeg / ffprobe ラッパ。

外部 CLI（PATH 上の ffmpeg/ffprobe）を subprocess で叩く薄いユーティリティ。
重い依存を増やさず、CUDA対応のシステム ffmpeg をそのまま使う方針。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

__all__ = ["MediaInfo", "probe", "ffprobe_path", "ffmpeg_path"]


@dataclass
class MediaInfo:
    path: str
    width: int = 0
    height: int = 0
    fps: int = 0
    duration_s: float = 0.0
    has_video: bool = False
    has_audio: bool = False
    audio_channels: int = 0
    audio_rate: int = 0


def ffprobe_path() -> str:
    p = shutil.which("ffprobe")
    if not p:
        raise RuntimeError("ffprobe が PATH に見つからない")
    return p


def ffmpeg_path() -> str:
    p = shutil.which("ffmpeg")
    if not p:
        raise RuntimeError("ffmpeg が PATH に見つからない")
    return p


def _parse_fps(rate: str) -> int:
    """``r_frame_rate`` ('30000/1001' 等) を最も近い整数 fps へ。"""
    if not rate or rate == "0/0":
        return 0
    try:
        return round(float(Fraction(rate)))
    except (ValueError, ZeroDivisionError):
        return 0


def probe(path: str | Path) -> MediaInfo:
    """ffprobe でメディア情報を取得する。"""
    path = str(path)
    cmd = [
        ffprobe_path(),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        path,
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe 失敗: {path}\n{out.stderr}")
    data = json.loads(out.stdout)

    info = MediaInfo(path=path)
    fmt = data.get("format", {})
    if "duration" in fmt:
        info.duration_s = float(fmt["duration"])

    for st in data.get("streams", []):
        if st.get("codec_type") == "video" and not info.has_video:
            info.has_video = True
            info.width = int(st.get("width", 0))
            info.height = int(st.get("height", 0))
            info.fps = _parse_fps(st.get("r_frame_rate", "0/0"))
        elif st.get("codec_type") == "audio" and not info.has_audio:
            info.has_audio = True
            info.audio_channels = int(st.get("channels", 0))
            info.audio_rate = int(st.get("sample_rate", 0) or 0)
    return info
