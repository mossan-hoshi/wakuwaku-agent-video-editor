"""[V] 話者同一性チェック（publish/_speaker_sim.py）のテスト。

合成音が参照と別人になったら弾く。判定は numpy だけで完結させる
（Qwen3-TTS 側の venv からそのまま呼ぶため）。
"""

from __future__ import annotations

import numpy as np
import pytest

from wwedit.publish._speaker_sim import (
    F0_MAX_OCTAVE,
    SIM_THRESHOLD,
    compare_wave,
    cosine,
    embed,
    f0_median,
)

SR = 16000


def _voice(f0: float, formants, sec: float = 1.0, sr: int = SR) -> np.ndarray:
    """声っぽい信号（基本周波数＋フォルマント帯を強調した倍音列）。"""
    t = np.arange(int(sr * sec)) / sr
    x = np.zeros_like(t)
    for k in range(1, 40):
        f = f0 * k
        if f >= sr / 2:
            break
        gain = sum(np.exp(-((f - c) ** 2) / (2 * 120.0 ** 2)) for c in formants)
        x += (gain + 0.02) * np.sin(2 * np.pi * f * t) / k
    return (x / (np.abs(x).max() or 1.0)).astype(np.float32)


def test_embed_is_unit_length_and_ignores_loudness():
    a = _voice(200.0, (700, 1200, 2600))
    ea, eb = embed(a, SR), embed(a * 0.2, SR)
    assert ea is not None
    assert np.linalg.norm(ea) == pytest.approx(1.0, abs=1e-5)
    assert cosine(ea, eb) > 0.999            # 音量差では話者判定が動かない


def test_embed_returns_none_for_too_short_input():
    assert embed(np.zeros(100, dtype=np.float32), SR) is None


def test_same_voice_scores_higher_than_a_different_one():
    ref = _voice(200.0, (700, 1200, 2600))
    same = _voice(205.0, (710, 1210, 2620))
    other = _voice(120.0, (400, 900, 2200))
    e = embed(ref, SR)
    assert cosine(e, embed(same, SR)) > cosine(e, embed(other, SR))


def test_f0_median_tracks_the_fundamental():
    assert f0_median(_voice(180.0, (700, 1200)), SR) == pytest.approx(180.0, rel=0.06)


def test_compare_wave_rejects_an_octave_jump_even_if_timbre_is_close():
    """音色が近くても**1オクターブ下**なら別人（実測の最悪例が F0 115Hz vs 222Hz）。"""
    ref = _voice(220.0, (700, 1200, 2600))
    low = _voice(110.0, (700, 1200, 2600))
    got = compare_wave(low, SR, [(embed(ref, SR), f0_median(ref, SR))])
    assert got["octave"] > F0_MAX_OCTAVE
    assert got["ok"] is False


def test_compare_wave_accepts_the_same_speaker():
    ref = _voice(220.0, (700, 1200, 2600))
    got = compare_wave(_voice(224.0, (705, 1205, 2610)), SR,
                       [(embed(ref, SR), f0_median(ref, SR))])
    assert got["sim"] > SIM_THRESHOLD
    assert got["ok"] is True


def test_window_check_catches_a_clip_that_starts_as_someone_else():
    """**途中だけ別人**は全体平均に埋もれる。窓ごとに見て最悪の窓で落とすこと。

    実測: #103 の idx=142 は前半だけ別人なのに全体 0.9533 で合格していた
    （最悪窓は 0.00秒で 0.731）。
    """
    ref = _voice(220.0, (700, 1200, 2600))
    refs = [(embed(ref, SR), f0_median(ref, SR))]
    bad = _voice(230.0, (400, 900, 2100), sec=2.0)      # 出だしだけ声色が違う
    good = _voice(224.0, (705, 1205, 2610), sec=6.0)
    got = compare_wave(np.concatenate([bad, good]), SR, refs)
    assert got["n_win"] > 2
    assert got["sim_min"] < got["sim"]                  # 平均では見えない
    assert got["sim_min_at"] < 2.0                      # 崩れているのは出だし
    assert got["ok"] is False


def test_window_check_leaves_a_clean_clip_alone():
    ref = _voice(220.0, (700, 1200, 2600))
    got = compare_wave(_voice(224.0, (705, 1205, 2610), sec=6.0), SR,
                       [(embed(ref, SR), f0_median(ref, SR))])
    assert got["n_win"] > 2
    assert got["ok"] is True


def test_compare_wave_skips_clips_that_are_too_short_to_judge():
    """短い相槌は材料不足。判定せず通す（延々と引き直しになるのを防ぐ）。"""
    ref = _voice(220.0, (700, 1200, 2600))
    got = compare_wave(_voice(120.0, (400, 900), sec=0.2), SR,
                       [(embed(ref, SR), f0_median(ref, SR))])
    assert got["skipped"] is True
    assert got["ok"] is True
