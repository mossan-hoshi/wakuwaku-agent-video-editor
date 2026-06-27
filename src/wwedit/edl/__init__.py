"""EDL (Edit Decision List) — 編集状態の単一の真実 (SSOT)。

各工程はこの JSON を読み、自工程の結果を追記/更新する。
最終的に ffmpeg 合成器と fcpxml 書き出し器がこれを消費する。
"""

from wwedit.edl.schema import (
    BgmCue,
    Chapter,
    Edl,
    FramingRegion,
    PostUnit,
    Segment,
    SourceMedia,
    SpeakerTrack,
    Subtitle,
    Utterance,
    Word,
    load_edl,
    save_edl,
)

__all__ = [
    "BgmCue",
    "Chapter",
    "Edl",
    "FramingRegion",
    "PostUnit",
    "Segment",
    "SourceMedia",
    "SpeakerTrack",
    "Subtitle",
    "Utterance",
    "Word",
    "load_edl",
    "save_edl",
]
