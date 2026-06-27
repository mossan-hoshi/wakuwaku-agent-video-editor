"""無音検出（ffmpeg ``silencedetect``）。

GPU 不要。video の音声を基準に無音区間を求める（plan: 無音基準はビデオ音声）。
出力は EDL の ``Segment``（invalid=True, reason="silence"）へ変換できる。

将来は学習済みモデルに差し替えるが、当面のベースライン兼・fcpxml との突合検証用。
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from wwedit.common.media import ffmpeg_path
from wwedit.edl.schema import Segment

__all__ = ["SilenceInterval", "detect_silence", "silence_to_segments"]

_START_RE = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_END_RE = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")


@dataclass
class SilenceInterval:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def detect_silence(
    media_path: str | Path,
    noise_db: float = -30.0,
    min_silence_s: float = 0.3,
) -> list[SilenceInterval]:
    """``media_path`` の音声から無音区間を返す。

    ``noise_db``: これより小さい音量を無音とみなす閾値（dB）。
    ``min_silence_s``: 無音とみなす最小継続時間（秒）。
    """
    cmd = [
        ffmpeg_path(),
        "-hide_banner",
        "-nostats",
        "-i",
        str(media_path),
        "-map",
        "0:a:0",
        "-af",
        f"silencedetect=noise={noise_db}dB:d={min_silence_s}",
        "-f",
        "null",
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    # silencedetect は stderr に出力する
    lines = proc.stderr.splitlines()

    intervals: list[SilenceInterval] = []
    pending_start: float | None = None
    for line in lines:
        m = _START_RE.search(line)
        if m:
            pending_start = float(m.group(1))
            continue
        m = _END_RE.search(line)
        if m and pending_start is not None:
            end = float(m.group(1))
            intervals.append(SilenceInterval(start=max(0.0, pending_start), end=end))
            pending_start = None
    return intervals


def silence_to_segments(
    intervals: list[SilenceInterval],
    source_duration_s: float,
) -> list[Segment]:
    """無音区間列を、発話(invalid=False)と無音(invalid=True)の連続 Segment 列へ。"""
    segments: list[Segment] = []
    idx = 0
    cursor = 0.0
    for iv in sorted(intervals, key=lambda x: x.start):
        s = max(0.0, min(iv.start, source_duration_s))
        e = max(0.0, min(iv.end, source_duration_s))
        if s > cursor + 1e-6:
            segments.append(Segment(id=f"seg{idx:04d}", start=cursor, end=s, invalid=False))
            idx += 1
        if e > s + 1e-6:
            segments.append(
                Segment(id=f"seg{idx:04d}", start=s, end=e, invalid=True, reason="silence")
            )
            idx += 1
        cursor = max(cursor, e)
    if source_duration_s > cursor + 1e-6:
        segments.append(
            Segment(id=f"seg{idx:04d}", start=cursor, end=source_duration_s, invalid=False)
        )
    return segments
