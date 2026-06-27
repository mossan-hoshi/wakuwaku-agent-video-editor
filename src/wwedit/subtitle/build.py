"""[I] EDL.utterances から字幕(Subtitle)列を作る。

main 字幕は本来「要所のみ」（[I]）だが、その取捨は LLM/ヒューリスティックの後段。ここでは
発話単位の素直な字幕化を提供し、長い発話は読みやすい長さに分割、極短・空は除外する。
要所選別やイントロ全文(style=intro)は呼び出し側で差し替える。
"""

from __future__ import annotations

from wwedit.edl.schema import Edl, Subtitle, SubtitleStyle

__all__ = ["split_text", "subtitles_from_utterances"]


def split_text(text: str, max_chars: int = 28) -> list[str]:
    """長い本文を max_chars 程度の読みやすいチャンクへ分割（句読点優先・なければ機械分割）。"""
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []
    chunks: list[str] = []
    cur = ""
    for ch in text:
        cur += ch
        # 句読点で区切れる長さに達したら切る
        if len(cur) >= max_chars and ch in "、。！？!?,.　 ":
            chunks.append(cur.strip())
            cur = ""
    if cur.strip():
        chunks.append(cur.strip())
    # それでも長すぎるチャンクは機械分割
    out: list[str] = []
    for c in chunks:
        while len(c) > max_chars:
            out.append(c[:max_chars])
            c = c[max_chars:]
        if c:
            out.append(c)
    return out


def subtitles_from_utterances(
    edl: Edl,
    *,
    style: SubtitleStyle = "main",
    max_chars: int = 28,
    min_dur: float = 0.4,
) -> list[Subtitle]:
    """各発話を字幕化（長文は分割し、発話区間内に時間を比例配分）。ソース時刻のまま返す。"""
    subs: list[Subtitle] = []
    for u in edl.utterances:
        if not u.text.strip() or u.end - u.start < min_dur:
            continue
        parts = split_text(u.text, max_chars=max_chars)
        if not parts:
            continue
        span = (u.end - u.start) / len(parts)
        for i, p in enumerate(parts):
            subs.append(
                Subtitle(
                    start=u.start + span * i,
                    end=u.start + span * (i + 1),
                    text=p,
                    style=style,
                    speaker=u.speaker,
                )
            )
    return subs
