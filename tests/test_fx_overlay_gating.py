"""[E] 感情エフェクトは**出ている区間だけ**合成する。

エフェクトは実測で 25分の動画に約10秒（1%未満）しか出ない。それなのに素のままだと
ffconcat を全長ぶん流し、毎フレーム RGBA のアルファ合成が左右2段走る。
`enable` で出ている所だけに絞り、**1度も出ない側は入力ごと作らない**。
"""

from __future__ import annotations

from wwedit.compose.ffmpeg_compose import _enable_expr


def test_a_single_span_becomes_one_between():
    assert _enable_expr([(10.0, 11.0)], pad=0.0) == "between(t,10.000,11.000)"


def test_spans_are_joined_with_plus():
    """ffmpeg の式では ``+`` が論理和として働く（非0なら真）。"""
    got = _enable_expr([(1.0, 2.0), (5.0, 6.0)], pad=0.0)
    assert got == "between(t,1.000,2.000)+between(t,5.000,6.000)"


def test_a_pad_widens_both_ends():
    """境界のフレームを取りこぼさないための余白。"""
    got = _enable_expr([(10.0, 11.0)], pad=0.05)
    assert got == "between(t,9.950,11.050)"


def test_the_pad_never_goes_negative():
    """先頭のエフェクトで ``between(t,-0.05,..)`` を作らない。"""
    assert _enable_expr([(0.0, 1.0)], pad=0.05).startswith("between(t,0.000,")


def test_no_spans_gives_an_empty_expression():
    """空なら呼び手が**その段ごと作らない**（式で false にするのではない）。"""
    assert _enable_expr([]) == ""


def test_fx_specs_record_where_the_effect_plays(tmp_path):
    """`fx_side_specs` は ffconcat と一緒に**出現区間**を返す。"""
    from wwedit.compose.chibi_fx_overlay import fx_side_specs

    class _Side:                       # emotions は (開始, 終了, 感情)
        side = "left"
        emotions = [(0.0, 5.0, "normal"), (5.0, 6.0, "angry"), (6.0, 60.0, "normal")]

    specs = fx_side_specs([_Side()], tmp_dir=tmp_path, chibi_h=320, total=60.0)
    assert specs, "1体ぶんの仕様が返る"
    fs = specs[0]
    assert isinstance(fs.spans, tuple)
    # 何も出ない側は spans が空＝合成側でスキップされる
    for a, b in fs.spans:
        assert 0.0 <= a < b <= 60.0
