"""チャプターリボンは**1入力・1段の overlay**で重ねる（章の数だけ積まない）。

2026-08-07 実測: 10章の動画で `-loop 1 -i rib_NN.png` を**10入力**、
`overlay ... enable='between(t,..)'` を**10段**直列にしていた。同時に見えるのは
常に1枚で、区間は隙間なく連続しているのに、1920x1080 の RGBA 全画面合成が
毎フレーム10回走る。本体合成はフィルタ律速（CPU 94%）なので、ここが直接効く。

ちびキャラと同じ ffconcat（PNG プレイリスト）方式にすれば 1入力・1段で済む。
"""

from __future__ import annotations

from pathlib import Path

from wwedit.compose.ffmpeg_compose import _blank_png, _ffconcat_text


def test_each_png_gets_its_duration(tmp_path):
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    a.write_bytes(b"x")
    b.write_bytes(b"y")
    t = _ffconcat_text([(a, 12.5), (b, 3.25)])
    assert "duration 12.50000" in t
    assert "duration 3.25000" in t


def test_the_last_entry_is_repeated(tmp_path):
    """**これが本命**。concat demuxer は最後の duration を無視するので重複させる。

    忘れると最終章のリボンが1フレームで消える。
    """
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    a.write_bytes(b"x")
    b.write_bytes(b"y")
    t = _ffconcat_text([(a, 1.0), (b, 2.0)])
    assert t.count("b.png") == 2
    assert t.count("a.png") == 1


def test_windows_paths_are_written_with_forward_slashes(tmp_path):
    p = tmp_path / "rib.png"
    p.write_bytes(b"x")
    assert "\\" not in _ffconcat_text([(p, 1.0)])


def test_an_empty_list_does_not_crash():
    assert _ffconcat_text([]).strip() == "ffconcat version 1.0"


def test_the_blank_filler_is_fully_transparent(tmp_path):
    """章が付いていない区間は**透明**で埋める（黒で埋めると画面が隠れる）。"""
    from PIL import Image

    tmp: list[str] = []
    p = _blank_png(tmp_path, 64, 32, tmp)
    im = Image.open(p)
    assert im.size == (64, 32)
    assert im.mode == "RGBA"
    assert im.getextrema()[3] == (0, 0)          # アルファが全部0


def test_the_blank_is_made_once(tmp_path):
    tmp: list[str] = []
    _blank_png(tmp_path, 8, 8, tmp)
    _blank_png(tmp_path, 8, 8, tmp)
    assert len(tmp) == 1
