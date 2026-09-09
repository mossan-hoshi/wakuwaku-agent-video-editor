"""全長トラックの組み立ては**分けて足す**（`-i` を並べすぎない）。

`amix=inputs=N` に365本を一度に渡すとデコーダがN個同時に開いて極端に遅くなる。
実測(2026-08-07): **12分**。ワープ映像/音声で踏んだのと同じ形。
クリップは直列化済みで重ならないので、分けて足しても結果は同じ。
"""

from __future__ import annotations

from wwedit.publish import voice_convert
from wwedit.publish.voice_convert import ASSEMBLE_BATCH, assemble_track


def _placements(n: int):
    return [(float(i) * 2.0, f"c{i}.wav", 1.0) for i in range(n)]


def _mk(monkeypatch, tmp_path):
    got: list[list[str]] = []

    def fake(cmd, what):
        got.append(cmd)
        out = cmd[-1]
        open(out, "wb").write(b"x")     # 中間ファイルを実在させる

    monkeypatch.setattr(voice_convert, "_run_ffmpeg", fake)
    monkeypatch.setattr(voice_convert, "normalize_voice_wav", lambda *a, **k: None)
    return got


def _inputs(cmd):
    return sum(1 for a in cmd if a == "-i")


def test_a_small_track_is_one_call(monkeypatch, tmp_path):
    got = _mk(monkeypatch, tmp_path)
    assemble_track(_placements(ASSEMBLE_BATCH), 100.0, tmp_path / "o.wav")
    assert len(got) == 1


def test_a_big_track_is_split_then_summed(monkeypatch, tmp_path):
    """**これが本命**。365本を1発に渡さない。"""
    got = _mk(monkeypatch, tmp_path)
    assemble_track(_placements(365), 1900.0, tmp_path / "o.wav")
    assert len(got) == 8, "60本ずつ7バッチ（365=60*6+5）＋合算の8回"
    assert max(_inputs(c) for c in got) <= ASSEMBLE_BATCH


def test_the_sum_covers_every_clip(monkeypatch, tmp_path):
    got = _mk(monkeypatch, tmp_path)
    assemble_track(_placements(365), 1900.0, tmp_path / "o.wav")
    parts = [c for c in got[:-1]]
    assert sum(_inputs(c) for c in parts) == 365, "取りこぼしたクリップがある"


def test_the_final_mix_keeps_the_full_length(monkeypatch, tmp_path):
    got = _mk(monkeypatch, tmp_path)
    assemble_track(_placements(365), 1900.0, tmp_path / "o.wav")
    assert "atrim=0:1900.000" in " ".join(got[-1])
