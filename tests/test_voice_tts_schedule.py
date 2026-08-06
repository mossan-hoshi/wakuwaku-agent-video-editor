"""読み上げクリップの並べ方 — **絶対に重ならない／間は固定**の不変条件。

⚠️ このテストは「相槌だけ相手の声に重ねる」という**却下済みの設計**へ戻らないための番人。
実走で、相槌を重ねる実装を入れた結果「台詞がかぶってゴチャゴチャ」になり、
ユーザーから何度も同じ指摘を受けた。重なりは配置で解く問題ではなく、
**台本の時点でターン（タイミング）を加味して書くこと**で起きなくする。
"""
import random

from wwedit.publish.voice_tts import CLIP_GAP, schedule_clips


def _spans(got):
    return [(s, s + d) for s, d, _k in got]


def test_nothing_ever_overlaps():
    items = [(0.0, 10.0, "a1"), (2.0, 1.0, "b1"), (2.5, 4.0, "a2"), (2.6, 0.5, "b2")]
    sp = sorted(_spans(schedule_clips(items)))
    assert all(a[1] <= b[0] + 1e-9 for a, b in zip(sp, sp[1:], strict=False))


def test_the_gap_is_fixed_not_a_minimum():
    """元の会話に長い沈黙があっても、間は 0.15 秒に詰める。"""
    items = [(0.0, 1.0, "a"), (60.0, 1.0, "b"), (61.0, 1.0, "c")]
    got = dict((k, s) for s, _d, k in schedule_clips(items))
    assert got["a"] == 0.0
    assert got["b"] == 1.0 + CLIP_GAP
    assert got["c"] == 1.0 + CLIP_GAP + 1.0 + CLIP_GAP


def test_system_audio_waits_exactly_as_long_as_it_sounds():
    """PCシステム音が鳴っている**その長さぶんだけ**待つ。

    ⚠️ 「hold に掛かったら元の間隔をまるごと残す」ではない。実測（#103）では
    長い間 147.0秒のうち実際に鳴っていたのは 16.8秒だけで、残りはただの沈黙だった。
    沈黙のぶんは映像を速くして詰める（音声側に穴を残さない）。
    """
    items = [(0.0, 1.0, "a"), (30.0, 1.0, "b")]
    got = dict((k, s) for s, _d, k in schedule_clips(
        items, hold_spans=[(5.0, 25.0)], src_ends={"a": 1.0, "b": 31.0}))
    assert got["b"] == 1.0 + CLIP_GAP + 20.0      # 鳴っていた20秒だけ足す


def test_silence_that_only_touches_system_audio_is_still_squeezed():
    """間の端が hold にかすっただけなら、かすった分しか残さない。"""
    items = [(0.0, 1.0, "a"), (30.0, 1.0, "b")]
    got = dict((k, s) for s, _d, k in schedule_clips(
        items, hold_spans=[(0.5, 2.0)], src_ends={"a": 1.0, "b": 31.0}))
    assert abs(got["b"] - (1.0 + CLIP_GAP + 1.0)) < 1e-9


def test_system_audio_elsewhere_does_not_block_squeezing():
    items = [(0.0, 1.0, "a"), (30.0, 1.0, "b")]
    got = dict((k, s) for s, _d, k in schedule_clips(
        items, hold_spans=[(100.0, 120.0)], src_ends={"a": 1.0, "b": 31.0}))
    assert got["b"] == 1.0 + CLIP_GAP


def test_reading_longer_than_the_slot_still_never_overlaps():
    """読み上げが元の枠より長くても後ろへ送るだけ（重ねない）。"""
    items = [(0.0, 30.0, "a"), (1.0, 2.0, "b")]
    got = dict((k, s) for s, _d, k in schedule_clips(items))
    assert got["b"] == 30.0 + CLIP_GAP


def test_random_inputs_never_overlap():
    rnd = random.Random(0)
    for _ in range(50):
        items = [(rnd.uniform(0, 100), rnd.uniform(0.2, 8.0), i) for i in range(40)]
        holds = [(rnd.uniform(0, 90), rnd.uniform(0, 90)) for _ in range(3)]
        holds = [(min(a, b), max(a, b)) for a, b in holds]
        sp = sorted(_spans(schedule_clips(items, hold_spans=holds)))
        assert all(a[1] <= b[0] + 1e-9 for a, b in zip(sp, sp[1:], strict=False))


def test_schedule_clips_has_no_overlay_knob():
    """重ねる引数を復活させない（却下済みの設計）。"""
    import inspect
    names = set(inspect.signature(schedule_clips).parameters)
    assert "overlay" not in names
    assert "speaker_of" not in names
