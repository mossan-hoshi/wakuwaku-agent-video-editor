"""読み上げクリップの src アンカーは、**読まないターンの時間も受け持つ**。

voice-scripter は相槌などのターンを空にして中身を隣のターンへまとめる
（2026-08-06 の回は 197 ターン中 105 件が空）。ターン自身の区間しか按分に使わないと
空にしたターンの時間が宙に浮き、読み上げに対して素材が足りなくなる。すると
`timewarp` の速度計画が壊れて、**完成品で映像と音声がずれる**（実測: 逐語一致した
152 文の実位置と中央 25.6 秒ずれ / 素材は読み上げの 0.68 倍しかない）。
区間を「次に読むターンの開始」まで延ばすと 2.4 秒 / 1.01 倍になった。
"""

from __future__ import annotations

from wwedit.compose.ffmpeg_compose import TimeRange
from wwedit.publish.cli import _split_turn_spans


def _clip(uid: int, start: float, end: float, sub: int = 0) -> dict:
    return {"uid": uid, "sub": sub, "start": start, "end": end, "text": "あ"}


RANGES = [TimeRange(start=0.0, end=600.0)]


def test_a_turn_reaches_the_next_spoken_turn():
    """**これが本命**。あいだの空ターン（10〜50秒）は前のターンが受け持つ。"""
    clips = [_clip(1, 10.0, 12.0), _clip(9, 50.0, 55.0)]
    got = _split_turn_spans(clips, {0: 3.0, 1: 3.0}, RANGES)
    assert got[0][0] == 10.0
    assert got[0][1] == 50.0          # 自分の 12.0 ではなく次の開始まで


def test_the_last_turn_keeps_its_own_end():
    """末尾には「次」が無いので自分の終端のまま（勝手に素材の最後まで伸ばさない）。"""
    clips = [_clip(1, 10.0, 12.0), _clip(9, 50.0, 55.0)]
    got = _split_turn_spans(clips, {0: 3.0, 1: 3.0}, RANGES)
    assert got[1] == (50.0, 55.0)


def test_spans_tile_without_gaps():
    """区間は隙間なく単調に並ぶ（空ターンがどちらへまとめられたかに依存しない）。"""
    clips = [_clip(1, 0.0, 2.0), _clip(4, 30.0, 33.0), _clip(8, 100.0, 104.0)]
    got = _split_turn_spans(clips, {0: 1.0, 1: 1.0, 2: 1.0}, RANGES)
    assert got[0][1] == got[1][0] == 30.0
    assert got[1][1] == got[2][0] == 100.0


def test_sentences_in_one_turn_split_the_extended_span():
    """同じターンの文は、**延ばしたあとの区間**を読み上げ実尺の比で分け合う。"""
    clips = [_clip(1, 0.0, 10.0, sub=0), _clip(1, 0.0, 10.0, sub=1),
             _clip(5, 60.0, 62.0)]
    got = _split_turn_spans(clips, {0: 1.0, 1: 3.0, 2: 1.0}, RANGES)
    assert got[0][0] == 0.0
    assert got[0][1] == got[1][0] == 15.0     # 60秒を 1:3 で分けた境目
    assert got[1][1] == 60.0


def test_an_absorbing_turn_gets_the_whole_stretch():
    """吸収パターン: 2秒のターンが後続の空ターンぶん（3分）を受け持つ。

    これができないと「読み上げ3分に対して素材2秒」になり、映像がほぼ止まる。
    """
    clips = [_clip(3, 100.0, 102.0), _clip(40, 280.0, 283.0)]
    got = _split_turn_spans(clips, {0: 180.0, 1: 3.0}, RANGES)
    assert got[0][1] - got[0][0] == 180.0     # 読み上げ 180 秒に素材 180 秒


def test_an_overlapping_next_turn_does_not_shorten_a_span():
    """次のターンが自分より前に始まっていても（話者かぶり）区間は縮めない。"""
    clips = [_clip(1, 10.0, 30.0), _clip(2, 25.0, 40.0)]
    got = _split_turn_spans(clips, {0: 5.0, 1: 5.0}, RANGES)
    assert got[0][1] == 30.0
