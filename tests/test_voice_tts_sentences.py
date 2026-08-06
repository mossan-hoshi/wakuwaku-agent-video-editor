"""合成の単位は**文** — 分割・順序・素材区間の按分。

ターン丸ごとを1本の wav にすると、3文入りの行で「1文目だけ別人の声」になっても
クリップ平均に薄まって検出できず、直すときも丸ごと引き直しになる。感情・口パク・
字幕もターン単位でしか付けられない（2026-08-06 ユーザー指摘）。
**合成の単位＝後処理の単位**にするための不変条件をここで固定する。
"""
from __future__ import annotations

from wwedit.edl.schema import TimeRange
from wwedit.publish.cli import _split_turn_spans
from wwedit.publish.voice_tts import (
    SENT_MIN_CHARS,
    clip_name,
    schedule_clips,
    split_sentences,
    tts_clips,
)


def test_sentences_are_split_at_the_end_of_each_sentence():
    got = split_sentences("あー、はいはい。あ、そうですね、間の交換音ですよね。")
    assert got == ["あー、はいはい。", "あ、そうですね、間の交換音ですよね。"]


def test_a_too_short_fragment_is_glued_to_the_next_one():
    """「はい。」だけのクリップは材料が短すぎて声質が安定しない。"""
    got = split_sentences("はい。それで、さっきの話の続きなんですけど。")
    assert got == ["はい。それで、さっきの話の続きなんですけど。"]


def test_a_too_short_tail_is_glued_to_the_previous_one():
    got = split_sentences("さっきの話の続きなんですけどね。はい。")
    assert got == ["さっきの話の続きなんですけどね。はい。"]


def test_bang_question_stays_with_its_sentence():
    """「！？」の「？」は**前の文の末尾**に付く（次の文の頭ではない）。"""
    assert split_sentences("本当にそこまでやるんですか！？すごいですね、それは。") == [
        "本当にそこまでやるんですか！？", "すごいですね、それは。"]


def test_a_short_exclamation_is_never_a_clip_of_its_own():
    """短い「本当ですか！？」は次の文へ吸収される（記号だけの断片も独立しない）。"""
    assert split_sentences("本当ですか！？そこまでやるんですね。") == [
        "本当ですか！？そこまでやるんですね。"]


def test_a_decimal_point_is_not_a_sentence_end():
    """``.`` で割ると「リリア3.5」が壊れる。"""
    assert split_sentences("リリア3.5の話でしたよね。") == ["リリア3.5の話でしたよね。"]


def test_min_chars_counts_only_the_meat():
    """長さは記号を除いた中身で測る（句読点で水増ししない）。"""
    assert SENT_MIN_CHARS >= 2
    assert split_sentences("ええ、ええ。", min_chars=5) == ["ええ、ええ。"]


def test_a_single_sentence_turn_keeps_the_old_file_name():
    """文分割の前に合成済みのクリップを名前を変えずに再利用する。"""
    assert clip_name(17, 0, 1) == "u0017.wav"
    assert clip_name(17, 1, 3) == "u0017_01.wav"


def _units() -> list[dict]:
    return [
        {"uid": 0, "u_idx": 0, "speaker": "A", "start": 0.0, "end": 4.0, "text": "元の文"},
        {"uid": 1, "u_idx": 1, "speaker": "B", "start": 5.0, "end": 9.0, "text": "元の文2"},
        {"uid": 2, "u_idx": 2, "speaker": "A", "start": 10.0, "end": 14.0, "text": "元の文3"},
    ]


def test_clips_carry_a_unique_key_per_sentence():
    clips = tts_clips(_units(), {0: "こんばんは、始めましょうか。よろしくお願いします。",
                                 1: "", 2: "はい、よろしくお願いします。"})
    assert [c["key"] for c in clips] == ["0.0", "0.1", "2"]
    assert [c["wav"] for c in clips] == ["u0000_00.wav", "u0000_01.wav", "u0002.wav"]
    # 空文字のターンは読まない（スキルが隣へ文をまとめた）
    assert all(c["uid"] != 1 for c in clips)
    assert [c["sub"] for c in clips] == [0, 1, 0]
    assert [c["last"] for c in clips] == [False, True, True]


def test_a_turn_without_a_decision_falls_back_to_the_original_text():
    clips = tts_clips(_units(), {})
    assert [c["text"] for c in clips] == ["元の文", "元の文2", "元の文3"]


def test_sentences_of_one_turn_keep_their_order():
    """同じ希望位置のクリップは**入力順**のまま（尺で並べ替えない）。"""
    items = [(10.0, 5.0, "s0"), (10.0, 1.0, "s1"), (10.0, 3.0, "s2")]
    assert [k for _s, _d, k in schedule_clips(items)] == ["s0", "s1", "s2"]


def test_turn_spans_are_split_by_reading_length():
    """ターンの素材区間は、その中の文へ**読み上げ実尺の比**で配る。

    やらないと3文が全部同じ src 区間を指し、`timewarp` のアンカーが重なって
    映像の速度計画が壊れる。
    """
    ranges = [TimeRange(start=0.0, end=100.0)]
    clips = [{"uid": 1, "start": 10.0, "end": 20.0, "last": False},
             {"uid": 1, "start": 10.0, "end": 20.0, "last": True}]
    got = _split_turn_spans(clips, {0: 3.0, 1: 1.0}, ranges)
    assert got[0][0] == 10.0
    assert abs(got[0][1] - 17.5) < 1e-6
    assert abs(got[1][0] - 17.5) < 1e-6
    assert got[1][1] == 20.0      # 最後の文はターンの終端ちょうど


def test_turn_spans_are_contiguous_and_ordered():
    ranges = [TimeRange(start=0.0, end=100.0)]
    clips = [{"uid": 4, "start": 30.0, "end": 42.0, "last": i == 2} for i in range(3)]
    got = _split_turn_spans(clips, {0: 1.0, 1: 2.0, 2: 1.0}, ranges)
    assert got[0][0] == 30.0 and got[2][1] == 42.0
    assert abs(got[0][1] - got[1][0]) < 1e-6
    assert abs(got[1][1] - got[2][0]) < 1e-6
    assert got[0][1] < got[1][1] < got[2][1]


def test_long_sentences_are_reported():
    """60字を超える1文は合成前に警告する（スキルが見落としても気づけるように）。"""
    from wwedit.publish.voice_tts import SENT_MAX_CHARS, long_sentences

    clips = [{"key": "1", "text": "あ" * (SENT_MAX_CHARS + 1)},
             {"key": "2", "text": "あ" * SENT_MAX_CHARS}]
    assert [c["key"] for c in long_sentences(clips)] == ["1"]


def test_a_single_sentence_turn_keeps_the_whole_span():
    ranges = [TimeRange(start=0.0, end=100.0)]
    clips = [{"uid": 7, "start": 3.0, "end": 8.0, "last": True}]
    assert _split_turn_spans(clips, {0: 2.0}, ranges) == {0: (3.0, 8.0)}
