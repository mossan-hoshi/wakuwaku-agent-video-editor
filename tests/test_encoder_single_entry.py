"""映像を焼くところは**全部 `video_encode_args` を通す**（`libx264` を直書きしない）。

2026-08-07 の実害: 「NVENC を使う」と言われて `compose video` にだけ `--encoder` を
足し、`compose warp` が `libx264` 決め打ちのままだったので **12分ぶんが CPU で焼かれた**。
焼く場所はコード全体で10箇所あり、フラグを1つずつ足す方式では必ず取りこぼす。
入口を1つにして、**取りこぼしをテストで落とす**。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "wwedit"

#: `libx264` の文字列を持っていてよいファイル（実際に焼かない・説明だけ）。
ALLOWED = {
    "compose/ffmpeg_compose.py",   # 唯一の組み立て場所
    "common/media.py",             # エラー抽出の説明文に出てくるだけ
}


def _py_files():
    return sorted(p for p in SRC.rglob("*.py"))


def test_no_module_hardcodes_the_cpu_encoder():
    """**これが本命**。新しい工程が `-c:v libx264` を直書きしたら落ちる。"""
    bad = []
    for p in _py_files():
        rel = p.relative_to(SRC).as_posix()
        if rel in ALLOWED:
            continue
        if "libx264" in p.read_text(encoding="utf-8"):
            bad.append(rel)
    assert not bad, f"video_encode_args を通さず焼いている: {bad}"


def test_no_module_hardcodes_a_video_codec_flag():
    """`-c:v` を直に並べるのも禁止（別のコーデック名でも同じ穴が空く）。"""
    bad = []
    for p in _py_files():
        rel = p.relative_to(SRC).as_posix()
        if rel in ALLOWED:
            continue
        if re.search(r'"-c:v"\s*,\s*"(?!copy)', p.read_text(encoding="utf-8")):
            bad.append(rel)
    assert not bad, f"-c:v を直書きしている: {bad}"


def test_the_env_var_switches_every_stage(monkeypatch):
    from wwedit.compose.ffmpeg_compose import ENCODER_ENV, default_encoder, video_encode_args

    monkeypatch.setenv(ENCODER_ENV, "nvenc")
    assert default_encoder() == "nvenc"
    assert "h264_nvenc" in video_encode_args(None, 20, "medium")


def test_an_explicit_encoder_beats_the_env(monkeypatch):
    from wwedit.compose.ffmpeg_compose import ENCODER_ENV, video_encode_args

    monkeypatch.setenv(ENCODER_ENV, "nvenc")
    assert "libx264" in video_encode_args("x264", 20, "medium")


@pytest.mark.parametrize("val", ["", "cuda", "qsv", "  "])
def test_an_unknown_value_falls_back_to_cpu(monkeypatch, val):
    """未知の値で**黙って壊れた引数を吐かない**（落ちるより x264 で焼く方がまし）。"""
    from wwedit.compose.ffmpeg_compose import ENCODER_ENV, default_encoder

    monkeypatch.setenv(ENCODER_ENV, val)
    assert default_encoder() == "x264"


def test_switching_the_encoder_invalidates_the_warp_cache(monkeypatch, tmp_path):
    """エンコーダを変えたら**ワープ素材を焼き直す**。

    鍵に入れ忘れると、x264 で焼いた素材を nvenc 指定のまま使い回して
    「切り替えたのに何も変わらない」になる。
    """
    from wwedit.compose import warp_apply
    from wwedit.compose.ffmpeg_compose import ENCODER_ENV

    src = tmp_path / "in.mp4"
    src.write_bytes(b"x" * 64)
    pieces = [(0.0, 1.0, 1.0, False)]
    seen: list[str] = []

    monkeypatch.setattr(warp_apply, "video_frame_count", lambda _s: 100)
    monkeypatch.setattr(
        warp_apply, "_encode_warp_part",
        lambda *a, encoder="x264", **k: (seen.append(encoder), a[2].write_bytes(b"m")))

    monkeypatch.setenv(ENCODER_ENV, "x264")
    warp_apply.render_warped_footage(src, pieces, tmp_path / "o.mp4", fps=25)
    monkeypatch.setenv(ENCODER_ENV, "nvenc")
    warp_apply.render_warped_footage(src, pieces, tmp_path / "o.mp4", fps=25)
    assert seen == ["x264", "nvenc"]
