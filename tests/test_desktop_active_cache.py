"""PC音声の「鳴っている区間」キャッシュは、**中身が変わったら測り直す**。

ワープ済みPC音声は計画が変わるたび同じ名前で作り直される。パス名だけをキーにすると
古い測定が残り、`--bgm-avoid-desktop` が効かなくなる。2026-08-07 に実害が出た:
書き込み途中の wav を測った「0件」が居座り、方式Bだけデモ音源にBGMが被った。
"""

from __future__ import annotations

from wwedit.compose.cli import _audio_fingerprint


def test_the_same_file_keeps_its_measurement(tmp_path):
    f = tmp_path / "warp.wav"
    f.write_bytes(b"x" * 100)
    assert _audio_fingerprint(str(f)) == _audio_fingerprint(str(f))


def test_rewriting_the_file_invalidates_it(tmp_path):
    """**これが本命**。同じ名前で焼き直したら別のキーになる。"""
    f = tmp_path / "warp.wav"
    f.write_bytes(b"x" * 100)
    before = _audio_fingerprint(str(f))
    f.write_bytes(b"y" * 250)                 # 焼き直し（サイズが変わる）
    assert _audio_fingerprint(str(f)) != before


def test_a_half_written_file_and_the_finished_one_are_different(tmp_path):
    """書き込み途中を測ってしまっても、完成後は測り直される。"""
    f = tmp_path / "warp.wav"
    f.write_bytes(b"")                        # 0バイト＝レンダ開始直後
    half = _audio_fingerprint(str(f))
    f.write_bytes(b"z" * 4096)
    assert _audio_fingerprint(str(f)) != half


def test_a_missing_file_falls_back_to_the_path(tmp_path):
    assert _audio_fingerprint(str(tmp_path / "nope.wav")) == str(tmp_path / "nope.wav")
