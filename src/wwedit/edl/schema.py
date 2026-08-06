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

# ユーザーが手で置くオーバーレイ（最上位レイヤー）の種別
OverlayKind = Literal["image", "text", "mosaic"]
TextAlign = Literal["left", "center", "right"]  # 複数行テキストの横揃え
MosaicType = Literal["pixelate", "gaussian"]    # モザイク方式（低解像度 / ガウシアン）
MosaicShape = Literal["rect", "ellipse"]        # モザイク形状（矩形 / 円・楕円）


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
    voice_path: str | None = Field(
        None,
        description=(
            "キャラ声差し替え後の wav。None=元の path を使う（非破壊・voice-revert で戻せる）"
        ),
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


#: 句読点のみの word。Whisper は無音をこれらのトークンに吸わせる（「。」が3.7秒など）
PUNCT_CHARS = frozenset("、。，．,.!?！？…‥・「」『』（）()：:；;　 \t\n")
#: 1文字あたりの発話上限秒。word の end は次の word の start と一致していて隙間が無いので、
#: 文字数から妥当な長さを見積もり、超過分は無音として扱う。
#: 口パク用の既定（見た目のズレが問題になるので短め）。
MAX_SEC_PER_CHAR = 0.22
#: 音声変換用。**小さい声・ゆっくりの発話を切らない**ことを優先して大きく取る。
#: 余分に無音が入っても Seed-VC は無音を無音のまま返す（実測: 元無音窓の変換後は中央 -70dB台）
#: ので実害はGPU時間だけ。逆に切りすぎると発話そのものが消えるため、こちらに倒す。
VOICE_SEC_PER_CHAR = 1.0
MIN_WORD_S = 0.10


def voiced_word_spans(
    words: list[Word], *, max_sec_per_char: float = MAX_SEC_PER_CHAR,
) -> list[tuple[float, float]]:
    """word 列から**実際に声が出ている区間**（素材秒）だけを取り出す。

    Whisper の word タイミングは隙間ゼロで連続しており、間（無音）は句読点トークンや
    直前の語の end に吸われている（実データで「ー」1文字が18秒など）。utterance の
    start/end は相槌をまたぐ数十秒の塊なので、そのまま使うと大半が無音になる
    （口パク・声変換の両方で問題になる）。
    (1) 句読点のみの word を捨て (2) 文字数から見積もった上限で各 word を打ち切る。

    音量では判定しない。小さい声でも Whisper が文字起こししていれば残る。
    ``max_sec_per_char`` は用途で変える（口パク=短め / 音声変換=長め）。
    """
    out: list[tuple[float, float]] = []
    for w in words:
        text = (getattr(w, "text", "") or "").strip()
        if not text or all(ch in PUNCT_CHARS for ch in text):
            continue
        cap = max(MIN_WORD_S, len(text) * max_sec_per_char)
        end = min(w.end, w.start + cap)
        if end > w.start:
            out.append((w.start, end))
    return out


# ちびキャラの感情（chibi-emotion-assigner スキルが発話単位で付与）
ChibiEmotion = Literal["normal", "smile", "surprised", "troubled", "angry", "thinking"]


class Utterance(BaseModel):
    """話者統合トランスクリプトの1発話。"""

    speaker: str
    text: str
    start: float
    end: float
    words: list[Word] = Field(default_factory=list)
    emotion: ChibiEmotion | None = Field(
        None, description="ちびキャラの感情。None=normal（差分のみ付与・次の割当まで持続）"
    )


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
    speaker: str = Field(
        "", description="その章を主に担当している話者（左上リボンの色分けに使う・"
        "chapter-detector が章の会話全体から判断）"
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


class Overlay(BaseModel):
    """ユーザーが編集ツールで配置する画像/テキスト/モザイク（非破壊）。

    時刻は他フィールドと同じ**ソースタイムライン秒**で持ち、合成時に出力時刻へ変換する。
    位置 ``x``/``y``・領域 ``w``/``h`` は **ソースフレーム基準の正規化座標(0..1)・左上基準**。
    編集ツールはソース映像の上に置くので、**素材の同じ場所に貼り付く**（モザイクが被写体を
    追従する）。合成時にフレーミング crop で切り出され拡大されるぶんは
    ``compose.overlay.place_overlays`` が写像する（``scale``/``size``/枠/``strength`` にも
    crop 拡大率が掛かるので、見かけの大きさはソース基準で保たれる）。

    レイヤー順（下→上）は 映像 → 画像 → **モザイク** → 字幕 → チャプターリボン → テキスト。
    モザイクは映像とユーザー画像にだけ掛かり、文字情報/UI には掛からない。
    """

    id: str
    kind: OverlayKind = "text"
    start: float = Field(..., description="ソースタイムライン上の表示開始秒")
    end: float = Field(..., description="ソースタイムライン上の表示終了秒")
    x: float = Field(0.5, description="左上基準の正規化X(0..1)・**ソースフレーム基準**")
    y: float = Field(0.5, description="左上基準の正規化Y(0..1)・**ソースフレーム基準**")

    # kind="image"
    path: str = Field("", description="画像ファイルの絶対パス（透過PNG可）")
    scale: float = Field(1.0, description="画像の拡大率（1.0=原寸）")
    opacity: float = Field(1.0, description="画像の不透明度 0..1")

    # kind="mosaic"（bbox形式で下の映像をぼかす／モザイク化）
    w: float = Field(0.25, description="領域の幅（**ソース幅**に対する正規化 0..1）")
    h: float = Field(0.25, description="領域の高さ（**ソース高**に対する正規化 0..1）")
    mosaic_type: MosaicType = Field(
        "pixelate", description="モザイク方式（pixelate=低解像度 / gaussian=ぼかし）"
    )
    shape: MosaicShape = Field("rect", description="形状（rect=矩形 / ellipse=円・楕円）")
    strength: float = Field(
        16.0,
        description="強さ。pixelate=ブロックの粗さ(px) / gaussian=ぼかし半径(sigma)。"
        "**ソース基準**で、crop 拡大率が掛かる",
    )

    # kind="text"
    text: str = Field("", description="表示文字列（\\n で改行）")
    font: str = Field("Meiryo", description="フォント名")
    size: int = Field(64, description="フォントサイズ(px・**ソース解像度基準**)")
    color: str = Field(
        "blue",
        description="文字色。パレットキー(red/purple/blue/green) か #RRGGBB",
    )
    double_border: bool = Field(
        True,
        description="二重縁取り（色文字→白1次枠→同色外枠＝字幕と同一仕様）",
    )
    white_ring: float = Field(
        5.0, description="1次枠線(白・固定色)の太さ。0.01刻みで調整"
    )
    outer_outline: float = Field(
        9.0, description="外枠(文字と同色)の太さ。double_border=True のときだけ使う"
    )
    align: TextAlign = Field("left", description="複数行テキストの横揃え（左/中央/右）")
    line_spacing: float = Field(
        1.0, description="行間の倍率（1.0=標準。枠が被るなら上げる）"
    )

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


class Freeze(BaseModel):
    """方式B（TTS読み上げ）で合成音声が元尺を超えた時のフリーズフレーム。

    レンダ時に ``at`` の位置で映像を静止して ``extra`` 秒だけ伸ばす。
    カット境界・framing・章 start_at は動かさない（加算情報のみ＝G2手修正ルール適合）。
    """

    at: float = Field(..., description="ソース秒（keep区間内・発話end直前に置く）")
    extra: float = Field(..., description="伸ばす秒数")
    note: str = ""


class EmotionCue(BaseModel):
    """[E] ちびキャラの表情を動かす**瞬間**（ソース秒）。

    以前は ``Utterance.emotion``（＝発話まるごとに1つ）だったが、utterance は相槌をまたぐ
    数十秒の塊なので「塊の頭で1回」しか表情を動かせず、**実際に驚いた瞬間に驚けない**
    という指摘を受けた（明らかに驚いている所が normal のまま／「なるほど」に surprised）。
    ここでは有声区間ごとの**時刻付きキュー**にして、鳴った瞬間に短く出す。
    """

    at: float = Field(..., description="ソース秒（この時刻から EMOTION_HOLD_S 秒だけ出す）")
    speaker: str
    emotion: ChibiEmotion
    source: str = Field("", description="audio / text / both（どちらの根拠で付けたか）")
    score: float = Field(0.0, description="音声判定のスコア（テキスト由来は0）")


class ChibiConfig(BaseModel):
    """ゆっくり風ちびキャラ表示の設定。"""

    enabled: bool = False
    sides: dict[str, str] = Field(
        default_factory=dict,
        description='{"left": 話者, "right": 話者}。空=話者名ソート順で左→右',
    )
    height_px: int = Field(320, description="ちびキャラの表示高さ(px・1080p基準)")
    margin_px: tuple[int, int] = Field((24, 24), description="画面端からの余白 (x, y)")
    flip_sides: list[str] = Field(
        default_factory=lambda: ["left"],
        description="左右反転する側。2体を対面させるため既定で left を反転する",
    )


class InfographicConfig(BaseModel):
    """[I] 本編冒頭に出す**要約インフォグラフィック**（横長1枚）の表示設定。

    イントロは別ファイルとして前に連結されるので、``start_s``/``duration_s`` は
    **本編の出力タイムライン**の秒（0.0 = 本編の1フレーム目）で持つ。
    サイズは「上部UI（チャプターリボン）・ちびキャラ・字幕に**重ならない**安全枠」に
    contain 収めする（``compose.infographic_overlay.safe_box``）。
    """

    enabled: bool = True
    path: str = Field("", description="生成済み横長PNGの絶対パス。空=表示しない")
    start_s: float = Field(0.0, description="本編出力タイムライン上の表示開始秒")
    duration_s: float = Field(10.0, description="表示秒数")
    fade_s: float = Field(0.4, description="表示前後のフェード秒（0=カットイン）")
    top_reserve_px: int = Field(
        78, description="上部に空ける高さ(px・1080p基準)。チャプターリボン54px＋余白",
    )
    bottom_reserve_px: int = Field(
        352, description="下部に空ける高さ(px)。ちびキャラ(320+24)と字幕の大きい方＋余白",
    )
    side_margin_px: int = Field(48, description="左右に空ける幅(px)")


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
    overlays: list[Overlay] = Field(
        default_factory=list,
        description="ユーザー配置の最上位レイヤー（画像/テキスト）。編集ツールで追加・移動する。",
    )
    post_units: list[PostUnit] = Field(default_factory=list)

    character_cast: dict[str, str] = Field(
        default_factory=dict,
        description="話者→のべつべキャラid（noa/yume/...）。音声変換・字幕色・ちびキャラ表示の"
        "共有SoT。publish voice-cast が書き込む。",
    )
    freezes: list[Freeze] = Field(
        default_factory=list,
        description="方式B（TTS読み上げ）のフリーズフレーム。空=タイムライン無変更",
    )
    emotion_cues: list[EmotionCue] = Field(
        default_factory=list,
        description="[E] ちびキャラの表情キュー（ソース秒）。空なら Utterance.emotion に落ちる",
    )
    chibi: ChibiConfig | None = Field(None, description="ちびキャラ表示設定。None=無効")
    infographic: InfographicConfig | None = Field(
        None, description="[I] 本編冒頭の要約インフォグラフィック表示設定。None=無効",
    )

    meta: dict = Field(
        default_factory=dict,
        description='任意のメタ情報。規約キー: "voice"={method, confirmed_at, prev_colors}',
    )

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
