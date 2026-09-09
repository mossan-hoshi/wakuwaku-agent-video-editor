"""LLM に渡した原文と**いまの原文**を突き合わせ、古くなった決定を見つける。

方式B の読み上げ台本も要約字幕も、作り方は同じ形をしている:

1. `prepare` が**その時点の EDL** から入力TSV（`<idx>\\t…\\t<text>`）を書く
2. スキル(LLM)が `{"lines"|"captions": [{idx, text}]}` を返す
3. 後段が **idx をキーに**その文を読み上げ／字幕にする

ここに穴がある。**手順2と3の間（G2）で人がカットを直す**と、idx の指す中身が変わるのに
決定JSONは古い原文のまま残る。結果、**切ったはずの発言がそのまま読み上げられ、字幕にも出る**。
実際に 2026-08-06 の回で、ほぼ全部カットした発話の内容が方式Bだけに復活し、
個人的な事情がそのまま喋られた（`docs/STATUS.md` §21.10）。

対策は単純で、**prepare 時の原文がTSVに残っている**のだから突き合わせればよい。
食い違った idx は「台本が古い」＝ **既定で落とす**（読み上げない・字幕に出さない）。
止めるのではなく落とす: エラーにすると `--allow` 相当で押し切られて事故が再発する。
"""

from __future__ import annotations

from difflib import SequenceMatcher
from pathlib import Path

__all__ = ["STALE_RATIO", "prepared_texts", "stale_indices", "stale_against_tsv",
           "realign"]

#: これ未満の一致率なら「別の中身」とみなす。**完全一致は求めない**——
#: word 境界が 0.01 秒動いただけで読点1つが出入りすることがあり、それで台本を
#: 捨てると無音の穴が増えるだけだから。逆に半分カットされれば 0.5 前後まで落ちる。
STALE_RATIO = 0.9


def _norm(text: str) -> str:
    return "".join((text or "").split())


def prepared_texts(tsv_path: str | Path) -> dict[int, str]:
    """入力TSV から ``{idx: 原文}`` を読む。

    `#` 始まりの行（見出し・末尾に付く画面OCR文脈）と空行は飛ばす。**原文は最終カラム**
    （TTS は `idx/speaker/char/slot_s/gap_s/text`、字幕は `idx/out_time/speaker/text`）。
    TSV が無ければ空 dict＝「照合できない」を返す（呼び手が黙って落とさないように）。
    """
    path = Path(tsv_path)
    if not path.exists():
        return {}
    out: dict[int, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        cols = raw.split("\t")
        if len(cols) < 2:
            continue
        try:
            idx = int(cols[0])
        except ValueError:
            continue                      # `idx\tspeaker\t…` の見出し行
        out[idx] = cols[-1]
    return out


def realign(prepared: dict[int, str], current: dict[int, str],
            *, threshold: float = STALE_RATIO) -> dict[int, int]:
    """``{いまの idx: 台本を書いたときの idx}`` を**本文で**対応づける。

    idx がずれる原因は「中身が変わった」より **「途中のターンが増減して番号が繰り上がった」**
    方が圧倒的に多い。2026-08-06 の回は 214→197 ターンになり、**133件が食い違ったうち
    110件は同じ文が別番号に居るだけ**だった。ここで落とすと発話の6割が無音になる——
    直すどころか壊す。だから**まず貼り直す**。

    両方とも時間順なので、対応は**単調**（交差しない）でなければならない。`difflib` の
    opcode で一致ブロックを取り、残った塊の中だけ貪欲に近い者どうしを繋ぐ。
    """
    p_keys, c_keys = sorted(prepared), sorted(current)
    p_txt = [_norm(prepared[k]) for k in p_keys]
    c_txt = [_norm(current[k]) for k in c_keys]
    out: dict[int, int] = {}

    def _greedy(pi0: int, pi1: int, ci0: int, ci1: int) -> None:
        """一致しなかった塊。**順序を保ったまま**似た者だけ繋ぐ。"""
        pi = pi0
        for ci in range(ci0, ci1):
            best, best_r = -1, threshold
            for j in range(pi, pi1):
                r = SequenceMatcher(None, p_txt[j], c_txt[ci]).ratio()
                if r >= best_r:
                    best, best_r = j, r
            if best >= 0:
                out[c_keys[ci]] = p_keys[best]
                pi = best + 1

    for tag, i1, i2, j1, j2 in SequenceMatcher(None, p_txt, c_txt, autojunk=False).get_opcodes():
        if tag == "equal":
            for k in range(j2 - j1):
                out[c_keys[j1 + k]] = p_keys[i1 + k]
        elif tag == "replace":
            _greedy(i1, i2, j1, j2)
    return out


def stale_against_tsv(tsv_path: str | Path, current: dict[int, str],
                      *, threshold: float = STALE_RATIO) -> list[int]:
    """入力TSV と突き合わせて古い idx を返す。**TSV が無ければ何も落とさない**。

    照合材料が無いときに全部落とすと、TSV を1つ消しただけで**無音・無字幕の動画**が
    出来上がる。落とすのは「食い違いを実際に見つけたとき」だけにする。
    """
    prepared = prepared_texts(tsv_path)
    if not prepared:
        return []
    return stale_indices(prepared, current, threshold=threshold)


def stale_indices(prepared: dict[int, str], current: dict[int, str],
                  *, threshold: float = STALE_RATIO) -> list[int]:
    """台本を書いたときの原文と、いまの原文がずれた idx（昇順）。

    ``current`` に無い idx は見ない（そもそも読み上げ対象にならない）。
    ``prepared`` に無い idx は**ずれたものとして扱う**——照合材料が無いのに
    その idx の文を喋らせるのは、まさに今回やらかした事故そのものだから。
    """
    bad: list[int] = []
    for idx, now in current.items():
        was = prepared.get(idx)
        if was is None:
            bad.append(idx)
            continue
        a, b = _norm(was), _norm(now)
        if a == b:
            continue
        if not a or not b or SequenceMatcher(None, a, b).ratio() < threshold:
            bad.append(idx)
    return sorted(bad)
