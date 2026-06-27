"""compose.ffmpeg_compose のフレーミング適用（crop+scale）テスト。"""

from __future__ import annotations

from wwedit.compose.ffmpeg_compose import (
    bbox_at,
    build_filter_script_framed,
    build_framed_overlay_script,
    framing_crop_filter,
    loading_overlay_intervals,
)
from wwedit.edl.schema import Edl, FramingRegion, SourceMedia, TimeRange


def _edl(framing):
    return Edl(
        recording_dir="2026-06-04",
        source=SourceMedia(video_path="v.mp4", duration_s=100.0),
        framing=framing,
    )


def test_crop_filter_with_bbox():
    f = framing_crop_filter((307, 209, 1304, 733), 1920, 1080)
    assert f == "crop=1304:733:307:209,scale=1920:1080"


def test_crop_filter_none_and_degenerate_is_scale_only():
    assert framing_crop_filter(None) == "scale=1920:1080"
    assert framing_crop_filter((0, 0, 0, 0)) == "scale=1920:1080"


def test_bbox_at_finds_covering_region():
    edl = _edl(
        [
            FramingRegion(start=0.0, end=10.0, kind="static", bbox=(1, 2, 3, 4)),
            FramingRegion(start=10.0, end=20.0, kind="static", bbox=(5, 6, 7, 8)),
        ]
    )
    assert bbox_at(edl, 5.0) == (1, 2, 3, 4)
    assert bbox_at(edl, 15.0) == (5, 6, 7, 8)
    assert bbox_at(edl, 25.0) is None  # 範囲外


def test_framed_script_applies_per_segment_crop():
    edl = _edl(
        [
            FramingRegion(start=0.0, end=10.0, kind="static", bbox=(100, 50, 800, 450)),
            FramingRegion(start=10.0, end=30.0, kind="static", bbox=None),  # no_crop
        ]
    )
    ranges = [TimeRange(start=1.0, end=5.0), TimeRange(start=12.0, end=18.0)]
    script = build_filter_script_framed(edl, ranges, vsrc="0:v", asrc="1:a")
    # 1区間目は crop あり、2区間目は scale のみ
    assert "crop=800:450:100:50,scale=1920:1080[v0]" in script
    assert "setpts=PTS-STARTPTS,scale=1920:1080[v1]" in script
    assert "concat=n=2:v=1:a=1[outv][outa]" in script
    # 音声入力ラベルが反映される
    assert "[1:a]atrim=start=1.000:end=5.000" in script


def test_loading_intervals_map_to_output_time():
    edl = _edl(
        [FramingRegion(start=5.0, end=8.0, kind="loading", loading_label="次の準備")]
    )
    # カット無し: 出力時刻=ソース時刻
    iv = loading_overlay_intervals(edl, [TimeRange(start=0.0, end=10.0)])
    assert len(iv) == 1
    assert iv[0]["out_start"] == 5.0 and iv[0]["out_end"] == 8.0
    assert iv[0]["label"] == "次の準備"


def test_loading_intervals_compress_across_cut():
    edl = _edl([FramingRegion(start=5.0, end=8.0, kind="loading")])
    # ソース4-6をカット → loading∩keep は [6,8] が keep[6,10] に乗り、出力では 4..6
    iv = loading_overlay_intervals(
        edl, [TimeRange(start=0.0, end=4.0), TimeRange(start=6.0, end=10.0)]
    )
    assert len(iv) == 1
    assert iv[0]["out_start"] == 4.0 and iv[0]["out_end"] == 6.0


def test_overlay_script_layers_loading_on_top():
    edl = _edl(
        [
            FramingRegion(start=0.0, end=10.0, kind="static", bbox=(0, 0, 960, 540)),
            FramingRegion(start=5.0, end=8.0, kind="loading", loading_label="準備"),
        ]
    )
    ranges = [TimeRange(start=0.0, end=10.0)]
    iv = loading_overlay_intervals(edl, ranges)
    script = build_framed_overlay_script(edl, ranges, iv, ["2:v"], asrc="1:a")
    assert "[base0]" in script  # 土台
    assert "setpts=PTS+5.000/TB[L0]" in script  # クリップを出力5sへ遅延
    assert "overlay=eof_action=pass:enable='between(t,5.000,8.000)'[outv]" in script
    assert not script.rstrip().endswith(";")  # 末尾セミコロン無し


def test_overlay_script_no_intervals_renames_base():
    edl = _edl([FramingRegion(start=0.0, end=10.0, kind="static", bbox=None)])
    ranges = [TimeRange(start=0.0, end=10.0)]
    script = build_framed_overlay_script(edl, ranges, [], [], asrc="0:a")
    assert "[base0]null[outv]" in script
