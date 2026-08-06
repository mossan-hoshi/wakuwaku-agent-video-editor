"""[S] 発話の間を一定に揃える高速化（compose/speedup.py）の純関数テスト。"""

from __future__ import annotations

import pytest

from wwedit.compose.speedup import (
    _atempo_chain,
    auto_target_gap,
    blocked_spans_out,
    build_filter_script,
    effective_plan,
    eyecatch_inserts,
    frame_segments,
    invert_spans,
    limit_shrink_in,
    merge_spans,
    pad_spans,
    shift_chapter_lines,
    shift_plan_by_inserts,
    shifted_time,
    soft_regions_out,
    speech_blocks_out,
    speech_spans_out,
    speedup_plan,
    src_spans_to_out,
    uniform_gap_plan,
)
from wwedit.edl.schema import (
    Chapter,
    Edl,
    InfographicConfig,
    Segment,
    SourceMedia,
    Subtitle,
    TimeRange,
    Utterance,
    Word,
)


def _edl(**kw) -> Edl:
    return Edl(
        recording_dir="2026-08-03",
        source=SourceMedia(video_path="v.mp4", fps=30, duration_s=100.0),
        **kw,
    )


# --------------------------------------------------------------------------- 区間演算


def test_merge_spans_sorts_and_joins_overlaps():
    assert merge_spans([(5, 6), (0, 2), (1.5, 3)]) == [(0, 3), (5, 6)]


def test_merge_spans_gap_joins_near_neighbours_and_drops_empty():
    assert merge_spans([(0, 1), (1.3, 2)], gap=0.5) == [(0, 2)]
    assert merge_spans([(0, 1), (1.3, 2)]) == [(0, 1), (1.3, 2)]
    assert merge_spans([(1, 1), (2, 1)]) == []


def test_pad_spans_clamps_to_bounds():
    assert pad_spans([(1, 2), (10, 11)], 0.5, lo=0.0, hi=10.8) == [(0.5, 2.5), (9.5, 10.8)]
    # 広げた結果くっつくものは結合される
    assert pad_spans([(1, 2), (2.5, 3)], 0.5) == [(0.5, 3.5)]


def test_invert_spans_returns_gaps_including_head_and_tail():
    assert invert_spans([(2, 3), (5, 6)], 10.0) == [(0, 2), (3, 5), (6, 10)]
    assert invert_spans([(0, 10)], 10.0) == []
    assert invert_spans([], 10.0) == [(0, 10)]


def test_invert_spans_clips_busy_past_total():
    assert invert_spans([(8, 20)], 10.0) == [(0, 8)]


# --------------------------------------------------------------------------- 素材→出力


def test_src_spans_to_out_splits_at_cut_boundaries():
    ranges = [TimeRange(start=0, end=10), TimeRange(start=20, end=30)]
    # 素材 5..25 はカット(10..20)で分断され、出力では 5..10 と 10..15 が連続する
    assert src_spans_to_out([(5, 25)], ranges) == [(5.0, 15.0)]


def test_src_spans_to_out_drops_ranges_fully_cut():
    ranges = [TimeRange(start=0, end=10), TimeRange(start=20, end=30)]
    assert src_spans_to_out([(12, 18)], ranges) == []


# --------------------------------------------------------------------------- 発話ブロック


def test_speech_spans_out_prefers_voice_clips_over_word_timings():
    """方式Bは meta.voice.clips が正（元 word タイミングを使うと声と合わない）。"""
    edl = _edl(
        segments=[Segment(id="s0", start=0, end=100)],
        utterances=[Utterance(speaker="a", text="あ", start=0, end=5,
                              words=[Word(text="あ", start=0.0, end=5.0)])],
        meta={"voice": {"clips": [{"speaker": "a", "out_start": 40.0, "out_end": 42.0}]}},
    )
    assert speech_spans_out(edl, edl.kept_ranges()) == [(40.0, 42.0)]


def test_speech_spans_out_falls_back_to_word_spans_with_padding():
    edl = _edl(
        segments=[Segment(id="s0", start=0, end=100)],
        utterances=[Utterance(speaker="a", text="あいう", start=0, end=30,
                              words=[Word(text="あいう", start=1.0, end=30.0)])],
    )
    # word は隙間ゼロなので文字数上限(3×0.22)で打ち切られ、見積りなので前後に余白が付く
    assert speech_spans_out(edl, edl.kept_ranges()) == [
        (pytest.approx(0.75), pytest.approx(1.91))]


def test_speech_blocks_out_holds_a_subtitle_that_ends_just_after_the_voice():
    """発話が終わるまで字幕を出す＝すぐ後で終わる字幕はブロック終端を延ばす。"""
    edl = _edl(
        segments=[Segment(id="s0", start=0, end=100)],
        subtitles=[Subtitle(start=10, end=12.5, text="少しはみ出す")],
        meta={"voice": {"clips": [{"speaker": "a", "out_start": 10.0, "out_end": 12.0}]}},
    )
    assert speech_blocks_out(edl, edl.kept_ranges()) == [(10.0, 12.5)]


def test_speech_blocks_out_does_not_hold_a_long_summary_card():
    """方式Aの要約字幕（数十秒出っぱなし）でブロックを延ばすと全部が塞がる。"""
    edl = _edl(
        segments=[Segment(id="s0", start=0, end=100)],
        subtitles=[Subtitle(start=10, end=60, text="要約カード")],
        meta={"voice": {"clips": [{"speaker": "a", "out_start": 10.0, "out_end": 12.0}]}},
    )
    # 発話 10..12 はそのまま。字幕は「頭 2.5 秒は読ませる」ぶんだけ（10..12.5 と重なる）
    assert speech_blocks_out(edl, edl.kept_ranges()) == [(10.0, 12.5)]


def test_speech_blocks_out_guarantees_reading_time_for_every_subtitle():
    """発話と重ならない字幕でも、表示開始から min_read 秒は通常速度で流す。"""
    edl = _edl(
        segments=[Segment(id="s0", start=0, end=100)],
        subtitles=[Subtitle(start=40, end=90, text="無音中に出る要約")],
        meta={"voice": {"clips": [{"speaker": "a", "out_start": 10.0, "out_end": 12.0}]}},
    )
    assert speech_blocks_out(edl, edl.kept_ranges()) == [(10.0, 12.0), (40.0, 42.5)]


def test_blocked_spans_out_covers_desktop_audio_but_not_infographic():
    """図解は「速くできない区間」ではない（静止カードなので下の無音は詰めてよい）。"""
    edl = _edl(
        segments=[Segment(id="s0", start=0, end=100)],
        infographic=InfographicConfig(path="ig.png", start_s=0.0, duration_s=10.0,
                                      fade_s=0.0),
    )
    got = blocked_spans_out(edl, edl.kept_ranges(), desktop_src_spans=[(70.0, 90.0)],
                            desktop_pad=0.3)
    assert got == [(69.7, 90.3)]


def test_soft_regions_out_returns_the_infographic_window():
    edl = _edl(
        segments=[Segment(id="s0", start=0, end=100)],
        infographic=InfographicConfig(path="ig.png", start_s=0.0, duration_s=10.0,
                                      fade_s=0.0),
    )
    assert soft_regions_out(edl, edl.kept_ranges()) == [(0.0, 10.3)]
    assert soft_regions_out(_edl(segments=[Segment(id="s0", start=0, end=10)]),
                            [TimeRange(start=0, end=10)]) == []


def test_limit_shrink_in_truncates_instead_of_dropping():
    """図解の中では縮む量に上限を掛ける。区間を捨てず**短く切り詰める**。"""
    # 区間 10秒・上限20% = 2.0秒まで縮めてよい。8倍速は 1秒につき 0.875秒縮む
    plan = [(1.0, 5.0, 8.0)]                       # 4秒 ×8 → 3.5秒縮む（上限超過）
    got = limit_shrink_in(plan, [(0.0, 10.0)], ratio=0.20)
    assert len(got) == 1
    a, b, f = got[0]
    assert (a, f) == (1.0, 8.0)
    assert (b - a) * (1 - 1 / f) == pytest.approx(2.0)   # ちょうど上限まで


def test_limit_shrink_in_keeps_spans_outside_regions():
    plan = [(1.0, 2.0, 8.0), (50.0, 60.0, 8.0)]
    got = limit_shrink_in(plan, [(0.0, 10.0)], ratio=0.20)
    assert got == plan                              # 1秒ぶん(0.875秒)は上限2.0秒の内側


# --------------------------------------------------------------------------- 目標の間


def test_auto_target_gap_takes_the_median_of_short_gaps():
    # 0.2/0.2/0.2 の短いギャップと 5.0 の長いギャップ → 目標は 0.2
    blocks = [(0, 1), (1.2, 2), (2.2, 3), (3.2, 4), (9.0, 10.0)]
    assert auto_target_gap(blocks, 10.0, cutoff=1.0) == pytest.approx(0.2)


def test_auto_target_gap_clamps_and_falls_back():
    assert auto_target_gap([(0, 1), (1.02, 2)], 2.0) == 0.10          # 下限クランプ
    assert auto_target_gap([(0, 1), (5, 6)], 6.0, cutoff=0.5) == 0.30  # 短い間が無い


# --------------------------------------------------------------------------- 計画


def test_uniform_gap_plan_leaves_exactly_the_target_gap():
    """4秒の間 → 目標0.5秒。残る間がぴったり0.5秒になる長さだけ速くする。"""
    plan = uniform_gap_plan([(0, 1), (5, 6)], [], 6.0, target=0.5, factor=8.0)
    (a, b, f), = plan
    assert (a, f) == (1.0, 8.0)
    assert b - a == pytest.approx(3.5 * 8 / 7)          # x = (G-target)*f/(f-1)
    gap_after = (4.0 - (b - a)) + (b - a) / f
    assert gap_after == pytest.approx(0.5)              # ← これが目的


def test_uniform_gap_plan_raises_factor_when_8x_cannot_reach_target():
    """目標×倍率 を超える長い間は、8倍のままだと届かないので倍率を上げる。"""
    plan = uniform_gap_plan([(0, 1), (21, 22)], [], 22.0, target=0.5, factor=8.0)
    (a, b, f), = plan
    assert (a, b) == (1.0, 21.0)          # 間まるごと
    assert f == pytest.approx(40.0)       # 20 / 0.5
    assert (b - a) / f == pytest.approx(0.5)


def test_uniform_gap_plan_respects_max_factor():
    plan = uniform_gap_plan([(0, 1), (21, 22)], [], 22.0, target=0.5, factor=8.0,
                            max_factor=10.0)
    assert plan[0][2] == 10.0


def test_uniform_gap_plan_skips_gaps_already_tight():
    assert uniform_gap_plan([(0, 1), (1.2, 2)], [], 2.0, target=0.15,
                            min_gain=0.15) == []


def test_uniform_gap_plan_treats_blocked_regions_as_events():
    """PC音声の手前にも同じ間を残し、PC音声そのものは速くしない。"""
    plan = uniform_gap_plan([(0, 1)], [(10.0, 15.0)], 15.0, target=0.5, factor=8.0)
    (a, b, f), = plan
    assert (a, b) == (1.0, 10.0)           # PC音声の手前までで止まる（食い込まない）
    assert (b - a) / f == pytest.approx(0.5)   # PC音声の前にも同じ間が残る


def test_speedup_plan_reports_target_and_factors():
    edl = _edl(
        segments=[Segment(id="s0", start=0, end=100)],
        meta={"voice": {"clips": [
            {"speaker": "a", "out_start": 0.0, "out_end": 1.0},
            {"speaker": "b", "out_start": 1.2, "out_end": 2.0},   # 0.2秒＝連続とみなす
            {"speaker": "a", "out_start": 20.0, "out_end": 100.0},
        ]}},
    )
    plan, info = speedup_plan(edl, edl.kept_ranges())
    assert info["target_gap_s"] == 0.2 and info["auto_target"] is True
    assert info["n_spans"] == 1
    assert plan[0][0] == 2.0                       # 発話の終了直後から
    assert info["factor_max"] > 8.0                # 18秒の間は8倍では届かない


# --------------------------------------------------------------------------- 時刻補正


def test_shifted_time_compresses_only_past_spans():
    plan = [(10.0, 18.0, 8.0)]
    assert shifted_time(5.0, plan) == 5.0
    assert shifted_time(14.0, plan) == pytest.approx(10.5)   # 4秒→0.5秒
    assert shifted_time(20.0, plan) == pytest.approx(13.0)   # 8秒→1秒


def test_shifted_time_uses_each_spans_own_factor():
    plan = [(0.0, 8.0, 8.0), (10.0, 30.0, 20.0)]
    # 8秒/8倍=1秒 + 通常2秒(8..10) + 20秒/20倍=1秒
    assert shifted_time(30.0, plan) == pytest.approx(1.0 + 2.0 + 1.0)


def test_shift_chapter_lines_rewrites_timestamps_only():
    lines = ["00:00 オープニング", "1:00:20 まとめ", "見出しだけ"]
    out = shift_chapter_lines(lines, [(0.0, 80.0, 8.0)])
    assert out[0] == "00:00 オープニング"
    assert out[1] == "59:10 まとめ"       # 3620 - 70 = 3550
    assert out[2] == "見出しだけ"


# --------------------------------------------------------------------------- アイキャッチ


def test_eyecatch_inserts_skips_first_chapter():
    edl = _edl(
        segments=[Segment(id="s0", start=0, end=100)],
        chapters=[Chapter(start_at=0.0, chapter_title="A"),
                  Chapter(start_at=40.0, chapter_title="B")],
    )
    assert eyecatch_inserts(edl, edl.kept_ranges(), duration=2.0) == [(40.0, 2.0)]


def test_shift_plan_by_inserts_splits_span_at_insert_point():
    # 10..30 の区間の途中(20)に2秒のアイキャッチが入る → 2つに割れて後半が2秒ずれる
    assert shift_plan_by_inserts([(10.0, 30.0, 8.0)], [(20.0, 2.0)]) == [
        (10.0, 20.0, 8.0), (22.0, 32.0, 8.0)]


def test_shift_plan_by_inserts_shifts_span_after_insert():
    assert shift_plan_by_inserts([(30.0, 40.0, 8.0)], [(20.0, 2.0)]) == [(32.0, 42.0, 8.0)]
    assert shift_plan_by_inserts([(10.0, 15.0, 8.0)], [(20.0, 2.0)]) == [(10.0, 15.0, 8.0)]


def test_atempo_chain_decomposes_factor():
    assert _atempo_chain(8) == "atempo=2,atempo=2,atempo=2"
    assert _atempo_chain(3.0) == "atempo=2,atempo=1.5"
    assert _atempo_chain(1.0) == "anull"


# --------------------------------------------------------------------------- フレーム割


def test_frame_segments_snaps_inward_without_truncating_to_a_multiple():
    """倍率の倍数へ切り詰めると端数が通常速度で残り、間がばらつく（実際に踏んだ）。"""
    segs = frame_segments([(1.0, 2.0, 8.0)], 4.0, fps=25)
    assert segs == [(0, 25, 1), (25, 50, 8), (50, 100, 1)]   # 25枚まるごと高速化


def test_frame_segments_never_eats_into_speech():
    """境界は**内側**へ丸める（round だと直前の発話を半フレーム食う）。"""
    segs = frame_segments([(1.019, 2.0, 8.0)], 4.0, fps=25)
    assert segs[1][0] == 26          # ceil(25.475) = 26。25 に切り下げない


def test_frame_segments_drops_a_span_too_short_for_even_2x():
    """2フレーム未満は 2倍にも落とせないので諦める（1枚だけの高速化は作らない）。"""
    assert frame_segments([(1.0, 1.04, 8.0)], 4.0, fps=25) == [(0, 100, 1)]


def test_frame_segments_covers_the_whole_timeline_without_gaps():
    segs = frame_segments([(1.0, 2.0, 8.0), (3.0, 3.9, 8.0)], 4.0, fps=25)
    assert segs[0][0] == 0 and segs[-1][1] == 100
    assert all(b == segs[i + 1][0] for i, (a, b, _) in enumerate(segs[:-1]))


def test_frame_segments_keeps_per_span_factors():
    segs = frame_segments([(1.0, 2.0, 8.0), (2.5, 6.0, 20.0)], 8.0, fps=25)
    assert [fac for _, _, fac in segs if fac > 1] == [8, 20]


def test_frame_segments_lowers_the_factor_instead_of_dropping_a_short_span():
    """捨てるとその間だけ通常速度で残り「間を一定に」が崩れる → 倍率を落として残す。"""
    segs = frame_segments([(1.0, 1.32, 8.0)], 4.0, fps=25)
    assert segs == [(0, 25, 1), (25, 33, 4), (33, 100, 1)]   # 8枚 → 2枚出す4倍へ


def test_frame_segments_never_leaves_a_tiny_trailing_segment():
    """末尾に数フレームだけ残ると ffmpeg の trim/concat がデッドロックする。"""
    # 1.0..3.88 を8倍にすると末尾が3フレームしか残らない → 高速側を削って譲る
    segs = frame_segments([(1.0, 3.88, 8.0)], 4.0, fps=25)
    assert segs[-1][2] == 1 and segs[-1][1] - segs[-1][0] >= 12


def test_effective_plan_reports_the_real_factor_not_the_integer_one():
    """実効倍率は 長さ/出力枚数。整数倍率のままだと章時刻が出力とずれる。"""
    # 25枚を8倍で間引くと ceil(25/8)=4枚 → 実効 6.25倍
    assert effective_plan([(1.0, 2.0, 8.0)], 4.0, fps=25) == [(1.0, 2.0, 6.25)]


def test_seg_out_frames_matches_the_select_expression():
    from wwedit.compose.speedup import seg_out_frames

    assert seg_out_frames(48, 8) == 6        # 0,8,16,24,32,40
    assert seg_out_frames(25, 8) == 4        # 0,8,16,24
    assert seg_out_frames(264, 33) == 8


# --------------------------------------------------------------------------- filtergraph


def test_build_filter_script_pads_with_a_finite_whole_dur():
    """引数なしの apad は無限に無音を作って ffmpeg がハングする（実際に固まった）。"""
    script = build_filter_script([(0, 25, 1), (25, 49, 8)], fps=25)
    assert "apad=whole_dur=" in script
    assert "apad," not in script


def test_build_filter_script_decimates_with_select_not_fps_filter():
    """fps フィルタだと区間ごとに複製フレームが1枚乗って音とずれる。"""
    script = build_filter_script([(0, 25, 1), (25, 49, 8)], fps=25)
    assert "select='not(n-trunc(n/8)*8)'" in script
    assert "fps=" not in script          # fps フィルタは使わない
    assert "setpts=N/25/TB" in script


def test_build_filter_script_makes_audio_slightly_shorter_than_video():
    """concat は長い方へ揃えるので、音が長いと映像フレームが複製される。"""
    script = build_filter_script([(0, 25, 1)], fps=25)
    assert "atrim=0:0.999900" in script   # 25フレーム=1.000秒 より 0.1ms 短い


def test_build_filter_script_sizes_audio_from_the_actual_selected_frames():
    """音の目標長は ceil(枚数/倍率) から出す（長さ/倍率だと映像とずれる）。"""
    script = build_filter_script([(0, 25, 8)], fps=25)
    assert "atrim=0:0.159900" in script    # ceil(25/8)=4枚=0.16秒


def test_build_filter_script_concats_every_segment():
    script = build_filter_script([(0, 25, 1), (25, 49, 8), (49, 100, 1)], fps=25)
    assert "concat=n=3:v=1:a=1[outv][outa]" in script
