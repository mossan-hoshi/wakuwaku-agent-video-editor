"""[S2] 片が多いワープは分けて焼いて連結する（1発で回すと極端に遅い）。

`concat` は入力を順に読むので、1本の filtergraph に片を全部載せると後ろの分岐が
自分の担当フレームを抱えたまま待つ。実測(1327片・素材39分)で CPU が1.3コアしか
回らず完走5時間以上の見込みだった。ここでは**分割されること**と、**分割しても
出力の総フレーム数が変わらないこと**を押さえる。
"""

from __future__ import annotations

import pytest

from wwedit.compose import warp_apply
from wwedit.compose.warp_apply import WARP_BATCH, build_warp_video_script, render_warped_footage

FPS = 25


@pytest.fixture
def calls(monkeypatch, tmp_path):
    """ffmpeg を呼ばずにコマンドだけ集める。"""
    got: list[list[str]] = []
    monkeypatch.setattr(warp_apply, "_run", lambda cmd, what: got.append(cmd))
    monkeypatch.setattr(warp_apply, "ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(warp_apply, "video_frame_count", lambda _p: 10_000_000)
    src = tmp_path / "src.mp4"
    src.write_bytes(b"x")
    return got, src


def _pieces(n: int):
    return [(float(i), float(i) + 0.8, 0.4, False) for i in range(n)]


def _encodes(cmds):
    return [c for c in cmds if "-filter_complex_script" in c]


def _concats(cmds):
    return [c for c in cmds if "concat" in c]


def test_small_plans_stay_a_single_ffmpeg_call(calls, tmp_path):
    got, src = calls
    render_warped_footage(src, _pieces(WARP_BATCH), tmp_path / "out.mp4", fps=FPS)
    assert len(_encodes(got)) == 1
    assert _concats(got) == []


def test_big_plans_are_split_then_concatenated(calls, tmp_path):
    got, src = calls
    n = WARP_BATCH * 3 + 7
    render_warped_footage(src, _pieces(n), tmp_path / "out.mp4", fps=FPS)
    assert len(_encodes(got)) == 4          # 120*3 + 7
    assert len(_concats(got)) == 1
    assert _concats(got)[0][-1] == str(tmp_path / "out.mp4")


def test_splitting_keeps_every_output_frame(calls, tmp_path):
    """バッチに割っても、焼き出す枚数の合計は1発のときと同じでなければならない。"""
    import re

    n = WARP_BATCH * 2 + 3
    pieces = _pieces(n)
    whole = sum(int(m) for m in re.findall(r"trim=end_frame=(\d+)",
                                           build_warp_video_script(pieces, fps=FPS)))
    split = sum(
        int(m)
        for i in range(0, n, WARP_BATCH)
        for m in re.findall(r"trim=end_frame=(\d+)",
                            build_warp_video_script(pieces[i:i + WARP_BATCH], fps=FPS))
    )
    assert split == whole


def test_a_finished_render_is_not_redone(calls, tmp_path):
    got, src = calls
    out = tmp_path / "out.mp4"
    render_warped_footage(src, _pieces(WARP_BATCH * 2), out, fps=FPS)
    out.write_bytes(b"done")                # 1回目は _run を潰しているので自分で置く
    got.clear()
    render_warped_footage(src, _pieces(WARP_BATCH * 2), out, fps=FPS)
    assert got == []


# --- 音も同じ（映像だけ直して**音を直し忘れた**のが 2026-08-07 のバグ） ---------

def _aencodes(cmds):
    return [c for c in cmds if "-filter_complex_script" in c and "pcm_s16le" in c]


def test_small_audio_plans_stay_a_single_ffmpeg_call(calls, tmp_path):
    got, src = calls
    warp_apply.render_warped_audio(src, _pieces(WARP_BATCH), tmp_path / "a.wav")
    assert len(_aencodes(got)) == 1
    assert _concats(got) == []


def test_big_audio_plans_are_split_then_concatenated(calls, tmp_path):
    """PCMを切って繋ぐだけなのに、千片を1発で回すと1本34分かかっていた。"""
    got, src = calls
    warp_apply.render_warped_audio(src, _pieces(WARP_BATCH * 3 + 7), tmp_path / "a.wav")
    assert len(_aencodes(got)) == 4
    assert len(_concats(got)) == 1
    assert _concats(got)[0][-1] == str(tmp_path / "a.wav")


def test_splitting_keeps_every_audio_sample(calls, tmp_path):
    """バッチに割っても、出力の総尺は1発のときと同じでなければならない。"""
    import re

    n = WARP_BATCH * 2 + 3
    pieces = _pieces(n)
    whole = sum(float(m) for m in re.findall(r"atrim=0:([0-9.]+)",
                                             warp_apply.build_warp_audio_script(pieces)))
    split = sum(
        float(m)
        for i in range(0, n, WARP_BATCH)
        for m in re.findall(r"atrim=0:([0-9.]+)",
                            warp_apply.build_warp_audio_script(pieces[i:i + WARP_BATCH]))
    )
    assert round(split, 3) == round(whole, 3)


def test_a_finished_audio_render_is_not_redone(calls, tmp_path):
    got, src = calls
    out = tmp_path / "a.wav"
    warp_apply.render_warped_audio(src, _pieces(WARP_BATCH * 2), out)
    out.write_bytes(b"done")            # _run を潰しているので自分で置く
    got.clear()
    warp_apply.render_warped_audio(src, _pieces(WARP_BATCH * 2), out)
    assert got == []


# --- エンコーダ切り替え（NVENC は品質指定の意味が x264 と違う） ------------------

def test_x264_uses_crf():
    from wwedit.compose.ffmpeg_compose import video_encode_args

    assert video_encode_args("x264", 20, "medium") == [
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p"]


def test_nvenc_pins_the_quality_instead_of_a_bitrate():
    """`-b:v 0` を落とすと既定ビットレートに引っ張られて破綻する。"""
    from wwedit.compose.ffmpeg_compose import video_encode_args

    args = video_encode_args("nvenc", 20, "medium")
    assert args[:2] == ["-c:v", "h264_nvenc"]
    assert "-cq" in args and args[args.index("-cq") + 1] == "20"
    assert args[args.index("-b:v") + 1] == "0"
    assert "medium" not in args, "x264 の preset 名を NVENC へ渡してはいけない"


def test_an_unknown_encoder_is_refused():
    import pytest as _pytest

    from wwedit.compose.ffmpeg_compose import video_encode_args

    with _pytest.raises(ValueError):
        video_encode_args("h265", 20, "medium")
