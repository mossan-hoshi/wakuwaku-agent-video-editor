"""[E] 感情エフェクト（PIL 描画）と配置・切替時刻の純関数テスト。"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from wwedit.chibi import fx
from wwedit.chibi.timeline import emotion_change_times
from wwedit.compose.chibi_fx_overlay import fx_placement


@pytest.mark.parametrize("emotion", fx.FX_EMOTIONS)
def test_fx_frames_uniform_size_and_envelope(emotion: str):
    """全フレーム同一サイズ・RGBA。出はポップ、引きはフェード。"""
    paths = fx.fx_frames(emotion, 96)
    assert len(paths) == fx.FX_FRAMES
    imgs = [Image.open(p).convert("RGBA") for p in paths]
    assert {im.size for im in imgs} == {(96, 96)}          # concat demuxer の制約
    alphas = [float(np.asarray(im.split()[3]).mean()) for im in imgs]
    assert alphas[0] < alphas[6]                            # 立ち上がる
    assert alphas[-1] < alphas[6]                           # 消える
    assert max(alphas) > 0.5                                # 中盤は実体がある


def test_fx_frames_are_deterministic(tmp_path):
    """同じ入力なら**バイト一致**（キャッシュとレンダの再現性）。"""
    a = [p.read_bytes() for p in fx.fx_frames("angry", 64)]
    b = [p.read_bytes() for p in fx.fx_frames("angry", 64)]
    assert a == b


def test_fx_idle_frame_is_transparent():
    p = fx.fx_idle_frame(48)
    im = Image.open(p).convert("RGBA")
    assert im.size == (48, 48)
    assert int(np.asarray(im.split()[3]).max()) == 0


def test_fx_frames_rejects_normal():
    with pytest.raises(ValueError):
        fx.fx_frames("normal", 64)


def test_envelope_bounds():
    for i in range(fx.FX_FRAMES):
        s, a, dy = fx.envelope(i)
        assert 0.0 <= a <= 1.0
        assert 0.0 <= s <= 1.3
        assert dy >= 0.0


def test_fx_placement_is_mirrored_and_inside_frame():
    kw = dict(out_w=1920, out_h=1080, chibi_h=320, margin=(24, 24))
    lx, ly, size = fx_placement("left", **kw)
    rx, ry, size2 = fx_placement("right", **kw)
    assert size == size2 == 112                     # 320 * 0.35
    assert ly == ry                                 # 高さは左右同じ
    assert isinstance(lx, int) and isinstance(ly, int)
    # 画面内に収まる
    assert 0 <= lx and lx + size <= 1920
    assert 0 <= rx and rx + size <= 1920
    assert 0 <= ly and ly + size <= 1080
    # 左右対称（画面中央に対して）
    assert lx + size // 2 == pytest.approx(1920 - (rx + size // 2), abs=1)


def test_emotion_change_times_skips_normal_and_dedupes():
    track = [(0.0, 5.0, "normal"), (5.0, 7.5, "surprised"), (7.5, 8.0, "normal"),
             (8.0, 8.3, "smile"), (8.3, 20.0, "normal"), (20.0, 22.0, "angry")]
    out = emotion_change_times(track)
    assert out == [(5.0, "surprised"), (8.0, "smile"), (20.0, "angry")]
    # 最小間隔より近い切替は間引く
    dense = [(0.0, 1.0, "surprised"), (1.0, 1.2, "normal"), (1.2, 2.0, "angry")]
    assert emotion_change_times(dense, min_gap=2.0) == [(0.0, "surprised")]


def test_fx_side_specs_writes_ffconcat(tmp_path):
    """エフェクトの ffconcat は総尺＝出力尺で、末尾を重複させる。"""
    from wwedit.compose.chibi_fx_overlay import fx_side_specs

    class _Spec:
        side = "left"
        emotions = ((0.0, 2.0, "normal"), (2.0, 4.0, "surprised"), (4.0, 10.0, "normal"))

    specs = fx_side_specs([_Spec()], tmp_dir=tmp_path, chibi_h=320, total=10.0)
    assert len(specs) == 1
    text = specs[0].ffconcat_path.read_text(encoding="utf-8")
    assert text.startswith("ffconcat version 1.0")
    durs = [float(ln.split()[1]) for ln in text.splitlines() if ln.startswith("duration")]
    assert sum(durs) == pytest.approx(10.0, abs=1e-3)
    files = [ln for ln in text.splitlines() if ln.startswith("file ")]
    assert files[-1] == files[-2]                   # 末尾重複（duration 無視対策）
