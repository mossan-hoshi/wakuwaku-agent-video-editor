"""アイキャッチの一言ボイス（キャラ/台詞のランダム選択と合成呼び出し）のテスト。"""

from __future__ import annotations

from wwedit.publish.character import FULL_NAME
from wwedit.publish.eyecatch_voice import (
    NOBETUBE_VOICES,
    VOICE_LINES,
    pick_line,
    pick_voice,
    synth_eyecatch_voice,
    synth_eyecatch_voices,
)


def test_every_voice_has_a_display_name() -> None:
    # 右上バッジに出す名前が引けないキャラを混ぜない
    assert all(v in FULL_NAME for v in NOBETUBE_VOICES)


def test_voice_lines_are_short_and_have_kana_reading() -> None:
    for disp, reading in VOICE_LINES:
        assert disp and reading
        assert len(disp) <= 10  # 一言（章冒頭に差し込む長さ）
        # 読みは かな書き（SBV2は漢字/英字を誤読する）
        assert not any("一" <= ch <= "鿿" for ch in reading)
        assert not any(ch.isascii() and ch.isalpha() for ch in reading)


def test_pick_is_deterministic_per_seed() -> None:
    assert pick_voice(3) == pick_voice(3)
    assert pick_line(3) == pick_line(3)


def test_pick_varies_across_chapters() -> None:
    # 章ごとに変化する（全章同じ声・同じ台詞にならない）
    assert len({pick_voice(i) for i in range(12)}) > 1
    assert len({pick_line(i)[0] for i in range(12)}) > 1


def test_pick_voice_stays_in_pool() -> None:
    assert all(pick_voice(i) in NOBETUBE_VOICES for i in range(30))


def test_synth_eyecatch_voice_passes_kana_reading_to_tts(tmp_path) -> None:
    got = {}

    def fake_synth(text, out, voice, **kw):
        got["text"], got["voice"] = text, voice
        return 1.1

    wav, char, disp, dur = synth_eyecatch_voice(
        tmp_path / "v.wav", seed=5, synth_fn=fake_synth
    )
    assert dur == 1.1
    assert char in NOBETUBE_VOICES
    assert got["voice"] == char
    # 合成には**読み**を、表示には正表記を使う
    expected_disp, expected_reading = pick_line(5)
    assert disp == expected_disp
    assert got["text"] == expected_reading
    assert wav == tmp_path / "v.wav"


def test_synth_eyecatch_voice_honours_explicit_char_and_line(tmp_path) -> None:
    def fake_synth(text, out, voice, **kw):
        return 0.8

    _wav, char, disp, _d = synth_eyecatch_voice(
        tmp_path / "v.wav", seed=0, char="yume", line=("よし！", "よし！"),
        synth_fn=fake_synth,
    )
    assert char == "yume" and disp == "よし！"


def test_synth_eyecatch_voices_batches_all_chapters_in_one_call(tmp_path) -> None:
    """**モデル読み込みは1回**＝全章ぶんを1回の合成呼び出しにまとめる。"""
    calls = []

    def fake_batch(jobs):
        calls.append(jobs)
        return [1.0] * len(jobs)

    made = synth_eyecatch_voices(
        {1: 11, 2: 12, 3: 13}, tmp_path, voices=["suzu", "noa"], batch_fn=fake_batch
    )
    assert len(calls) == 1  # 章ごとにプロセスを起こさない
    assert len(calls[0]) == 3
    assert sorted(made) == [1, 2, 3]
    for i, (wav, char, disp) in made.items():
        assert char in ("suzu", "noa")
        assert wav.parent == tmp_path
        # 合成に渡すのは読み、表示は正表記
        assert disp == pick_line({1: 11, 2: 12, 3: 13}[i])[0]
    texts = {j["text"] for j in calls[0]}
    assert texts <= {r for _d, r in VOICE_LINES}


def test_qwen_voices_all_have_display_names() -> None:
    from wwedit.publish.qwen_tts import QWEN_VOICES

    assert all(v in FULL_NAME for v in QWEN_VOICES)
    # のべつべ！のキャラだけ（実在の人の声クローンはアイキャッチに使わない）
    assert "mossan_hoshi" not in QWEN_VOICES
