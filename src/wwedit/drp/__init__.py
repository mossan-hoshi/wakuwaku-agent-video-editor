"""DaVinci Resolve プロジェクト書き出し (.drp) の読み込み。

`.drp` は **ZIP + XML**（バイナリではない）。`SeqContainer/<uuid>.xml` に各タイムライン、
クリップは `Sm2TiVideoClip` / `Sm2TiAudioClip` で、Start/Duration/MediaStartTime は
**フレーム数**（`MediaFrameRate` は hex-double）。これが wakuwaku の **最終編集の正解**。

このプロジェクトは「複数日のタイムラインが混在するモノプロジェクト」で、各日が MediaPool の
bin（例 `006_20260604`）。タイムラインは MediaFilePath の日付で各日に対応づける。

- カット/タイミングは平文XMLから抽出可能（実装済）。
- フレーミング transform(Zoom/Pan/Crop) は各クリップの `FieldsBlob`（hex-UTF16 シリアライズ）
  内。デコードは後続（`transform` モジュール）。
"""

from wwedit.drp.reader import Clip, Timeline, keep_ranges_for_day, read_timelines

__all__ = ["Clip", "Timeline", "read_timelines", "keep_ranges_for_day"]
