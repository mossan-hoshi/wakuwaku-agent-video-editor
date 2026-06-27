"""フィラーの意味判断を LLM(Haiku等)にファイル経由で任せるための入出力。

「どのフィラーを切るか」は文字列規則では決まらない（例: 「なんか」が口癖か「何か」の意味か、
「ええ」が相槌か肯定か）。そこで広めの正規表現で**候補**を全部抽出し、ID＋文脈付きで
ファイルに書き出す。LLM はそれを読み、意味的に切るべきIDだけを別ファイルへ返す。
タイムスタンプ等のインデックス計算は LLM にさせず、こちら側で id→区間 を保持して堅牢にする。

「どこで切るか（音響的な切れ目）」は別途 ``cut.energy`` が音量の谷へスナップする。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from wwedit.edl.schema import Edl, Utterance

__all__ = [
    "FillerCandidate",
    "extract_candidates",
    "write_candidate_files",
    "load_decisions_to_intervals",
]

# 広めのフィラー候補パターン（漏れなく拾い、取捨は LLM が意味で判断する）
_FILLER_RE = re.compile(
    r"(えー+っと|えー+と|えっと|ええと|えー+|あのー+|あのう|あの|そのー+|うーん|"
    r"んー+と|んー+|まあ+|なんか|ええ|あー+|うー+)"
)

# 文脈として前後に見せる文字数
_CTX = 18


@dataclass
class FillerCandidate:
    id: int
    speaker: str
    text: str  # フィラー候補文字列
    start: float
    end: float
    context: str  # 前後文脈つき（候補は【】で囲む）


def _utt_char_stream(u: Utterance):
    """発話を (文字列, 各文字のWord) に。utterance.text は strip 済みのため words から再構成。"""
    chars = [w for w in u.words if w.text]
    return "".join(w.text for w in chars), chars


def extract_candidates(utterances: list[Utterance]) -> list[FillerCandidate]:
    """全発話からフィラー候補を抽出する（時刻つき）。"""
    cands: list[FillerCandidate] = []
    cid = 0
    for u in utterances:
        s, chars = _utt_char_stream(u)
        if len(s) != len(chars):
            continue  # 文字とWordの対応が崩れている発話はスキップ（安全側）
        for m in _FILLER_RE.finditer(s):
            i, j = m.start(), m.end()
            ctx = s[max(0, i - _CTX) : i] + "【" + s[i:j] + "】" + s[j : j + _CTX]
            cands.append(
                FillerCandidate(
                    id=cid,
                    speaker=u.speaker,
                    text=s[i:j],
                    start=chars[i].start,
                    end=chars[j - 1].end,
                    context=ctx,
                )
            )
            cid += 1
    return cands


def write_candidate_files(edl: Edl, out_dir: str | Path) -> tuple[Path, Path]:
    """LLM 用候補TSVと、id→区間の対応マップJSONを書き出す。

    返り値: (候補TSVパス, マップJSONパス)。LLM には候補TSVだけ渡す。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cands = extract_candidates(edl.utterances)

    tsv = out_dir / "filler_candidates.tsv"
    header = "id\tspeaker\tcontext（【】内が候補。意味のない言い淀みなら切る）"
    lines = [header] + [f"{c.id}\t{c.speaker}\t{c.context}" for c in cands]
    tsv.write_text("\n".join(lines), encoding="utf-8")

    mp = out_dir / "filler_map.json"
    mp.write_text(
        json.dumps([asdict(c) for c in cands], ensure_ascii=False), encoding="utf-8"
    )
    return tsv, mp


def load_decisions_to_intervals(
    map_path: str | Path, decisions_path: str | Path
) -> list[tuple[float, float]]:
    """LLM の決定（切るID集合）を id→区間 マップで時間区間へ変換する。

    decisions JSON は ``{"cut": [id, ...]}`` 形式を想定（リスト直書きも許容）。
    """
    cmap = {c["id"]: c for c in json.loads(Path(map_path).read_text(encoding="utf-8"))}
    dec = json.loads(Path(decisions_path).read_text(encoding="utf-8"))
    cut_ids = dec.get("cut", []) if isinstance(dec, dict) else dec
    iv: list[tuple[float, float]] = []
    for cid in cut_ids:
        c = cmap.get(int(cid))
        if c:
            iv.append((c["start"], c["end"]))
    iv.sort()
    return iv
