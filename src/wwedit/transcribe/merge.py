"""話者別の word列を、単一の話者ラベル付きトランスクリプト（発話列）へ統合する。

2ファイルを別々にLLMへ渡すと混乱するため、発話ごとに話者を付与した単一列にまとめる（plan [B]）。
"""

from __future__ import annotations

from wwedit.edl.schema import Utterance, Word
from wwedit.transcribe.stt import Word as SttWord

__all__ = ["words_to_utterances", "merge_speakers"]


def words_to_utterances(
    speaker: str, words: list[SttWord], *, gap_s: float = 1.0
) -> list[Utterance]:
    """1話者の word列を、無音の隙間（gap_s 以上）で発話単位に区切る。"""
    utterances: list[Utterance] = []
    cur: list[SttWord] = []

    def flush() -> None:
        if not cur:
            return
        utterances.append(
            Utterance(
                speaker=speaker,
                text="".join(w.text for w in cur).strip(),
                start=cur[0].start,
                end=cur[-1].end,
                words=[Word(text=w.text, start=w.start, end=w.end) for w in cur],
            )
        )

    for w in words:
        if cur and w.start - cur[-1].end > gap_s:
            flush()
            cur = []
        cur.append(w)
    flush()
    return utterances


def merge_speakers(
    per_speaker: dict[str, list[SttWord]], *, gap_s: float = 1.0
) -> list[Utterance]:
    """複数話者の word列を統合し、時刻順の発話列にする。"""
    all_utts: list[Utterance] = []
    for speaker, words in per_speaker.items():
        all_utts.extend(words_to_utterances(speaker, words, gap_s=gap_s))
    all_utts.sort(key=lambda u: u.start)
    return all_utts
