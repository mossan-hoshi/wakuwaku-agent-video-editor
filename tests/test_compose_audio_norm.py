"""[F] 整音フィルタ生成（30秒粒度窓ノーマライズ）のテスト。"""

from __future__ import annotations

import pytest

from wwedit.compose.ffmpeg_compose import DYNNORM, LOUDNORM, build_speaker_mix_filter


def test_single_track_windowed():
    f = build_speaker_mix_filter(1, windowed=True)
    assert DYNNORM in f  # 30秒粒度の窓ノーマライズが入る
    assert LOUDNORM in f  # 全体ラウドネス目標
    assert f.endswith("[outa]")
    assert "[0:a]" in f


def test_single_track_not_windowed_is_global_only():
    f = build_speaker_mix_filter(1, windowed=False)
    assert DYNNORM not in f  # 従来の一括のみ
    assert f == f"[0:a]{LOUDNORM}[outa]"


def test_two_tracks_windowed_per_track_then_mix():
    f = build_speaker_mix_filter(2, windowed=True)
    # 各トラックに dynaudnorm → d0/d1 → amix → loudnorm
    assert f.count(DYNNORM) == 2
    assert "[d0]" in f and "[d1]" in f
    assert "amix=inputs=2:normalize=0[mix]" in f
    assert f.endswith(f"[mix]{LOUDNORM}[outa]")


def test_two_tracks_not_windowed():
    f = build_speaker_mix_filter(2, windowed=False)
    assert DYNNORM not in f
    assert "[0:a][1:a]amix=inputs=2:normalize=0[mix]" in f


def test_zero_tracks_raises():
    with pytest.raises(ValueError):
        build_speaker_mix_filter(0)
