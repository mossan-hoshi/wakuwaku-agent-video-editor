"""時間表現の正規化。

収録データは時間単位が混在する:
- fcpxml タイムライン: ``N/25s`` (25fps)
- ソース mp4: ``46962000/30000s`` (30fps系)
- 話者別 m4a: ``75135936/48000s`` (48kHz)

内部では常に ``fractions.Fraction``（秒）で厳密に保持し、表示や fcpxml 書き出し時に
任意のタイムベース（分母）へ量子化する。float は丸め誤差が累積するため境界計算には使わない。
"""

from __future__ import annotations

from fractions import Fraction

__all__ = ["parse_rational", "format_rational", "to_seconds", "frames_to_seconds"]


def parse_rational(value: str) -> Fraction:
    """fcpxml の有理数時間文字列を秒の ``Fraction`` にする。

    >>> parse_rational("108/25s")
    Fraction(108, 25)
    >>> parse_rational("0s")
    Fraction(0, 1)
    >>> parse_rational("46962000/30000s")
    Fraction(7827, 5)
    """
    s = value.strip()
    if s.endswith("s"):
        s = s[:-1]
    s = s.strip()
    if not s:
        raise ValueError(f"empty rational time: {value!r}")
    if "/" in s:
        num_str, den_str = s.split("/", 1)
        return Fraction(int(num_str), int(den_str))
    return Fraction(int(s), 1)


def format_rational(seconds: Fraction | int | float, timebase: int) -> str:
    """秒を ``N/<timebase>s`` 形式へ量子化して文字列化する（fcpxml 書き出し用）。

    ``timebase`` はタイムラインの分母（例: 25fps なら 25、音声 48kHz なら 48000）。

    >>> format_rational(Fraction(108, 25), 25)
    '108/25s'
    >>> format_rational(1.0, 25)
    '25/25s'
    """
    sec = seconds if isinstance(seconds, Fraction) else Fraction(seconds).limit_denominator(10**9)
    frames = round(sec * timebase)
    return f"{frames}/{timebase}s"


def to_seconds(value: Fraction | int | float | str) -> float:
    """任意の時間表現を float 秒へ（表示・ログ・概算用。境界計算には使わない）。"""
    if isinstance(value, str):
        return float(parse_rational(value))
    return float(value)


def frames_to_seconds(frames: int, fps: int) -> Fraction:
    """フレーム数を秒の ``Fraction`` へ。"""
    return Fraction(frames, fps)
