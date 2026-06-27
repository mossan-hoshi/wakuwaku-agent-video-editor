"""ゴールデン検証の中核（区間オーバーラップ指標と fcpxml 正解抽出）。"""

from __future__ import annotations

from pathlib import Path

from wwedit.compose.fcpxml import read_keep_ranges

__all__ = [
    "GOLDEN_DIRS",
    "removed_silence_from_fcpxml",
    "intervals_overlap",
    "interval_total",
    "score_cuts",
]

Interval = tuple[float, float]

# 編集済み（Recut の .fcpxml がある）収録 = カット正解のゴールデンセット
GOLDEN_DIRS: list[str] = [
    "D:/Users/sackn/Videos/wakuwaku/2026-06-04",
    "D:/Users/sackn/Videos/wakuwaku/2026-06-01",
    "D:/Users/sackn/Videos/wakuwaku/2026-05-28",
]


def removed_silence_from_fcpxml(fcpxml: str | Path, duration: float) -> list[Interval]:
    """fcpxml の keep区間の補集合 = Recut が除去した区間（無音/フィラー等）。"""
    keep = read_keep_ranges(fcpxml)
    gaps: list[Interval] = []
    prev = 0.0
    for r in keep:
        if r.start > prev + 1e-6:
            gaps.append((prev, r.start))
        prev = max(prev, r.end)
    if duration > prev + 1e-6:
        gaps.append((prev, duration))
    return gaps


def interval_total(intervals: list[Interval]) -> float:
    return sum(max(0.0, e - s) for s, e in intervals)


def intervals_overlap(a: list[Interval], b: list[Interval]) -> float:
    """2つの区間集合の重なり合計（秒）。"""
    a = sorted(a)
    b = sorted(b)
    i = j = 0
    total = 0.0
    while i < len(a) and j < len(b):
        lo = max(a[i][0], b[j][0])
        hi = min(a[i][1], b[j][1])
        if hi > lo:
            total += hi - lo
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return total


def score_cuts(pred: list[Interval], gt: list[Interval]) -> dict[str, float]:
    """予測カット区間 vs 正解カット区間の recall/precision/IoU。

    recall   = 正解カットのうち予測でも切れた割合（取りこぼしの少なさ）
    precision= 予測カットのうち正解でも切られた割合（過剰カットの少なさ）
    iou      = 重なり / 和集合
    """
    ov = intervals_overlap(pred, gt)
    p, g = interval_total(pred), interval_total(gt)
    union = p + g - ov
    return {
        "recall": ov / g if g else 0.0,
        "precision": ov / p if p else 0.0,
        "iou": ov / union if union else 0.0,
        "pred_s": p,
        "gt_s": g,
        "overlap_s": ov,
    }
