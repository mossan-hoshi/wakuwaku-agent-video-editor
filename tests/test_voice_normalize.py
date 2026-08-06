"""[V] 合成声（Seed-VC / Qwen3-TTS）を収録音と同じ基準へ正規化する。

ユーザー指摘（2026-08-06）「BGMがいつもよりうるさい気がする」→「seed-VCやTTS後の音量の
方が小さいのかな？ これらの音ってノーマライズしてる？」。していなかった。実測:

    tts    mossan  -16.00 LUFS / TP -0.76dB      seedvc mossan  -16.39 / TP +0.38（クリップ）
    tts    tanig   -18.64 LUFS / TP -0.83dB      seedvc tanig   -18.81 / TP +0.25（クリップ）

話者間で 2.6dB ばらつき、Seed-VC は 0dBFS を超えていた。目標値は**収録音の整音と同じ**
（compose の ``LOUDNORM``）。
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

from wwedit.publish.voice_convert import (
    VOICE_LRA,
    VOICE_LUFS,
    VOICE_TP_DB,
    loudnorm_filter,
    measure_loudness,
    normalize_voice_wav,
)

MEASURED = {"input_i": -18.64, "input_tp": -0.83, "input_lra": 11.3,
            "input_thresh": -29.16, "target_offset": -0.2}


def test_targets_match_the_recording_normalization():
    """「通常の収録音ノーマライズと同等」= compose の LOUDNORM と同じ値。"""
    from wwedit.compose.ffmpeg_compose import LOUDNORM

    assert f"I={VOICE_LUFS:g}" in LOUDNORM
    assert f"TP={VOICE_TP_DB:g}" in LOUDNORM
    assert f"LRA={VOICE_LRA:g}" in LOUDNORM


def test_the_filter_targets_the_voice_level():
    f = loudnorm_filter(MEASURED)
    assert f.startswith(f"loudnorm=I={VOICE_LUFS:g}:TP={VOICE_TP_DB:g}:LRA={VOICE_LRA:g}")


def test_the_measured_values_are_passed_to_the_second_pass():
    """実測値を渡さないと loudnorm は1パスの推定になり、狙った値へ落ちない。"""
    f = loudnorm_filter(MEASURED)
    for key, val in [("measured_I", "-18.64"), ("measured_TP", "-0.83"),
                     ("measured_LRA", "11.30"), ("measured_thresh", "-29.16")]:
        assert f"{key}={val}" in f


def test_linear_mode_is_requested():
    """可能なら一定ゲインで済ませる（抑揚を触らない）。無理な時だけ ffmpeg が動的へ落ちる。"""
    assert "linear=true" in loudnorm_filter(MEASURED)


def test_the_filter_has_no_time_stretching():
    """音声は絶対に加工しない＝速度・ピッチには一切触れない。"""
    f = loudnorm_filter(MEASURED)
    for forbidden in ("atempo", "asetrate", "rubberband", "atrim"):
        assert forbidden not in f


# ---- 実際に ffmpeg を通す（合成音が本当に目標へ乗るか）----

pytestmark_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg が無い")


def _tone(path, *, db: float, seconds: float = 6.0) -> None:
    """一定音量のトーン＋無音（合成声トラックのように無音が混ざる形）。"""
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"sine=frequency=220:duration={seconds}",
         "-af", f"volume={db}dB,apad=pad_dur={seconds}",
         "-ar", "48000", "-ac", "1", str(path)],
        check=True, capture_output=True)


@pytestmark_ffmpeg
@pytest.mark.parametrize("db", [-30.0, -12.0, -3.0])
def test_tracks_of_any_level_land_on_the_target(tmp_path, db):
    wav = tmp_path / f"v{db}.wav"
    _tone(wav, db=db)
    res = normalize_voice_wav(wav)
    assert abs(res["after"] - VOICE_LUFS) < 1.0
    assert res["tp"] <= VOICE_TP_DB + 0.5


@pytestmark_ffmpeg
def test_two_different_levels_end_up_matching(tmp_path):
    """話者間のばらつき（実測 2.64dB）が消えるのがこの修正の目的。"""
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    _tone(a, db=-6.0)
    _tone(b, db=-20.0)
    ra, rb = normalize_voice_wav(a), normalize_voice_wav(b)
    assert abs(ra["before"] - rb["before"]) > 10.0     # 元は大きく違う
    assert abs(ra["after"] - rb["after"]) < 1.0        # 揃う


@pytestmark_ffmpeg
def test_a_clipping_track_is_brought_under_the_peak_ceiling(tmp_path):
    """Seed-VC 実測は TP +0.38dB でクリップしていた。0dBFS 下へ収める。"""
    wav = tmp_path / "hot.wav"
    _tone(wav, db=0.0)
    res = normalize_voice_wav(wav)
    assert res["tp"] <= VOICE_TP_DB + 0.5


@pytestmark_ffmpeg
def test_running_it_twice_does_not_drift(tmp_path):
    """作り直しで何度通しても劣化しない（正規化は冪等であってほしい）。"""
    wav = tmp_path / "x.wav"
    _tone(wav, db=-24.0)
    first = normalize_voice_wav(wav)["after"]
    second = normalize_voice_wav(wav)["after"]
    assert abs(first - second) < 0.5


@pytestmark_ffmpeg
def test_silence_is_left_alone(tmp_path):
    """無音トラック（同話者2本目のマイク）は触らない。"""
    wav = tmp_path / "sil.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=3", str(wav)],
        check=True, capture_output=True)
    size_before = wav.stat().st_size
    res = normalize_voice_wav(wav)
    assert res["before"] == res["after"]
    assert wav.stat().st_size == size_before


@pytestmark_ffmpeg
def test_measure_reports_every_key_the_second_pass_needs(tmp_path):
    wav = tmp_path / "m.wav"
    _tone(wav, db=-18.0)
    m = measure_loudness(wav)
    assert loudnorm_filter(m)          # 欠けていれば KeyError で落ちる
