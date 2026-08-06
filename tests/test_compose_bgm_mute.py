"""`--bgm-avoid-desktop`: PCシステム音が鳴っている間だけ BGM を落とす。

**回ごとの判断**（既定OFF）。その回の音そのものを聴かせるときだけ付ける
（#103 は音楽生成の聴き比べで、下に BGM があると比較にならなかった）。
⚠️ **いきなり切らない**。前後にフェードを入れる（2026-08-06 ユーザー指示）。
"""
from __future__ import annotations

from wwedit.compose.ffmpeg_compose import (
    BGM_MUTE_FADE_S,
    BGM_MUTE_PAD_S,
    bgm_mute_expr,
    bgm_mute_spans_merged,
)


def test_no_spans_means_no_expression():
    assert bgm_mute_expr([]) == ""
    assert bgm_mute_expr(None) == ""


def test_padding_is_added_around_each_span():
    assert bgm_mute_spans_merged([(10.0, 20.0)], pad=0.3) == [(9.7, 20.3)]


def test_padding_never_goes_below_zero():
    assert bgm_mute_spans_merged([(0.1, 5.0)], pad=0.3) == [(0.0, 5.3)]


def test_spans_are_clamped_to_total():
    assert bgm_mute_spans_merged([(10.0, 30.0)], pad=0.3, total=30.0) == [(9.7, 30.0)]


def test_touching_spans_are_merged():
    assert bgm_mute_spans_merged([(10.0, 20.0), (20.4, 25.0)], pad=0.3) == [(9.7, 25.3)]


def test_separate_spans_stay_separate():
    got = bgm_mute_spans_merged([(10.0, 20.0), (40.0, 45.0)], pad=0.3)
    assert got == [(9.7, 20.3), (39.7, 45.3)]


def test_zero_length_spans_are_dropped():
    assert bgm_mute_spans_merged([(10.0, 10.0)], pad=0.0) == []


def _volume_at(expr: str, t: float) -> float:
    """ffmpeg の式を Python で評価して、その時刻の音量を得る（テスト用）。"""
    import re

    def clip(x, lo, hi):
        return max(lo, min(hi, x))

    py = expr.replace("\\,", ",")
    py = re.sub(r"\bt\b", repr(float(t)), py)
    return eval(py, {"clip": clip, "max": max, "min": min})   # noqa: S307


def test_volume_is_zero_inside_the_span():
    expr = bgm_mute_expr([(10.0, 20.0)], pad=0.0, fade=0.6)
    assert _volume_at(expr, 15.0) == 0.0
    assert _volume_at(expr, 10.0) == 0.0
    assert _volume_at(expr, 20.0) == 0.0


def test_volume_is_one_far_from_the_span():
    expr = bgm_mute_expr([(10.0, 20.0)], pad=0.0, fade=0.6)
    assert _volume_at(expr, 0.0) == 1.0
    assert _volume_at(expr, 100.0) == 1.0


def test_it_fades_out_before_and_in_after():
    """いきなり消えない: 手前で下がり、終わってから戻る。"""
    expr = bgm_mute_expr([(10.0, 20.0)], pad=0.0, fade=0.6)
    assert abs(_volume_at(expr, 9.7) - 0.5) < 1e-6      # 半分手前で半分
    assert abs(_volume_at(expr, 20.3) - 0.5) < 1e-6
    assert abs(_volume_at(expr, 9.4) - 1.0) < 1e-9      # fade ぶん手前では元通り
    assert abs(_volume_at(expr, 20.6) - 1.0) < 1e-9


def test_the_fade_is_monotonic():
    expr = bgm_mute_expr([(10.0, 20.0)], pad=0.0, fade=0.6)
    xs = [_volume_at(expr, 9.4 + i * 0.1) for i in range(7)]
    assert all(a >= b - 1e-9 for a, b in zip(xs, xs[1:], strict=False))


def test_two_spans_both_dip():
    expr = bgm_mute_expr([(10.0, 12.0), (40.0, 42.0)], pad=0.0, fade=0.5)
    assert _volume_at(expr, 11.0) == 0.0
    assert _volume_at(expr, 41.0) == 0.0
    assert _volume_at(expr, 25.0) == 1.0


def test_commas_are_escaped_for_the_filtergraph():
    """式のカンマはフィルタ引数の区切りと衝突するのでエスケープする。"""
    expr = bgm_mute_expr([(10.0, 20.0)])
    assert "\\," in expr
    assert ",0,1" not in expr


def test_defaults_are_sane():
    assert 0.0 < BGM_MUTE_PAD_S <= 1.0
    assert 0.0 < BGM_MUTE_FADE_S <= 2.0
