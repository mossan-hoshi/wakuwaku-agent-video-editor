"""[J] 合成 / fcpxml 入出力。

- ``fcpxml.read_keep_ranges`` : 既存 Recut 編集 (.fcpxml) から残す区間を抽出。
  無音カットのベースライン兼・学習データ抽出元。
- （後続）EDL -> ffmpeg 合成 / EDL -> fcpxml 書き出し。
"""

from wwedit.compose.fcpxml import (
    KeepRange,
    keep_ranges_to_segments,
    read_keep_ranges,
    write_fcpxml,
)

__all__ = ["KeepRange", "read_keep_ranges", "keep_ranges_to_segments", "write_fcpxml"]
