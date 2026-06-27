"""subtitle.build（発話→字幕）と compose.subtitles_to_output（出力時刻変換）のテスト。"""

from __future__ import annotations

from wwedit.compose.ffmpeg_compose import subtitles_to_output
from wwedit.edl.schema import Edl, SourceMedia, Subtitle, TimeRange, Utterance
from wwedit.subtitle.build import split_text, subtitles_from_utterances


def _edl(utts):
    return Edl(
        recording_dir="2026-06-04",
        source=SourceMedia(video_path="v.mp4", duration_s=100.0),
        utterances=utts,
    )


def test_split_text_short_passthrough():
    assert split_text("短い", max_chars=28) == ["短い"]
    assert split_text("", max_chars=28) == []


def test_split_text_breaks_long_on_punctuation():
    t = "これはとても長い発話で、句読点のところで区切られるはずです。たぶん。"
    parts = split_text(t, max_chars=12)
    assert len(parts) >= 2
    assert all(len(p) <= 12 + 4 for p in parts)  # 句読点境界で多少超過は許容
    assert "".join(parts).replace(" ", "") == t.replace("、", "、").replace(" ", "")


def test_subtitles_from_utterances_distributes_time():
    edl = _edl([Utterance(speaker="s", text="A" * 56, start=10.0, end=14.0)])
    subs = subtitles_from_utterances(edl, max_chars=28)
    assert len(subs) == 2  # 56字→28×2
    assert subs[0].start == 10.0 and subs[1].end == 14.0
    assert subs[0].end == subs[1].start  # 連続


def test_subtitles_skip_empty_and_too_short():
    edl = _edl(
        [
            Utterance(speaker="s", text="  ", start=0.0, end=5.0),
            Utterance(speaker="s", text="短", start=0.0, end=0.1),  # min_dur未満
        ]
    )
    assert subtitles_from_utterances(edl) == []


def test_subtitles_to_output_maps_and_drops_cut():
    subs = [
        Subtitle(start=2.0, end=3.0, text="keep", style="main"),
        Subtitle(start=4.5, end=5.5, text="cut", style="main"),  # カット区間内
    ]
    ranges = [TimeRange(start=0.0, end=4.0), TimeRange(start=6.0, end=10.0)]
    out = subtitles_to_output(subs, ranges)
    assert len(out) == 1  # cut内は除外
    assert out[0].text == "keep"
    assert out[0].start == 2.0 and out[0].end == 3.0
