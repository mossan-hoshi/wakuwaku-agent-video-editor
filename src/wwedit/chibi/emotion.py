"""ちびキャラの感情割当（chibi-emotion-assigner スキルの入出力）。

chapter/caption 工程と同じ**ファイル経由のLLM分業**: 発話TSVを書き出し → スキル（Haiku級）が
差分JSONを返す → EDL の ``Utterance.emotion`` へ適用する。基調は normal（未割当=None=normal）
で、感情は次の割当まで持続する運用（タイムライン側 ``emotion_track`` が補間する）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import get_args

from wwedit.compose.ffmpeg_compose import _src_to_out
from wwedit.edl.schema import ChibiEmotion, Edl

__all__ = [
    "EMOTION_TSV", "EMOTION_DECISIONS", "CHIBI_EMOTIONS",
    "write_emotion_input", "apply_emotion_decisions",
]

EMOTION_TSV = "chibi_emotion_input.tsv"
EMOTION_DECISIONS = "chibi_emotion_decisions.json"

CHIBI_EMOTIONS: tuple[str, ...] = get_args(ChibiEmotion)


def _mmss(t: float) -> str:
    m, s = divmod(int(t), 60)
    return f"{m:02d}:{s:02d}"


def write_emotion_input(edl: Edl, out_tsv: Path, *, audio_json: Path | None = None) -> int:
    """kept区間と交差するマイク話者の**有声区間**を TSV へ書き出し、行数を返す。

    各行 ``key<TAB>time<TAB>speaker<TAB>audio<TAB>text``。

    * ``key`` = ``<utterance添字>:<区間番号>``。utterance まるごとではなく**有声区間ごと**に
      するのが要点。utterance は相槌をまたぐ数十秒の塊なので、塊の先頭テキストで1回しか
      判定できず「なるほど」に surprised が付いていた（ユーザー指摘）。
    * ``audio`` = **元の収録マイク音声**を emotion2vec+ に掛けた結果（`top:score`）。
      合成音（棒読み）ではなく元音声で判定する。無ければ ``-``。
    * ``text`` = その区間に重なる word を繋いだもの（無ければ utterance 全文）。
    """
    from wwedit.chibi.audio_emotion import audio_spans, load_audio_emotions

    ranges = edl.kept_ranges()
    audio = load_audio_emotions(audio_json) if audio_json else {}
    lines = ["key\ttime\tspeaker\taudio\ttext"]
    n = 0
    for it in audio_spans(edl):
        u = edl.utterances[it["utt"]]
        os_ = _src_to_out(ranges, it["start"])
        if _src_to_out(ranges, it["end"]) - os_ <= 1e-3:
            continue
        words = [w.text for w in u.words if w.end > it["start"] and w.start < it["end"]]
        text = " ".join("".join(words).split()) or " ".join((u.text or "").split())
        a = audio.get(it["key"]) or {}
        hint = f"{a.get('top', '')}:{a.get('score', 0):.2f}" if a.get("top") else "-"
        lines.append(f"{it['key']}\t{_mmss(os_)}\t{it['speaker']}\t{hint}\t{text}")
        n += 1
    out_tsv.parent.mkdir(parents=True, exist_ok=True)
    out_tsv.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return n


def apply_emotion_decisions(edl: Edl, decisions_path: Path) -> int:
    """決定JSON ``{"emotions":[{"key":"12:0","emotion":"smile"}]}`` を EDL へ適用する。

    ``key`` は ``<utterance添字>:<有声区間番号>`` で、**その区間の頭に立つキュー**
    (`EDL.emotion_cues`) になる。旧形式 ``{"utt": idx}`` も受け付け、その場合は
    従来どおり ``Utterance.emotion`` に入れる（古い決定JSONを壊さないため）。
    normal は差分に書かれない（未割当＝normal）。適用件数を返す。
    """
    from wwedit.chibi.audio_emotion import audio_spans
    from wwedit.edl.schema import EmotionCue

    data = json.loads(decisions_path.read_text(encoding="utf-8"))
    spans = {it["key"]: it for it in audio_spans(edl)}
    cues: list[EmotionCue] = []
    n = 0
    for row in data.get("emotions", []):
        emo = (row.get("emotion") or "").strip()
        if emo not in CHIBI_EMOTIONS:
            continue
        key = str(row.get("key") or "")
        if key and key in spans:
            it = spans[key]
            if emo != "normal":
                cues.append(EmotionCue(at=float(it["start"]), speaker=it["speaker"],
                                       emotion=emo,  # type: ignore[arg-type]
                                       source=str(row.get("source") or "text"),
                                       score=float(row.get("score") or 0.0)))
            n += 1
            continue
        idx = int(row.get("utt", -1))
        if 0 <= idx < len(edl.utterances):
            edl.utterances[idx].emotion = None if emo == "normal" else emo  # type: ignore[assignment]
            n += 1
    if cues:
        edl.emotion_cues = sorted(cues, key=lambda c: c.at)
    return n
