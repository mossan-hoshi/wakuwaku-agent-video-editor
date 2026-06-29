"""STT駆動の無音/フィラーカット（Recut自動化のコア）。

固定dB閾値は使わない。STT の word単位タイムスタンプで「発話している区間」を確定し、
- keep区間 = 各語の区間を前後マージン(pad_s)で広げて結合（語頭/語尾を削らない）
- 近接(bridge_s未満)の keep区間は繋ぐ（過剰分割を防ぐ）
- 残りの隙間 = 無音（カット候補）
フィラー語は別途カット候補にする（特に sakamoto/mossan-hoshi）。

マージン pad_s は語頭/語尾の取りこぼし防止の要。既定 0.15s。Recut(fcpxml)との突合で調整する。
"""

from __future__ import annotations

from wwedit.edl.schema import Edl, Segment, Utterance

__all__ = [
    "FILLERS",
    "CUTTABLE_FILLERS",
    "keep_regions_from_utterances",
    "segments_from_keep",
    "mark_fillers_from_utterances",
    "mark_filler_intervals",
    "mark_invalid_intervals",
    "filler_intervals_from_chars",
    "build_segments",
    "is_filler",
]

# 単独で現れたらカット対象にするフィラー語（前後の記号/空白は除去して判定）
FILLERS = {
    "えー", "えーと", "えっと", "あの", "あのー", "あのう", "んー", "うーん",
    "まあ", "そのー", "その", "なんか", "ええと", "ええ", "あー", "うー",
}

# 文字単位アライメント(WhisperX)用の保守的フィラー集合。
# 実発話に紛れやすい語（なんか/その/ええ/あの/まあ）は誤カット回避のため除外し、
# 伸ばし記号付きの明確な間投詞のみを、前後が句読点/間で区切られた時だけ切る。
CUTTABLE_FILLERS = (
    "えーっと", "えーと", "えっと", "ええと", "あのー", "あのう",
    "そのー", "うーん", "んー", "えー", "あー", "うー", "んーと",
)

# 区切りとみなす記号（前後がこれ/発話端/時間ギャップならフィラーは単独とみなす）
_BOUNDARY = set("、。，．・…！？!?「」（）()　 \n\t")

Interval = tuple[float, float]


def is_filler(text: str) -> bool:
    t = text.strip().strip("、。,. 　").replace("ー", "ー")
    return t in FILLERS


def filler_intervals_from_chars(
    utterances: list[Utterance], *, gap_s: float = 0.2
) -> list[Interval]:
    """文字単位 word から、前後が区切られた明確なフィラーの時間区間を抽出する。

    WhisperX は日本語を1文字ずつ整列するため語単位照合ができない。発話ごとに文字列を組み、
    ``CUTTABLE_FILLERS`` を最長一致で探す。マッチは**前後が句読点/発話端/時間ギャップ(gap_s)**
    で区切られている時のみ採用（実発話の途中を切らない＝precision優先）。
    """
    out: list[Interval] = []
    fillers = sorted(CUTTABLE_FILLERS, key=len, reverse=True)
    for u in utterances:
        chars = [w for w in u.words if w.text]
        s = "".join(w.text for w in chars)
        n = len(s)
        i = 0
        while i < n:
            matched = False
            for f in fillers:
                m = len(f)
                if s[i : i + m] != f:
                    continue
                # 左境界
                left_ok = i == 0 or s[i - 1] in _BOUNDARY or (
                    chars[i].start - chars[i - 1].end >= gap_s
                )
                # 右境界
                j = i + m
                right_ok = j >= n or s[j] in _BOUNDARY or (
                    chars[j].start - chars[j - 1].end >= gap_s
                )
                if left_ok and right_ok:
                    out.append((chars[i].start, chars[j - 1].end))
                    i = j
                    matched = True
                    break
            if not matched:
                i += 1
    return out


def _word_intervals(utterances: list[Utterance]) -> list[Interval]:
    """全話者の word区間を時刻順に集める。"""
    iv: list[Interval] = []
    for u in utterances:
        for w in u.words:
            if w.end > w.start:
                iv.append((w.start, w.end))
    iv.sort()
    return iv


def keep_regions_from_utterances(
    utterances: list[Utterance],
    *,
    pad_s: float = 0.15,
    bridge_s: float = 0.4,
    min_keep_s: float = 0.2,
) -> list[Interval]:
    """発話語からマージン付き keep区間を作る。

    ``pad_s``: 各語の前後に足すマージン（語頭/語尾を削らない）。
    ``bridge_s``: keep区間どうしの隙間がこれ未満なら繋ぐ。
    ``min_keep_s``: これより短い keep区間は捨てる（孤立ノイズ語対策）。
    """
    iv = _word_intervals(utterances)
    if not iv:
        return []
    # マージン付与
    padded = [(max(0.0, s - pad_s), e + pad_s) for s, e in iv]
    # 結合（bridge 以下の隙間は繋ぐ）
    merged: list[Interval] = [padded[0]]
    for s, e in padded[1:]:
        ps, pe = merged[-1]
        if s - pe <= bridge_s:
            merged[-1] = (ps, max(pe, e))
        else:
            merged.append((s, e))
    return [(s, e) for s, e in merged if e - s >= min_keep_s]


def segments_from_keep(keep: list[Interval], dur: float) -> list[Segment]:
    """keep区間列から keep/無音(silence) の Segment 列を作る。

    keep の供給元（STT word境界 / VAD発話区間）に依らず共通で使う。
    無音 = keep区間の隙間（reason="silence"）。
    """
    segments: list[Segment] = []
    idx = 0
    prev = 0.0
    for s, e in keep:
        if s > prev + 1e-6:
            segments.append(
                Segment(id=f"seg{idx:04d}", start=prev, end=s, invalid=True, reason="silence")
            )
            idx += 1
        segments.append(Segment(id=f"seg{idx:04d}", start=s, end=e, invalid=False))
        idx += 1
        prev = e
    if dur > prev + 1e-6:
        segments.append(
            Segment(id=f"seg{idx:04d}", start=prev, end=dur, invalid=True, reason="silence")
        )
    return segments


def build_segments(
    edl: Edl,
    *,
    pad_s: float = 0.15,
    bridge_s: float = 0.4,
    cut_fillers: bool = True,
) -> list[Segment]:
    """EDL.utterances から keep/無音/フィラーの Segment 列を作る。

    無音 = keep区間の隙間（reason="silence"）。
    フィラー = keep区間内でも単独フィラー語の区間を invalid(reason="filler")にする。
    """
    keep = keep_regions_from_utterances(edl.utterances, pad_s=pad_s, bridge_s=bridge_s)
    segments = segments_from_keep(keep, edl.source.duration_s)

    # フィラー語を invalid 化（keep区間内の単独フィラー）
    if cut_fillers:
        segments = mark_fillers_from_utterances(segments, edl.utterances)
    return segments


def mark_fillers_from_utterances(
    segments: list[Segment], utterances: list[Utterance]
) -> list[Segment]:
    """発話の word から単独フィラー区間を抽出し、keep区間内のそれを invalid 化する。

    VAD由来の無音セグメントに STT(WhisperX) のフィラーを重ねるのに使う（[C] の音量＋フィラー）。
    word が文字単位(WhisperX)か語単位(faster-whisper)かを自動判別して抽出方法を切替える。
    """
    words = [w for u in utterances for w in u.words]
    char_level = bool(words) and sum(len(w.text) == 1 for w in words) / len(words) > 0.8
    if char_level:
        filler_iv = filler_intervals_from_chars(utterances)
    else:
        filler_iv = [(w.start, w.end) for w in words if is_filler(w.text)]
    return mark_invalid_intervals(segments, filler_iv, reason="filler")


def mark_filler_intervals(
    segments: list[Segment], intervals: list[Interval]
) -> list[Segment]:
    """明示的なフィラー時間区間（LLM判断由来など）で keep区間を invalid(filler)化する。"""
    return mark_invalid_intervals(segments, list(intervals), reason="filler")


def mark_invalid_intervals(
    segments: list[Segment], intervals: list[Interval], *, reason: str = "filler"
) -> list[Segment]:
    """keep区間にかかる任意の区間を切り出して invalid(reason=...)化する汎用処理。

    フィラー(reason="filler")・NGワード(reason="ngword")など、LLM/規則由来の
    「ここを切る」区間を keep区間に重ねるのに共通で使う。invalid区間には触れない。
    """
    if not intervals:
        return segments
    pfx = (reason or "x")[0]  # id接頭辞: filler→f / ngword→n
    out: list[Segment] = []
    idx = 0
    for seg in segments:
        if seg.invalid:
            out.append(seg)
            continue
        # この keep区間に重なるカット区間で分割
        cuts = sorted(
            (max(seg.start, fs), min(seg.end, fe))
            for fs, fe in intervals
            if fe > seg.start and fs < seg.end
        )
        cursor = seg.start
        for cs, ce in cuts:
            if cs > cursor + 1e-6:
                out.append(Segment(id=f"k{idx:04d}", start=cursor, end=cs, invalid=False))
                idx += 1
            out.append(
                Segment(id=f"{pfx}{idx:04d}", start=cs, end=ce, invalid=True, reason=reason)
            )
            idx += 1
            cursor = max(cursor, ce)
        if seg.end > cursor + 1e-6:
            out.append(Segment(id=f"k{idx:04d}", start=cursor, end=seg.end, invalid=False))
            idx += 1
    return out
