"""投稿単位(PostUnit)ごとの出力対象を解決する（1収録→複数投稿）。

各投稿は EDL.post_units[idx] に対応し、その**ソース区間 ∩ kept** を連結して1本の動画になる。
compose は本モジュールの区間を `ranges` として受け、字幕/フレーミング/BGMはそこから一貫導出される。
"""

from __future__ import annotations

from wwedit.edl.schema import Edl, TimeRange


def _src_to_out(ranges: list[TimeRange], t: float) -> float:
    """ソース秒 t を、指定 ranges を連結した出力秒へ（カット内なら次区間先頭へスナップ）。"""
    acc = 0.0
    for r in ranges:
        if t < r.start:
            return acc
        if t <= r.end:
            return acc + (t - r.start)
        acc += r.end - r.start
    return acc


def post_unit_ranges(edl: Edl, idx: int) -> list[TimeRange]:
    """投稿単位 idx の出力対象区間 = kept ∩ 単位スパン[min start, max end)。

    post_units が無い/範囲未設定なら kept 全体（=従来の1本）を返す。
    """
    units = edl.post_units or []
    if idx >= len(units) or not units[idx].ranges:
        return edl.kept_ranges()
    unit = units[idx]
    lo = min(r.start for r in unit.ranges)
    hi = max(r.end for r in unit.ranges)
    out: list[TimeRange] = []
    for r in edl.kept_ranges():
        a, b = max(r.start, lo), min(r.end, hi)
        if b > a + 1e-9:
            out.append(TimeRange(start=a, end=b))
    return out


def post_unit_chapter_lines(edl: Edl, idx: int) -> list[str]:
    """投稿単位 idx の YouTube章行（**単位内の出力時刻**・先頭は必ず 00:00）。"""
    ranges = post_unit_ranges(edl, idx)
    if not ranges:
        return []
    lo, hi = ranges[0].start, ranges[-1].end
    chs = [c for c in sorted(edl.chapters, key=lambda c: c.start_at)
           if lo - 1e-6 <= c.start_at < hi]
    lines: list[str] = []
    for i, c in enumerate(chs):
        ot = 0.0 if i == 0 else _src_to_out(ranges, c.start_at)
        h, rem = divmod(int(ot), 3600)
        m, s = divmod(rem, 60)
        ts = f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
        lines.append(f"{ts} {c.chapter_title or f'チャプター{i + 1}'}")
    return lines


def n_post_units(edl: Edl) -> int:
    """投稿単位の数（0なら未設定＝収録まるごと1本扱い）。"""
    return len(edl.post_units or [])
