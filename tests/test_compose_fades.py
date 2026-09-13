"""[S3] 本編音声フェードの包絡線を縛る。

`afade` を重ねると「立ち上がりの前が全部無音」になるため `volume` の式で組んでいる。
式が壊れると**気づかないまま本編の音が消える**ので、値そのものを確認する。
"""

from __future__ import annotations

import math

import pytest

from wwedit.compose.fades import (
    apply_audio_fades,
    fade_volume_expr,
    parse_chapter_seconds,
)


def _eval(expr: str, t: float) -> float:
    """ffmpeg の式を Python で評価する（between/if/min だけ使っている）。

    ``if`` は Python の予約語なので ``_if`` へ置換してから eval する。
    """
    ns = {
        "t": t,
        "between": lambda x, a, b: 1.0 if a <= x <= b else 0.0,
        "_if": lambda c, a, b=0.0: a if c else b,
        "min": min,
        "max": max,
    }
    py = expr.replace("if(", "_if(")
    return float(eval(py, {"__builtins__": {}}, ns))  # noqa: S307 - 自前の式のみ


def test_parse_chapter_seconds():
    lines = ["00:00 先頭", "06:28 次", "1:02:03 長い回", "ゴミ行"]
    assert parse_chapter_seconds(lines) == [0, 388, 3723]


def test_body_fade_in_and_out():
    expr = fade_volume_expr([], total=100.0, body_in=0.8, body_out=1.2)
    assert _eval(expr, 0.0) == pytest.approx(0.0)
    assert _eval(expr, 0.4) == pytest.approx(0.5, abs=1e-3)
    assert _eval(expr, 0.8) == pytest.approx(1.0)
    assert _eval(expr, 50.0) == pytest.approx(1.0)      # 中間はそのまま
    assert _eval(expr, 99.4) == pytest.approx(0.5, abs=1e-3)
    assert _eval(expr, 100.0) == pytest.approx(0.0)


def test_chapter_boundary_fades_down_then_back():
    # 境界 T=50 にアイキャッチ 2 秒。落とし 0.35 秒 → 無音 2 秒 → 戻し 0.35 秒。
    expr = fade_volume_expr([50.0], total=100.0, eyecatch_dur=2.0, edge=0.35)
    assert _eval(expr, 49.0) == pytest.approx(1.0)       # 手前は素通し
    assert _eval(expr, 49.825) == pytest.approx(0.5, abs=1e-2)
    assert _eval(expr, 50.0) == pytest.approx(0.0, abs=1e-6)
    assert _eval(expr, 51.0) == pytest.approx(0.0)       # アイキャッチ中は無音
    assert _eval(expr, 52.0) == pytest.approx(0.0, abs=1e-6)
    assert _eval(expr, 52.175) == pytest.approx(0.5, abs=1e-2)
    assert _eval(expr, 52.35) == pytest.approx(1.0)
    assert _eval(expr, 60.0) == pytest.approx(1.0)


def test_multiple_boundaries_do_not_mute_each_other():
    """`afade` を使うとここが壊れる（後段の立ち上がり前が全部無音になる）。"""
    expr = fade_volume_expr([50.0, 200.0], total=400.0, eyecatch_dur=2.0)
    for t in (10.0, 60.0, 150.0, 300.0):
        assert _eval(expr, t) == pytest.approx(1.0), f"t={t} で音が落ちている"
    assert _eval(expr, 200.0) == pytest.approx(0.0, abs=1e-6)


def test_boundary_at_the_very_edges_is_skipped():
    """端に寄りすぎた境界は本編フェードに任せる（負の時刻や総尺越えを作らない）。"""
    expr = fade_volume_expr([0.0, 99.9], total=100.0, eyecatch_dur=2.0)
    assert "between" not in expr           # 境界の項が1つも立たない
    assert _eval(expr, 50.0) == pytest.approx(1.0)


def test_envelope_never_leaves_unit_range():
    expr = fade_volume_expr([30.0, 60.0], total=100.0, eyecatch_dur=2.0)
    for i in range(0, 1001):
        v = _eval(expr, i * 0.1)
        assert -1e-9 <= v <= 1.0 + 1e-9, f"t={i * 0.1} で {v}"
        assert not math.isnan(v)


def test_apply_audio_fades_rejects_missing_input(tmp_path):
    with pytest.raises((RuntimeError, OSError)):
        apply_audio_fades(tmp_path / "nope.mp4", tmp_path / "out.mp4",
                          boundaries=[10.0], total=100.0)
