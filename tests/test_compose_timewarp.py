"""[S2] 読み上げ主・映像従の時間ワープ（compose/timewarp.py）のテスト。"""

from __future__ import annotations

import pytest

from wwedit.compose.timewarp import (
    Warp,
    WarpSeg,
    anchors_from_report,
    build_warp,
)
from wwedit.edl.schema import TimeRange

R100 = [TimeRange(start=0.0, end=100.0)]


def _rates(w: Warp) -> list[float]:
    return [round(s.rate, 3) for s in w.segs]


def test_warp_seg_rate_and_freeze():
    assert WarpSeg(0.0, 10.0, 5.0).rate == pytest.approx(2.0)
    assert WarpSeg(10.0, 10.0, 2.0).rate == 0.0        # フリーズ
    assert WarpSeg(0.0, 4.0, 4.0).src_dur == pytest.approx(4.0)


def test_src_to_out_and_back_are_inverse():
    w = Warp([WarpSeg(0.0, 10.0, 5.0), WarpSeg(10.0, 12.0, 2.0), WarpSeg(12.0, 92.0, 1.0)])
    assert w.out_total == pytest.approx(8.0)
    assert w.src_total == pytest.approx(92.0)
    for t in (0.0, 2.5, 5.0, 10.0, 11.0, 12.0, 52.0, 92.0):
        assert w.out_to_src(w.src_to_out(t)) == pytest.approx(t, abs=1e-6)


def test_src_to_out_is_linear_inside_a_segment():
    w = Warp([WarpSeg(0.0, 10.0, 5.0)])          # 2倍速
    assert w.src_to_out(0.0) == pytest.approx(0.0)
    assert w.src_to_out(5.0) == pytest.approx(2.5)
    assert w.src_to_out(10.0) == pytest.approx(5.0)
    assert w.src_to_out(999.0) == pytest.approx(5.0)   # 範囲外は端へクランプ


def test_out_total_equals_reading_plus_uniform_gaps():
    """出力尺 = 読み上げの合計 ＋ 間×本数。間は必ず target で一定になる。"""
    anchors = [(10.0, 25.0, 5.0), (30.0, 40.0, 4.0), (60.0, 70.0, 3.0)]
    w = build_warp(anchors, R100, total=100.0, target_gap=0.3)
    speech = sum(s.out_dur for s in w.segs if s.kind == "speech")
    assert speech == pytest.approx(12.0)              # 5+4+3
    gaps = [s.out_dur for s in w.segs if s.kind == "gap"]
    assert gaps[:3] == pytest.approx([0.3, 0.3, 0.3])  # 先頭・発話間はすべて target


def test_leftover_is_pushed_into_the_gap_at_a_rate_below_the_cap():
    """余りが間だけで収まるなら、**間がちょうど target になる倍率**を使う（8倍固定にしない）。"""
    # 読み上げ5秒のあいだに素材8秒ぶん進みたい → 余り3秒を 0.5秒の間へ ＝ 6倍
    w = build_warp([(0.0, 5.0, 5.0), (8.0, 9.0, 1.0)], R100, total=100.0,
                   target_gap=0.5, speech_max_rate=8.0)
    sp = [s for s in w.segs if s.kind == "speech"]
    assert all(s.rate == pytest.approx(1.0) for s in sp[:1])   # 発話は等速のまま
    gap = [s for s in w.segs if s.kind == "gap"][0]
    assert gap.out_dur == pytest.approx(0.5)                   # 間は target ちょうど
    assert gap.rate == pytest.approx(6.0)                      # 上限8倍より遅い


def test_only_the_tail_of_the_speech_speeds_up_when_the_gap_is_not_enough():
    """上限倍率でも間に収まらないときだけ**発話の末尾**を速くする。頭は等速のまま。"""
    # 読み上げ5秒＋間0.5秒で素材30秒ぶん進みたい。8倍の間で食えるのは4秒だけ
    # → 残り21秒ぶんを末尾 x 秒で食う: x = 21/(8-1) = 3.0秒
    w = build_warp([(0.0, 5.0, 5.0), (30.0, 31.0, 1.0)], R100, total=100.0,
                   target_gap=0.5, speech_max_rate=8.0)
    sp = [s for s in w.segs if s.kind == "speech"]
    assert (sp[0].rate, sp[0].out_dur) == pytest.approx((1.0, 2.0))   # 頭は等速
    assert (sp[1].rate, sp[1].out_dur) == pytest.approx((8.0, 3.0))   # 末尾だけ速い
    gap = [s for s in w.segs if s.kind == "gap"][0]
    assert (gap.rate, gap.out_dur) == pytest.approx((8.0, 0.5))
    assert sp[1].src_end == pytest.approx(26.0)      # 2 + 24 まで進み、間で 30 へ


def test_gap_catches_up_the_delay_with_a_high_rate():
    """遅れは「間」で取り戻す（間は誰も見ていないので倍率を上げてよい）。"""
    w = build_warp([(0.0, 30.0, 5.0), (40.0, 45.0, 5.0)], R100, total=100.0,
                   target_gap=0.5, speech_max_rate=3.0, gap_max_rate=80.0)
    gap = [s for s in w.segs if s.kind == "gap"][0]    # 2つの発話のあいだ
    assert gap.src_start == pytest.approx(15.0)        # 遅れた地点から
    assert gap.src_end == pytest.approx(40.0)          # 次の発話頭まで追いつく
    assert gap.out_dur == pytest.approx(0.5)           # 間の長さは target のまま
    assert gap.rate == pytest.approx(50.0)


def test_gap_goes_above_the_cap_only_when_the_whole_speech_is_not_enough():
    """発話まるごと上限倍率でも足りないときだけ、間の倍率を上げる（無音なので上げてよい）。"""
    w = build_warp([(0.0, 1.0, 1.0), (90.0, 95.0, 5.0)], R100, total=100.0,
                   target_gap=0.5, speech_max_rate=8.0, gap_max_rate=80.0)
    sp = [s for s in w.segs if s.kind == "speech"][0]
    assert sp.rate == pytest.approx(8.0)               # 発話まるごと上限倍率
    gap = [s for s in w.segs if s.kind == "gap"][0]
    assert gap.rate > 8.0                              # それでも足りず間で追いつく
    assert gap.out_dur == pytest.approx(0.5)           # 間の長さは target のまま


def test_reading_longer_than_the_footage_freezes_instead_of_running_ahead():
    """読み上げが元発話より長いときは**フリーズ**。スローにも早送りにもしない。

    等速で先へ流すと**次の発話の映像を先に消費**してしまい、映像だけが話の内容より
    先に進み続ける。素材は ``次の発話の頭`` を超えない。
    """
    # 素材は 0..5秒しか無い（次の発話が5.0から）のに読み上げは10秒
    w = build_warp([(0.0, 2.0, 10.0), (5.0, 6.0, 1.0)], R100, total=100.0,
                   target_gap=0.0, lookahead=0.0)
    sp = [s for s in w.segs if s.kind == "speech"][0]
    assert sp.rate == pytest.approx(1.0)
    assert sp.src_end == pytest.approx(5.0)            # 次の発話の頭で止まる
    frz = [s for s in w.segs if s.kind == "freeze"]
    assert frz and frz[0].out_dur == pytest.approx(5.0)   # 残り5秒はフリーズ
    assert w.placements[0] == pytest.approx((0.0, 10.0))  # 読み上げ枠は10秒のまま


def test_hold_spans_force_normal_speed():
    """PC音声が鳴っている区間は倍率1.0固定（速くすると画面と音がずれ音程も変わる）。"""
    w = build_warp([(0.0, 1.0, 1.0), (50.0, 55.0, 5.0)], R100, total=100.0,
                   target_gap=0.5, hold_spans=[(20.0, 30.0)])
    held = [s for s in w.segs if s.kind == "hold"]
    assert len(held) == 1
    assert (held[0].src_start, held[0].src_end) == pytest.approx((20.0, 30.0))
    assert held[0].rate == pytest.approx(1.0)
    assert held[0].out_dur == pytest.approx(10.0)      # 等速なので尺がそのまま乗る


def test_hold_span_does_not_swallow_the_neighbouring_fast_parts():
    w = build_warp([(0.0, 1.0, 1.0), (50.0, 55.0, 5.0)], R100, total=100.0,
                   target_gap=0.5, hold_spans=[(20.0, 30.0)], gap_max_rate=80.0)
    kinds = [s.kind for s in w.segs]
    assert "hold" in kinds and kinds.count("gap") >= 2   # hold の前後に gap が残る


def test_tail_material_is_flushed_at_the_end():
    w = build_warp([(0.0, 5.0, 5.0)], R100, total=100.0, target_gap=0.0,
                   gap_max_rate=80.0)
    assert w.segs[-1].src_end == pytest.approx(100.0)
    assert w.out_total < 8.0                            # 残り95秒は高速で流す


def test_placements_give_the_out_position_of_each_reading():
    """読み上げの出力位置は Warp から取る（実尺を足して計算してはいけない）。"""
    anchors = [(10.0, 25.0, 5.0), (30.0, 40.0, 4.0)]
    w = build_warp(anchors, R100, total=100.0, target_gap=0.3)
    assert len(w.placements) == 2
    assert w.placements[0] == pytest.approx((0.3, 5.0))       # 先頭の間 0.3 の直後
    assert w.placements[1] == pytest.approx((5.6, 4.0))       # 0.3+5.0+0.3
    for (a, d), (b, _) in zip(w.placements, w.placements[1:], strict=False):
        assert a + d <= b + 1e-9                              # 読み上げは重ならない


def test_placement_is_longer_than_the_reading_when_pc_audio_intrudes():
    """PC音声で倍率1.0を強制された区間が発話に食い込むと、その枠は読み上げより長くなる。"""
    w = build_warp([(0.0, 30.0, 5.0)], R100, total=100.0, target_gap=0.0,
                   speech_max_rate=3.0, hold_spans=[(5.0, 20.0)])
    out_start, out_dur = w.placements[0]
    assert out_start == pytest.approx(0.0)
    assert out_dur > 5.0                    # hold の15秒が等速で乗るので伸びる


def test_fps_snaps_every_segment_to_whole_frames():
    """フレーム丸めはタイムラインを積む**前**に効かせる（後で丸めると積み上がってずれる）。"""
    anchors = [(3.3, 9.7, 2.03), (20.4, 26.1, 1.77)]
    w = build_warp(anchors, R100, total=100.0, target_gap=0.17, fps=25)
    for s in w.segs:
        assert abs(s.out_dur * 25 - round(s.out_dur * 25)) < 1e-9
    for a, _d in w.placements:
        assert abs(a * 25 - round(a * 25)) < 1e-6
    assert abs(w.out_total * 25 - round(w.out_total * 25)) < 1e-6


def test_anchors_from_report_maps_raw_seconds_into_kept_coordinates():
    ranges = [TimeRange(start=10.0, end=20.0), TimeRange(start=30.0, end=40.0)]
    rows = [
        {"src_start": 12.0, "src_end": 15.0, "tts_s": 2.0},
        {"src_start": 32.0, "src_end": 34.0, "tts_s": 1.5},
        {"src_start": 35.0, "src_end": 36.0, "tts_s": 0.0},   # 空は捨てる
    ]
    assert anchors_from_report(rows, ranges) == [(2.0, 5.0, 2.0), (12.0, 14.0, 1.5)]


def test_build_warp_is_monotonic_and_covers_the_whole_material():
    anchors = [(5.0, 12.0, 3.0), (20.0, 26.0, 2.0), (55.0, 70.0, 6.0)]
    w = build_warp(anchors, R100, total=100.0, target_gap=0.15,
                   hold_spans=[(30.0, 35.0)])
    prev = 0.0
    for s in w.segs:
        assert s.src_start == pytest.approx(prev)      # 隙間も重なりも作らない
        assert s.src_end >= s.src_start
        assert s.out_dur > 0
        prev = s.src_end
    assert prev == pytest.approx(100.0)
    assert all(r >= 1.0 - 1e-9 for r in _rates(w))


def test_lookahead_lets_the_footage_run_a_little_past_the_next_utterance():
    """少しの先行は繋いで見せる。フリーズを 0秒許容にすると本数が跳ね上がるため。"""
    anchors = [(0.0, 2.0, 10.0), (5.0, 6.0, 1.0)]
    strict = build_warp(anchors, R100, total=100.0, target_gap=0.0, lookahead=0.0)
    loose = build_warp(anchors, R100, total=100.0, target_gap=0.0, lookahead=5.0)
    assert [s.kind for s in strict.segs].count("freeze") == 1
    assert [s.kind for s in loose.segs].count("freeze") == 0   # 5秒までは繋ぐ
    assert loose.segs[0].src_end == pytest.approx(10.0)        # 次の頭+5秒まで進む
