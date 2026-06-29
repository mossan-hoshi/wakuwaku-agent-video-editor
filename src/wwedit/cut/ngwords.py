"""NGワードによるカット判定。

`.env`(gitignore済) の ``WWEDIT_CUT_NGWORDS``（カンマ区切り）に挙げた語に **言及した発話
(utterance) をまるごと** カット対象（invalid, reason="ngword"）にする。語は PII 同様に
コード/リポジトリへ一切埋め込まない（[[pii-masking-and-ocr-engine]] と同方針）。未設定なら
空＝何もしない（安全側デグレード）。

判定は発話テキストへの部分一致（日本語は語境界が曖昧なため substring）。どの語に当たったかは
秘匿情報になりうるので、ログ/標準出力には語そのものを出さず件数のみ報告する。
"""

from __future__ import annotations

from wwedit.cut.autocut import mark_invalid_intervals
from wwedit.edl.schema import Edl, Segment, Utterance

__all__ = ["NGWORDS_ENV", "load_ngwords", "ng_intervals_from_utterances", "apply_ngword_cuts"]

NGWORDS_ENV = "WWEDIT_CUT_NGWORDS"
Interval = tuple[float, float]


def load_ngwords(env_var: str = NGWORDS_ENV, env_file: str = ".env") -> list[str]:
    """NGワードを取得（os.environ 優先・無ければ .env）。未設定なら空リスト。"""
    # PII語と同じ「.env のカンマ区切りを実行時に読む」汎用パーサを再利用する。
    from wwedit.privacy.masking import load_mask_terms

    return load_mask_terms(env_var=env_var, env_file=env_file)


def ng_intervals_from_utterances(
    utterances: list[Utterance], ngwords: list[str]
) -> list[Interval]:
    """NG語を含む発話の (start, end) 区間を返す（発話まるごと）。

    発話テキストは ``Utterance.text``（無ければ words から再構成）に対し部分一致で判定。
    ngwords が空なら空リスト。
    """
    terms = [w for w in (ngwords or []) if w]
    if not terms:
        return []
    out: list[Interval] = []
    for u in utterances:
        text = u.text or "".join(w.text for w in u.words)
        if any(t in text for t in terms):
            out.append((u.start, u.end))
    return out


def apply_ngword_cuts(
    edl: Edl, ngwords: list[str] | None = None
) -> tuple[list[Segment], int]:
    """EDL.segments に NG語カット（発話まるごと invalid/ngword）を重ねて返す。

    返り値 ``(segments, n_matched)``。n_matched は NG語に当たった発話数。
    ``ngwords`` 未指定なら .env から読む。語そのものは返さない（秘匿）。
    """
    terms = ngwords if ngwords is not None else load_ngwords()
    iv = ng_intervals_from_utterances(edl.utterances, terms)
    if not iv:
        return edl.segments, 0
    return mark_invalid_intervals(edl.segments, iv, reason="ngword"), len(iv)
