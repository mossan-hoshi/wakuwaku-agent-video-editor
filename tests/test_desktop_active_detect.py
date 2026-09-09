"""PC音声の「鳴っている区間」検出は、**鳴っているのが1%未満でも見つける**。

`--bgm-avoid-desktop` はこの結果でBGMを止める。取りこぼすと、聴かせたいデモ音源の上に
BGMが乗る（音楽生成AIの回で実害・2026-08-07）。ワープ後のPC音声は無音部分が
**完全な digital silence** になるので、上端を p99 で見ると無音を指してしまう。
"""

from __future__ import annotations

import numpy as np
import pytest

from wwedit.compose import speedup


@pytest.fixture
def fake_rms(monkeypatch):
    """`_rms_db` を差し替えて、任意のRMSプロファイルを食わせる。"""
    def use(db_values, hop=0.05):
        monkeypatch.setattr(speedup, "_rms_db",
                            lambda _p, **_k: (np.asarray(db_values, dtype=float), hop))
    return use


def _profile(total: int, loud_at: tuple[int, int], loud_db: float = -30.0):
    db = np.full(total, -120.0)
    db[loud_at[0]:loud_at[1]] = loud_db
    return db


def test_a_burst_under_one_percent_is_still_found(fake_rms):
    """**これが本命**。全体の0.5%だけ鳴っているトラック（p99 はまだ無音）。"""
    fake_rms(_profile(40_000, (10_000, 10_200)))     # 0.5%
    spans, info = speedup.desktop_active_spans("x.wav")
    assert info.get("reason") != "flat", "p99 で上端を見ると取りこぼす"
    assert spans, "鳴っている区間が見つからない"
    assert spans[0][0] == pytest.approx(500.0, abs=1.0)


def test_a_larger_burst_is_found_too(fake_rms):
    fake_rms(_profile(40_000, (10_000, 11_000)))     # 2.5%
    spans, info = speedup.desktop_active_spans("x.wav")
    assert info.get("reason") != "flat"
    assert len(spans) == 1


def test_a_flat_track_is_still_rejected(fake_rms):
    """ずっと一定ノイズのトラックは「鳴っていない」のまま（誤検出させない）。"""
    fake_rms(np.full(40_000, -55.0))
    spans, info = speedup.desktop_active_spans("x.wav")
    assert spans == [] and info["reason"] == "flat"


def test_pure_silence_is_rejected(fake_rms):
    fake_rms(np.full(40_000, -120.0))
    spans, info = speedup.desktop_active_spans("x.wav")
    assert spans == [] and info["reason"] == "flat"
