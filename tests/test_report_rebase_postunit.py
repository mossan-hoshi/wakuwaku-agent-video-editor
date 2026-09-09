"""読み上げレポートの時刻は**投稿単位[K]のローカル秒へ写してから**使う。

🚨 2026-08-07 の実害（方式B・後半）: `voice_tts_report.json` の ``out_start`` は
**収録まるごとの出力秒**なのに、投稿単位で合成するときそのまま使っていた。
先頭の単位は 0 始まりなので偶然合い、**後半だけ**壊れる:

* 片方のちびは区間が範囲外へ落ちて口が **0.5秒**しか開かない
* もう片方は 735秒の一続きと判定され、全尺**鳴りっぱなし**

ユーザー指摘「ずっとノアしか口パクしてない」。
"""

from __future__ import annotations

from wwedit.chibi.timeline import rebase_report_rows
from wwedit.edl.schema import Edl, Segment, SourceMedia, TimeRange


def _seg(i: int, a: float, b: float, invalid: bool = False) -> Segment:
    return Segment(id=f"s{i}", start=a, end=b, invalid=invalid)


def _edl(cuts=()) -> Edl:
    if not cuts:
        segs = [_seg(0, 0.0, 2000.0)]
    else:
        segs, prev, i = [], 0.0, 0
        for a, b in cuts:
            segs.append(_seg(i, prev, a)); i += 1
            segs.append(_seg(i, a, b, invalid=True)); i += 1
            prev = b
        segs.append(_seg(i, prev, 2000.0))
    return Edl(recording_dir=".",
               source=SourceMedia(video_path="v.mp4", fps=30.0, duration=2000.0),
               segments=segs)


def _rows(*starts):
    return [{"out_start": s, "speaker": "A", "tts_s": 1.0} for s in starts]


def test_the_first_unit_is_unchanged():
    """先頭の単位は 0 始まりなので値が変わらない（ここだけ見て安心してはいけない）。"""
    got = rebase_report_rows(_rows(10.0, 500.0), _edl(),
                             [TimeRange(start=0.0, end=1000.0)])
    assert [r["out_start"] for r in got] == [10.0, 500.0]


def test_a_later_unit_is_rebased_to_zero():
    """**これが本命**。1000秒始まりの単位では 1010 → 10 になる。"""
    got = rebase_report_rows(_rows(1010.0, 1500.0), _edl(),
                             [TimeRange(start=1000.0, end=2000.0)])
    assert [r["out_start"] for r in got] == [10.0, 500.0]


def test_rows_outside_the_unit_are_dropped():
    """単位に入らないクリップは落とす（残すと巨大な1区間に化ける）。"""
    got = rebase_report_rows(_rows(10.0, 1010.0, 1900.0), _edl(),
                             [TimeRange(start=1000.0, end=2000.0)])
    assert [r["out_start"] for r in got] == [10.0, 900.0]


def test_cut_holes_are_accounted_for():
    """カット穴があっても「全体出力秒 → ソース秒 → ローカル出力秒」で正しく写る。"""
    edl = _edl(cuts=[(100.0, 200.0)])          # 100〜200秒をカット（出力尺は1900）
    # 全体出力 150秒 ＝ ソース 250秒。単位をソース 200〜2000 にすると ローカル 50秒。
    got = rebase_report_rows(_rows(150.0), edl, [TimeRange(start=200.0, end=2000.0)])
    assert abs(got[0]["out_start"] - 50.0) < 1e-6


def test_old_rows_without_out_start_pass_through():
    """``out_start`` を持たない旧レポートは触らない（別経路で時刻を作る）。"""
    rows = [{"speaker": "A", "u_start": 5.0, "tts_s": 1.0}]
    got = rebase_report_rows(rows, _edl(), [TimeRange(start=1000.0, end=2000.0)])
    assert got == rows


# ── ワープ後レポートは out_start が**ソース秒** ────────────────────────


def test_warped_report_is_unaffected_by_cuts_made_after_the_warp():
    """🚨 本命。ワープ後EDLにカットを入れても口パク位置が動かないこと。

    2026-08-08 の実害（後半TTS版）: ワープ後EDLに 11.84秒のカットを1つ入れたところ、
    カット地点より後の口パクが**まるごと 11.84秒 遅れた**。ワープ後レポートの
    ``out_start`` は「ワープ素材のソース秒」なのに、``out_to_src`` で出力秒として
    解釈し直し、カット穴のぶん先送りしていたため。ユーザー指摘
    「final_B_p1.mp4 まだ口パクズレ起きてますよ」。
    """
    edl = _edl(cuts=[(1000.0, 1011.84)])
    unit = [TimeRange(start=990.0, end=1000.0), TimeRange(start=1011.84, end=2000.0)]
    # ソース 1100秒のクリップ = 単位ローカル 10（穴の前）+ (1100-1011.84) = 98.16秒
    got = rebase_report_rows(_rows(1100.0), edl, unit, out_is_source=True)
    assert abs(got[0]["out_start"] - 98.16) < 1e-6
    # 誤: 出力秒として解釈すると、カットした 11.84秒ぶん遅れる
    wrong = rebase_report_rows(_rows(1100.0), edl, unit)
    assert abs(wrong[0]["out_start"] - got[0]["out_start"] - 11.84) < 1e-6


def test_warped_report_without_cuts_is_identical_either_way():
    """カットが無いうちは両解釈が一致する＝この罠が**カットを入れるまで潜伏する**証拠。"""
    edl, unit = _edl(), [TimeRange(start=1000.0, end=2000.0)]
    assert (rebase_report_rows(_rows(1010.0, 1500.0), edl, unit, out_is_source=True)
            == rebase_report_rows(_rows(1010.0, 1500.0), edl, unit))
