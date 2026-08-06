"""[S2] Warp を実素材へ適用する側（compose/warp_apply.py）のテスト。"""

from __future__ import annotations

import pytest

from wwedit.compose.timewarp import Warp, WarpSeg
from wwedit.compose.warp_apply import (
    build_warp_audio_script,
    build_warp_video_script,
    src_prime_pieces,
    warp_pieces,
)
from wwedit.edl.schema import Freeze, TimeRange

R = [TimeRange(start=10.0, end=20.0), TimeRange(start=30.0, end=45.0)]


def test_src_prime_pieces_maps_kept_ranges_to_raw_seconds():
    assert src_prime_pieces(R) == [
        (0.0, 10.0, 10.0, 20.0, False),
        (10.0, 25.0, 30.0, 45.0, False),
    ]


def test_src_prime_pieces_inserts_freeze_rows():
    frz = [Freeze(at=15.0, extra=2.0)]
    got = src_prime_pieces(R, frz)
    assert got[0] == (0.0, 5.0, 10.0, 15.0, False)
    assert got[1] == (5.0, 7.0, 15.0, 15.0, True)      # フリーズは raw が進まない
    assert got[2] == (7.0, 12.0, 15.0, 20.0, False)


def test_warp_pieces_splits_at_the_hole_between_kept_ranges():
    """Warp 区間が素材の穴（カットした所）をまたいだら割る。割らないと捨てた所を拾う。"""
    w = Warp([WarpSeg(0.0, 20.0, 4.0)])                # src' 0..20 を 4秒で（5倍速）
    got = warp_pieces(w, R)
    assert len(got) == 2
    assert got[0] == pytest.approx((10.0, 20.0, 2.0, False))   # 前半 keep
    assert got[1] == pytest.approx((30.0, 40.0, 2.0, False))   # 後半 keep（穴を飛ぶ）


def test_warp_pieces_keeps_the_total_output_duration():
    w = Warp([WarpSeg(0.0, 8.0, 2.0), WarpSeg(8.0, 25.0, 17.0)])
    assert sum(d for _, _, d, _ in warp_pieces(w, R)) == pytest.approx(19.0)


def test_warp_pieces_marks_freeze_segments():
    w = Warp([WarpSeg(0.0, 5.0, 5.0), WarpSeg(5.0, 5.0, 1.6)])
    got = warp_pieces(w, R)
    assert got[-1] == pytest.approx((15.0, 15.0, 1.6, True))


def test_warp_pieces_rounds_cumulatively_so_the_split_keeps_the_frame_count():
    """穴で割った小片を個別に丸めると端数が積み上がる（実走で+35フレームずれた）。"""
    w = Warp([WarpSeg(0.0, 20.0, 4.02)])           # 4.02s @25fps ＝ 100.5 → 100枚
    got = warp_pieces(w, R, fps=25)
    assert sum(round(d * 25) for _, _, d, _ in got) == round(4.02 * 25)


def test_video_script_selects_frames_instead_of_using_the_fps_filter():
    """``fps`` は区間ごとに枚数がずれて積み上がる。フレーム番号で間引く。"""
    s = build_warp_video_script([(10.0, 20.0, 5.0, False)], fps=25)
    assert "trim=start_frame=250:end_frame=500" in s    # 10..20秒 ＝ 250..500枚目
    assert "select='trunc((n+1)*125/250)-trunc(n*125/250)'" in s   # 250枚→125枚
    assert "setpts=N/25/TB" in s
    assert "fps=25" not in s
    assert "concat=n=1:v=1:a=0[vcat]" in s
    assert "[vcat]setpts=N/25/TB[outv]" in s            # 継ぎ目で落とさないため通しで振り直す
    assert "atempo" not in s                            # 映像スクリプトに音は出てこない


def test_video_script_pins_the_frame_count_of_every_piece():
    """``select`` は前後1枚ぶれるので、全区間に tpad→trim の保険を掛ける。"""
    s = build_warp_video_script([(10.0, 20.0, 5.0, False)], fps=25)
    assert "tpad=stop_mode=clone" in s
    assert "trim=end_frame=125" in s                    # 5.0秒 × 25fps


def test_video_script_has_no_select_when_speed_is_exactly_one():
    s = build_warp_video_script([(10.0, 20.0, 10.0, False)], fps=25)
    assert "select=" not in s
    assert "trim=start_frame=250:end_frame=500" in s
    assert "trim=end_frame=250" in s


def test_video_script_uses_tpad_for_freeze():
    s = build_warp_video_script([(15.0, 15.0, 2.0, True)], fps=25)
    assert "tpad=stop_mode=clone" in s
    assert "trim=end_frame=50" in s                     # 2.0秒 × 25fps


def test_video_script_clamps_pieces_that_point_past_the_end_of_the_material():
    """素材の外を指す片は**最後のフレーム**へ寄せる。

    寄せないと ``trim`` が0枚を返し、``tpad`` は複製元が無いので伸ばせず、その片が
    まるごと消える（実測: 末尾のフリーズ1片で-30フレーム＝-1.2秒）。
    """
    # 素材は 100 枚（0..99）。片は 105 枚目のフリーズを要求している
    s = build_warp_video_script([(4.2, 4.2, 1.2, True)], fps=25, src_frames=100)
    assert "trim=start_frame=99:end_frame=100" in s
    assert "trim=end_frame=30" in s                      # 1.2秒 × 25fps は保たれる


def test_video_script_keeps_pieces_inside_the_material_untouched():
    s = build_warp_video_script([(10.0, 20.0, 10.0, False)], fps=25, src_frames=1000)
    assert "trim=start_frame=250:end_frame=500" in s


def test_audio_script_cuts_instead_of_changing_tempo():
    """音は絶対に伸縮しない。無音を捨てて詰めるだけ（音程が変わらない）。"""
    pieces = [(10.0, 20.0, 2.0, False)]
    s = build_warp_audio_script(pieces)
    assert "atrim=start=10.0000:end=12.0000" in s       # 頭から出力尺ぶんだけ採る
    assert "atempo" not in s
    assert "concat=n=1:v=0:a=1[outa]" in s


def test_audio_script_pads_when_the_material_is_shorter_than_the_slot():
    s = build_warp_audio_script([(10.0, 10.5, 2.0, False)])
    assert "apad=whole_dur=2.0000" in s


def test_audio_script_emits_silence_for_freeze():
    s = build_warp_audio_script([(15.0, 15.0, 1.0, True)])
    assert "anullsrc" in s
