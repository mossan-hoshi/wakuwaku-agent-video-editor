"""[V] 方式B フリーズフレームの compose 対応テスト（ffmpeg 非実行・文字列検証）。"""

from __future__ import annotations

import pytest

from wwedit.compose.ffmpeg_compose import (
    _src_to_out,
    build_audio_filter_script,
    build_filter_script,
    build_filter_script_framed,
    out_total,
    src_to_out,
    stretch_time,
    subtitles_to_output,
)
from wwedit.edl.schema import Edl, Freeze, SourceMedia, Subtitle, TimeRange


def _ranges():
    return [TimeRange(start=1.0, end=2.5), TimeRange(start=10.0, end=12.0)]


def _frz(at: float, extra: float) -> Freeze:
    return Freeze(at=at, extra=extra)


# ---- 回帰: freezes=() で現行出力と完全一致 ----

def test_no_freeze_scripts_unchanged():
    s = build_filter_script(_ranges())
    assert "[0:v]trim=start=1.000:end=2.500" in s
    assert "[0:a]atrim=start=10.000:end=12.000" in s
    assert "concat=n=2:v=1:a=1[outv][outa]" in s
    assert "tpad" not in s
    assert build_filter_script(_ranges()) == build_filter_script(_ranges(), freezes=())


def test_no_freeze_src_to_out_unchanged():
    assert _src_to_out(_ranges(), 1.5) == 0.5
    assert _src_to_out(_ranges(), 11.0) == 1.5 + 1.0
    assert _src_to_out(_ranges(), 0.0) == 0.0
    assert _src_to_out(_ranges(), 99.0) == 3.5


# ---- stretch_time / out_total ----

def test_stretch_time_shifts_only_after_at():
    frz = [_frz(10.0, 2.0)]
    assert stretch_time(5.0, frz) == 5.0
    assert stretch_time(10.0, frz) == 10.0   # at ちょうどはシフトしない
    assert stretch_time(10.5, frz) == 12.5


def test_out_total_adds_in_range_extras_only():
    frz = [_frz(1.5, 2.0), _frz(5.0, 9.0)]  # 5.0 はカット内 → 数えない
    assert out_total(_ranges(), frz) == pytest.approx(3.5 + 2.0)
    assert out_total(_ranges()) == pytest.approx(3.5)


# ---- _src_to_out の freeze シフト ----

def test_src_to_out_with_freeze():
    frz = [_frz(1.5, 2.0)]
    # freeze より前は不変、後は +2.0
    assert _src_to_out(_ranges(), 1.2, frz) == pytest.approx(0.2)
    assert _src_to_out(_ranges(), 2.0, frz) == pytest.approx(1.0 + 2.0)
    assert _src_to_out(_ranges(), 11.0, frz) == pytest.approx(2.5 + 2.0)
    # 公開版も同じ
    assert src_to_out(_ranges(), 11.0, frz) == _src_to_out(_ranges(), 11.0, frz)


def test_src_to_out_ignores_freeze_outside_ranges():
    frz = [_frz(5.0, 9.0)]  # カット区間内 → 無視
    assert _src_to_out(_ranges(), 11.0, frz) == pytest.approx(2.5)


# ---- 映像 tpad / 音声 σ 座標 ----

def test_filter_script_freeze_splits_and_tpads():
    frz = [_frz(1.5, 2.0)]
    s = build_filter_script(_ranges(), freezes=frz)
    # 1.0-2.5 が 1.0-1.5(+tpad 2.0s) と 1.5-2.5 に分割される
    assert "[0:v]trim=start=1.000:end=1.500,setpts=PTS-STARTPTS," \
           "tpad=stop_mode=clone:stop_duration=2.000[v0];" in s
    assert "[0:v]trim=start=1.500:end=2.500,setpts=PTS-STARTPTS[v1];" in s
    # 音声は σ 座標: piece0 は [1.0, 1.5+2.0]、piece1 以降は +2.0 シフト
    assert "[0:a]atrim=start=1.000:end=3.500,asetpts=PTS-STARTPTS[a0];" in s
    assert "[0:a]atrim=start=3.500:end=4.500,asetpts=PTS-STARTPTS[a1];" in s
    assert "[0:a]atrim=start=12.000:end=14.000,asetpts=PTS-STARTPTS[a2];" in s
    assert "concat=n=3:v=1:a=1" in s


def test_framed_filter_script_freeze():
    edl = Edl(recording_dir="d", source=SourceMedia(video_path="v.mp4"))
    frz = [_frz(10.5, 1.0)]
    s = build_filter_script_framed(edl, _ranges(), freezes=frz)
    assert "tpad=stop_mode=clone:stop_duration=1.000" in s
    assert "concat=n=3:v=1:a=1" in s
    # freeze 無しは従来通り
    s0 = build_filter_script_framed(edl, _ranges())
    assert "tpad" not in s0 and "concat=n=2:v=1:a=1" in s0


def test_audio_filter_script_freeze_sigma():
    frz = [_frz(1.5, 2.0)]
    s = build_audio_filter_script(_ranges(), freezes=frz)
    assert "atrim=start=1.000:end=3.500" in s
    assert "atrim=start=12.000:end=14.000" in s
    assert build_audio_filter_script(_ranges()) == build_audio_filter_script(
        _ranges(), freezes=())


# ---- 字幕のフリーズ持続 ----

def test_subtitle_spans_freeze():
    subs = [Subtitle(start=1.0, end=1.55, text="x")]  # freeze at=1.5 を跨ぐ
    frz = [_frz(1.5, 2.0)]
    out = subtitles_to_output(subs, _ranges(), freezes=frz)
    assert out[0].start == pytest.approx(0.0)
    assert out[0].end == pytest.approx(0.55 + 2.0)  # end 側だけ延長＝フリーズ中も表示
