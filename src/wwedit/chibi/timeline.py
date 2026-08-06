"""ちびキャラの口パク・感情タイムライン生成（**出力時刻系**・freeze対応σ写像を使用）。

発話タイミング → 「話者側だけ口パク」のスプライト区間列 → ffconcat プレイリスト。
- 方式A（Seed-VC・尺保存）: **word タイミング**から発話スパンを作る。
- 方式B（TTS）: 元 word は無効なので **voice_tts_report.json の配置済みクリップ実尺**から作る。
無音区間=口閉じ、発話中は**閉/開の2枚を離散的にパタパタ切り替える**（中間フレームは作らない
＝ゆっくり系の実際の作り。補間で繋ぐと線がボケてアニメらしさが落ちる）。
感情は**基本 normal**で、割当のある発話の頭だけ数秒リアクションとして変える。

スプライトの解決は ``assets.sprite_path``（char×emotion×mouth×eye）に一元化されており、
第二弾の瞬き（eye 次元）はこのモジュールの ``SpriteInterval.eye`` を埋めるだけで拡張できる。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from wwedit.compose.ffmpeg_compose import src_to_out
from wwedit.edl.schema import Edl, TimeRange, voiced_word_spans

__all__ = [
    "SpriteInterval", "GAP_MERGE_S", "MIN_SPAN_S", "MOUTH_STEP_S", "EMOTION_HOLD_S",
    "voiced_word_spans", "speaking_spans_from_words", "speaking_spans_from_report",
    "mouth_track", "emotion_track", "emotion_track_from_report",
    "build_side_timeline", "write_ffconcat",
]

GAP_MERGE_S = 0.25       # word 間の隙間がこれ未満なら連続発話とみなす
MIN_SPAN_S = 0.12        # これ未満の孤立スパンは捨てる（ノイズ）
MOUTH_STEP_S = 0.083     # 口の切替1段の表示秒（≒12fps。1サイクル 8段 ≒ 0.66s）
MOUTH_WAVE = (1, 0, 1, 1, 0, 1, 0, 0)  # 0=閉 / 1=開。等間隔だと機械的なので粗密を付ける
#: 感情を出す長さ。基本は normal で、割当のある発話の頭だけ短く表情を変える（メリハリ重視）
EMOTION_HOLD_S = 2.5


@dataclass(frozen=True)
class SpriteInterval:
    """出力時刻 [start, end) に表示するスプライト（キャラ×感情×口段。eye は瞬き用予約）。"""

    start: float
    end: float
    emotion: str
    mouth: int
    eye: int | None = None


def _merge_spans(spans: list[tuple[float, float]], *, merge_gap: float,
                 min_span: float) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for s, e in sorted(spans):
        if e <= s:
            continue
        if out and s - out[-1][1] <= merge_gap:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out if e - s >= min_span]


def speaking_spans_from_words(
    edl: Edl, ranges: list[TimeRange], speaker: str, *, freezes=(),
) -> list[tuple[float, float]]:
    """方式A: word タイミングから話者の発話スパン（出力秒）を作る。"""
    raw: list[tuple[float, float]] = []
    for u in edl.utterances:
        if u.speaker != speaker:
            continue
        spans = voiced_word_spans(u.words) if u.words else [(u.start, u.end)]
        for s, e in spans:
            os_, oe = src_to_out(ranges, s, freezes), src_to_out(ranges, e, freezes)
            if oe - os_ > 1e-3:
                raw.append((os_, oe))
    return _merge_spans(raw, merge_gap=GAP_MERGE_S, min_span=MIN_SPAN_S)


def _row_span(r: dict, ranges: list[TimeRange], freezes) -> tuple[float, float]:
    """report の1行 → クリップが**実際に鳴っている出力区間**。

    ``out_start`` は直列スケジュール後の確定位置。無い旧レポートは発話 start から引く。
    """
    out_at = (float(r["out_start"]) if "out_start" in r
              else src_to_out(ranges, float(r["u_start"]), freezes))
    dur = float(r["tts_s"]) / max(1.0, float(r.get("atempo") or 1.0))
    return out_at, out_at + dur


def speaking_spans_from_report(
    report_rows: list[dict], ranges: list[TimeRange], speaker: str, *, freezes=(),
) -> list[tuple[float, float]]:
    """方式B: voice_tts_report.json の行から、配置済みTTSクリップの実区間（出力秒）を作る。

    **元音声の word タイミングを使ってはいけない**（方式Bは読み上げ音声なので、元の発話と
    長さも位置も違う＝口と声が全く合わない。実走で指摘を受けた）。
    """
    raw = [_row_span(r, ranges, freezes)
           for r in report_rows if r.get("speaker") == speaker]
    return _merge_spans(raw, merge_gap=GAP_MERGE_S, min_span=MIN_SPAN_S)


def emotion_track_from_report(
    edl: Edl, report_rows: list[dict], ranges: list[TimeRange], speaker: str,
    total: float, *, freezes=(), hold: float = EMOTION_HOLD_S,
) -> list[tuple[float, float, str]]:
    """方式B: 感情も**読み上げクリップの位置**に合わせる（元発話位置ではない）。

    ``EDL.emotion_cues`` があればそれを使う（有声区間ごとの時刻付きキュー）。キューの
    ソース秒に**最も近いクリップ**の頭から出す。キューが無い古い EDL では従来どおり
    utterance の感情を、その utterance の最初のクリップの頭に出す。
    """
    if edl.emotion_cues:
        return _emotion_track_from_cues(edl, report_rows, ranges, speaker, total,
                                        freezes=freezes, hold=hold)
    first: dict[int, float] = {}
    for r in report_rows:
        if r.get("speaker") != speaker:
            continue
        u_idx = int(r.get("u_idx", r.get("idx", -1)))
        at = _row_span(r, ranges, freezes)[0]
        if u_idx >= 0 and (u_idx not in first or at < first[u_idx]):
            first[u_idx] = at

    spans: list[tuple[float, float, str]] = []
    for u_idx, at in sorted(first.items(), key=lambda kv: kv[1]):
        u = edl.utterances[u_idx] if 0 <= u_idx < len(edl.utterances) else None
        if u is None or not u.emotion:
            continue
        end = min(total, at + hold)
        if end > at:
            spans.append((at, end, u.emotion))
    return _fill_normal(spans, total)


def _emotion_track_from_cues(
    edl: Edl, report_rows: list[dict], ranges: list[TimeRange], speaker: str,
    total: float, *, freezes=(), hold: float = EMOTION_HOLD_S,
) -> list[tuple[float, float, str]]:
    """キュー（ソース秒）を**読み上げクリップの位置**へ写す。

    方式Bでは映像・音声の時刻がクリップ位置で決まるので、キューのソース秒をそのまま
    出力秒にしてはいけない。同じ話者のクリップのうち**元発話がキューを含む/最も近い**
    ものを選び、その頭から出す。
    """
    rows = [r for r in report_rows if r.get("speaker") == speaker]
    if not rows:
        return _fill_normal([], total)
    spans: list[tuple[float, float, str]] = []
    for c in edl.emotion_cues:
        if c.speaker != speaker or c.emotion == "normal":
            continue
        best = min(rows, key=lambda r: (
            0.0 if float(r.get("src_start", 0)) <= c.at <= float(r.get("src_end", 0))
            else min(abs(c.at - float(r.get("src_start", 0))),
                     abs(c.at - float(r.get("src_end", 0))))))
        at = _row_span(best, ranges, freezes)[0]
        end = min(total, at + hold)
        if end > at:
            spans.append((at, end, c.emotion))
    spans.sort()
    return _fill_normal(spans, total)


def mouth_track(
    spans: list[tuple[float, float]], total: float, *, step: float = MOUTH_STEP_S,
) -> list[tuple[float, float, int]]:
    """発話スパン列 → (start, end, mouth) の全時間トラック。スパン外=0(閉)、終端は必ず0。"""
    out: list[tuple[float, float, int]] = []
    pos = 0.0
    for s, e in spans:
        s, e = max(0.0, s), min(e, total)
        if e <= s:
            continue
        if s > pos:
            out.append((pos, s, 0))
        t = s
        i = 0
        while t < e:
            seg_end = min(t + step, e)
            mouth = MOUTH_WAVE[i % len(MOUTH_WAVE)]
            if seg_end >= e:
                mouth = 0  # スパン終端は閉に戻す
            out.append((t, seg_end, mouth))
            t = seg_end
            i += 1
        pos = e
    if pos < total:
        out.append((pos, total, 0))
    return out


def emotion_track(
    edl: Edl, ranges: list[TimeRange], speaker: str, total: float, *, freezes=(),
    hold: float = EMOTION_HOLD_S,
) -> list[tuple[float, float, str]]:
    """(start, end, emotion) の全時間トラック。**基本は normal**、割当のある発話だけ短く変える。

    以前は「次の割当まで感情が持続」だったが、utterance は相槌をまたぐ数十秒の塊なので
    1つの surprised が何分も続いてメリハリが無くなっていた。ここでは割当を
    **発話の頭から ``hold`` 秒だけのリアクション**として扱い、それ以外は normal に戻す。

    ``EDL.emotion_cues`` があればそれを使う（有声区間ごとの時刻付きなので、実際に驚いた
    瞬間に表情が動く）。無い場合だけ utterance 単位に落ちる＝時刻精度は utterance の頭まで。
    """
    spans: list[tuple[float, float, str]] = []
    for c in edl.emotion_cues:
        if c.speaker != speaker or c.emotion == "normal":
            continue
        os_ = src_to_out(ranges, c.at, freezes)
        end = min(os_ + hold, total)
        if end - os_ > 1e-3:
            spans.append((os_, end, c.emotion))
    if spans:
        spans.sort()
        return _fill_normal(spans, total)
    for u in edl.utterances:
        if u.speaker != speaker or not u.emotion or u.emotion == "normal":
            continue
        os_ = src_to_out(ranges, u.start, freezes)
        oe = src_to_out(ranges, u.end, freezes)
        end = min(oe, os_ + hold, total)
        if end - os_ > 1e-3:
            spans.append((os_, end, u.emotion))
    return _fill_normal(spans, total)


def _fill_normal(
    spans: list[tuple[float, float, str]], total: float
) -> list[tuple[float, float, str]]:
    """リアクション区間の隙間を normal で埋めて全時間トラックにする（重なりは先勝ち）。"""
    out: list[tuple[float, float, str]] = []
    pos = 0.0
    for s, e, emo in sorted(spans):
        s = max(s, pos)
        if e <= s or emo == "normal":
            continue          # 直前のリアクションに飲まれた
        if s > pos:
            out.append((pos, s, "normal"))
        out.append((s, e, emo))
        pos = e
    if total > pos:
        out.append((pos, total, "normal"))
    return [(s, e, m) for s, e, m in out if e - s > 1e-9]


def build_side_timeline(
    edl: Edl, ranges: list[TimeRange], speaker: str, *,
    total: float, freezes=(), spans: list[tuple[float, float]] | None = None,
    emotions: list[tuple[float, float, str]] | None = None,
    step: float = MOUTH_STEP_S,
) -> list[SpriteInterval]:
    """1体分のスプライト区間列（mouth×emotion の区間交差・同一スプライト連続は結合）。

    ``spans``/``emotions`` を渡すと**そちらを使う**（方式Bは読み上げクリップ由来を渡す）。
    """
    if spans is None:
        spans = speaking_spans_from_words(edl, ranges, speaker, freezes=freezes)
    mouths = mouth_track(spans, total, step=step)
    if emotions is None:
        emotions = emotion_track(edl, ranges, speaker, total, freezes=freezes)

    out: list[SpriteInterval] = []
    ei = 0
    for ms, me, mouth in mouths:
        t = ms
        while t < me - 1e-9:
            while ei < len(emotions) - 1 and emotions[ei][1] <= t + 1e-9:
                ei += 1
            es, ee, emo = emotions[ei]
            seg_end = min(me, ee)
            if seg_end <= t + 1e-9:
                seg_end = me  # 防御（感情トラック終端超え）
            prev = out[-1] if out else None
            if prev and prev.emotion == emo and prev.mouth == mouth and \
                    abs(prev.end - t) < 1e-9:
                out[-1] = SpriteInterval(prev.start, seg_end, emo, mouth, prev.eye)
            else:
                out.append(SpriteInterval(t, seg_end, emo, mouth))
            t = seg_end
    return out


def _ffconcat_path(p: Path) -> str:
    # concat demuxer はシングルクォート囲み。Windows パスは / 区切りにして ' をエスケープ
    return str(p.resolve()).replace("\\", "/").replace("'", r"'\''")


def write_ffconcat(
    intervals: list[SpriteInterval], char: str, out_path: Path,
    *, available_emotions: set[str] | None = None,
) -> Path:
    """スプライト区間列を ffconcat プレイリストへ書き出す（合計 duration = 出力尺）。

    ``available_emotions``: アセットが実在する感情。無い感情は normal のスプライトへ落とす
    （生成漏れでも合成を止めない）。末尾はフレームを重複させる（concat demuxer の既知仕様:
    最終エントリの duration が無視されるため）。
    """
    from wwedit.chibi.assets import sprite_path

    def resolve(emotion: str, mouth: int) -> Path:
        if available_emotions is not None and emotion not in available_emotions:
            emotion = "normal"
        return sprite_path(char, emotion, mouth)

    lines = ["ffconcat version 1.0"]
    last: Path | None = None
    for iv in intervals:
        p = resolve(iv.emotion, iv.mouth)
        lines.append(f"file '{_ffconcat_path(p)}'")
        lines.append(f"duration {iv.end - iv.start:.5f}")
        last = p
    if last is not None:
        lines.append(f"file '{_ffconcat_path(last)}'")  # 末尾重複（duration無視対策）
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path
