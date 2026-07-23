"""チャプター/投稿単位の検出を LLM にファイル経由で任せる入出力（[D]）。

filler-selector と同型: 文字起こし（話者ラベル付き発話列）をインデックス付きでファイルに書き出し、
LLM(chapter-detector スキル) が章境界＋タイトル＋投稿単位を JSON で返す。インデックス→時刻の
変換はコード側で行い、LLM には時刻計算をさせない。

時刻は EDL 同様すべて**ソースタイムライン秒**で持つ（YouTube用の出力時刻は書き出し時に変換）。
"""

from __future__ import annotations

import json
from pathlib import Path

from wwedit.edl.schema import Chapter, Edl, PostUnit, TimeRange

__all__ = [
    "write_chapter_input",
    "apply_decisions",
    "source_to_output",
    "youtube_chapter_lines",
]


def _mmss(t: float) -> str:
    m, s = divmod(int(t), 60)
    return f"{m:02d}:{s:02d}"


def write_chapter_input(edl: Edl, out_path: str | Path) -> Path:
    """LLM 用の発話インデックス付きトランスクリプトを書き出す。

    各行 ``<idx>\\t<mm:ss>\\t<speaker>\\t<text>``。idx は発話の並び順で、apply 時に
    そのまま ``edl.utterances[idx].start`` へ対応する。
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# idx\ttime\tspeaker\ttext（章の切れ目=話題の変わり目の発話idxを選ぶ）"]
    for i, u in enumerate(edl.utterances):
        text = u.text.replace("\t", " ").replace("\n", " ").strip()
        if not text:
            continue
        lines.append(f"{i}\t{_mmss(u.start)}\t{u.speaker}\t{text}")
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def source_to_output(edl: Edl, t: float) -> float:
    """ソース時刻 t を、カット適用後（keep連結）の出力タイムライン秒へ変換する。

    t が keep 区間内ならその区間先頭までの累積 keep 長＋区間内オフセット。
    t がカット区間内なら、直後の keep の開始（=それまでの累積 keep 長）にスナップ。
    """
    acc = 0.0
    for r in edl.kept_ranges():
        if t < r.start:
            return acc  # カット区間内 → 次keepの先頭へ
        if t <= r.end:
            return acc + (t - r.start)
        acc += r.end - r.start
    return acc


def youtube_chapter_lines(edl: Edl) -> list[str]:
    """YouTube説明欄用のチャプター行（出力タイムライン・先頭は必ず 00:00）。"""
    lines: list[str] = []
    for i, c in enumerate(sorted(edl.chapters, key=lambda c: c.start_at)):
        ot = 0.0 if i == 0 else source_to_output(edl, c.start_at)
        h, rem = divmod(int(ot), 3600)
        m, s = divmod(rem, 60)
        ts = f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
        title = c.chapter_title or f"チャプター{i + 1}"
        lines.append(f"{ts} {title}")
    return lines


def _kept_ranges_within(edl: Edl, start: float, end: float) -> list[TimeRange]:
    """[start,end) と各 keep 区間の交差を返す（投稿単位の実収録区間）。"""
    out: list[TimeRange] = []
    for r in edl.kept_ranges():
        lo, hi = max(r.start, start), min(r.end, end)
        if hi > lo:
            out.append(TimeRange(start=lo, end=hi))
    return out


def apply_decisions(edl: Edl, decisions_path: str | Path) -> Edl:
    """LLM の章/投稿単位決定を EDL.chapters / EDL.post_units に反映する。

    decisions JSON:
      ``{"chapters":[{"utt":int,"title":str,"section_title":str|null,"is_required":bool}],
         "post_units":[{"title":str,"chapters":[<章index>...]}]}``
    chapters は utt で開始位置を指す。post_units の chapters は chapters 配列のインデックス。
    """
    dec = json.loads(Path(decisions_path).read_text(encoding="utf-8"))
    n_utt = len(edl.utterances)

    # 章を生成（utt→start_at へ変換、開始時刻で整列）
    raw = dec.get("chapters", [])
    chapters: list[Chapter] = []
    for c in raw:
        idx = int(c.get("utt", 0))
        if not (0 <= idx < n_utt):
            continue
        chapters.append(
            Chapter(
                start_at=edl.utterances[idx].start,
                is_required=bool(c.get("is_required", True)),
                chapter_title=str(c.get("title", "")).strip(),
                section_title=(c.get("section_title") or None),
                speaker=str(c.get("speaker", "")).strip(),
            )
        )
    chapters.sort(key=lambda c: c.start_at)
    edl.chapters = chapters

    # 投稿単位を生成。各単位の章範囲→実収録区間へ。指定が無ければ全体で1単位。
    dur = edl.source.duration_s
    starts = [c.start_at for c in chapters]
    units_in = dec.get("post_units") or [{"title": "", "chapters": list(range(len(chapters)))}]
    post_units: list[PostUnit] = []
    for k, pu in enumerate(units_in):
        ch_idx = [i for i in pu.get("chapters", []) if 0 <= i < len(chapters)]
        if not ch_idx:
            continue
        lo = starts[min(ch_idx)]
        last = max(ch_idx)
        hi = starts[last + 1] if last + 1 < len(starts) else dur
        post_units.append(
            PostUnit(
                id=f"post{k:02d}",
                title=str(pu.get("title", "")).strip(),
                ranges=_kept_ranges_within(edl, lo, hi),
                chapter_ids=ch_idx,
            )
        )
    edl.post_units = post_units
    return edl
