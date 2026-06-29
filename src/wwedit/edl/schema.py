"""EDL スキーマ（pydantic v2）。

時間は基本的に **ソース動画タイムライン上の秒（float）** で保持する。
編集判断の粒度ではフレーム誤差は無視できる。最終的な fcpxml/ffmpeg 出力時に
``common.timecode`` で各メディアのタイムベースへ量子化する。

「カット＝削除」ではなく ``Segment.invalid`` フラグ＋``reason`` で表現し、
戻し/範囲調整を可能にする（plan の必須要件）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

EDL_VERSION = 1

# 区間を無効化（カット候補）にした理由
CutReason = Literal[
    "silence",  # 無音
    "filler",  # フィラー（えー/あのー等。特に sakamoto）
    "setup",  # 冒頭セットアップ
    "switch",  # 画面共有切替
    "offtopic",  # 勉強会に無関係（技術雑談は除く）
    "privacy",  # 個人情報（会社名/家族/sakamoto・taniguchi以外の人名/プライベート）
    "ngword",  # NGワード(.env WWEDIT_CUT_NGWORDS)に言及した発話
    "manual",  # 手動指定
]

# フレーミング区間の種別
FramingKind = Literal[
    "static",  # 同一フレーミングで安定
    "loading",  # 画面切替 → ローディング画面に置換
    "pending",  # 判定保留（ユーザー確認待ち。作業は止めない）
]

SubtitleStyle = Literal["main", "intro"]  # main=緑〜水色二重枠 / intro=ピンク二重枠
BgmScope = Literal["main", "section", "intro"]


class TimeRange(BaseModel):
    start: float = Field(..., description="ソースタイムライン上の開始秒")
    end: float = Field(..., description="ソースタイムライン上の終了秒")

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


class SpeakerTrack(BaseModel):
    speaker: str = Field(..., description="話者ID（例: mossan-hoshi, Taniguchi）")
    path: str = Field(..., description="話者別 m4a の絶対パス")
    is_desktop_audio: bool = Field(
        False, description="デスクトップ音声録音らしきトラック（文字起こし対象外）"
    )


class SourceMedia(BaseModel):
    video_path: str
    fps: int = 30
    width: int = 1920
    height: int = 1080
    duration_s: float = 0.0
    audio_tracks: list[SpeakerTrack] = Field(default_factory=list)


class Word(BaseModel):
    text: str
    start: float
    end: float


class Utterance(BaseModel):
    """話者統合トランスクリプトの1発話。"""

    speaker: str
    text: str
    start: float
    end: float
    words: list[Word] = Field(default_factory=list)


class Segment(BaseModel):
    """ソースタイムライン上の1区間。invalid=カット候補（戻せる）。"""

    id: str
    start: float
    end: float
    invalid: bool = False
    reason: CutReason | None = None
    note: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


class Chapter(BaseModel):
    """stt-chapter-detector の出力単位。"""

    start_at: float
    is_required: bool = True
    chapter_title: str = ""
    section_title: str | None = Field(
        None, description="セクション開始チャプターのみに付与"
    )


class FramingRegion(BaseModel):
    """同一フレーミングで居られる区間と、その代表 bbox。"""

    start: float
    end: float
    kind: FramingKind = "static"
    bbox: tuple[int, int, int, int] | None = Field(
        None, description="メイン領域 (x, y, w, h)。loading/pending では None も可"
    )
    loading_label: str | None = Field(
        None, description="loading 時の『○○中...』の○○"
    )
    warning: str = ""


class Subtitle(BaseModel):
    start: float
    end: float
    text: str
    style: SubtitleStyle = "main"
    speaker: str | None = Field(None, description="主に喋っている話者（本編字幕の色分けに使う）")


class BgmCue(BaseModel):
    start: float
    end: float
    path: str
    gain_db: float = -20.0
    scope: BgmScope = "main"


class PostUnit(BaseModel):
    """投稿単位（1収録から1〜2本）。各単位が1本のYouTube動画になる。"""

    id: str
    title: str = ""
    ranges: list[TimeRange] = Field(
        default_factory=list, description="この投稿に含めるソース区間（無効区間除外後）"
    )
    chapter_ids: list[int] = Field(default_factory=list)


class Edl(BaseModel):
    version: int = EDL_VERSION
    recording_dir: str = Field(..., description="正規化後の収録フォルダ（YYYY-MM-DD）")
    source: SourceMedia

    segments: list[Segment] = Field(default_factory=list)
    utterances: list[Utterance] = Field(default_factory=list)
    chapters: list[Chapter] = Field(default_factory=list)
    framing: list[FramingRegion] = Field(default_factory=list)
    subtitles: list[Subtitle] = Field(default_factory=list)
    subtitle_speaker_colors: dict[str, str] = Field(
        default_factory=dict,
        description="話者名→パレットキー(red/purple/blue/green)の上書き。空=自動(寒色/暖色で割当)。"
        "Webアプリで後から話者ごとに切替可能。",
    )
    bgm: list[BgmCue] = Field(default_factory=list)
    post_units: list[PostUnit] = Field(default_factory=list)

    meta: dict = Field(default_factory=dict, description="任意のメタ情報")

    def kept_ranges(self) -> list[TimeRange]:
        """invalid でない区間（実際に残す区間）を返す。"""
        return [TimeRange(start=s.start, end=s.end) for s in self.segments if not s.invalid]


def load_edl(path: str | Path) -> Edl:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return Edl.model_validate(data)


def save_edl(edl: Edl, path: str | Path) -> None:
    Path(path).write_text(
        json.dumps(edl.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
