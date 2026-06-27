"""ゴールデン検証 — 既存の編集成果物を参照に回帰評価する。

重要な前提:
- `video<id>.fcpxml` は **Recut の出力（無音/カット段階）であり、Resolve の最終編集結果ではない**。
- 最終編集には音量だけでなく**意味的編集**（オフトピック/個人情報の除去等）が入るため、
  **fcpxml との完全一致は目指さない**。fcpxml はあくまで「無音カット段階の目安」。
  → recall/precision は参考指標。意味的カットは fcpxml にも我々の無音検出にも現れない/異なる。
- Resolve の最終編集（フレーミング/字幕/BGM）はプロジェクトDB側にある。見つかればフレーミング等の
  正解に使う（探索中）。fcpxml には crop 情報は無い。
"""

from wwedit.eval.golden import (
    GOLDEN_DIRS,
    interval_total,
    intervals_overlap,
    removed_silence_from_fcpxml,
    score_cuts,
)

__all__ = [
    "GOLDEN_DIRS",
    "interval_total",
    "intervals_overlap",
    "removed_silence_from_fcpxml",
    "score_cuts",
]
