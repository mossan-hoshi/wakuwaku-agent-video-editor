from wwedit.cut.autocut import (
    build_segments,
    is_filler,
    keep_regions_from_utterances,
)
from wwedit.edl.schema import Edl, SourceMedia, Utterance, Word


def _utt(speaker, words):
    return Utterance(
        speaker=speaker,
        text="".join(w[0] for w in words),
        start=words[0][1],
        end=words[-1][2],
        words=[Word(text=t, start=s, end=e) for t, s, e in words],
    )


def test_is_filler():
    assert is_filler("えー")
    assert is_filler(" あのー、")
    assert not is_filler("コード")


def test_keep_regions_margin_and_bridge():
    # 2語: [10,11] と [11.3,12]。pad=0.15, bridge=0.4 → 隙間0.3<0.4で結合
    u = _utt("a", [("これ", 10.0, 11.0), ("です", 11.3, 12.0)])
    regions = keep_regions_from_utterances([u], pad_s=0.15, bridge_s=0.4)
    assert len(regions) == 1
    assert regions[0][0] == 9.85  # 10 - 0.15
    assert regions[0][1] == 12.15  # 12 + 0.15


def test_keep_regions_split_on_long_gap():
    # 隙間 2s > bridge → 分割
    u = _utt("a", [("あ", 10.0, 11.0), ("い", 13.0, 14.0)])
    regions = keep_regions_from_utterances([u], pad_s=0.1, bridge_s=0.4)
    assert len(regions) == 2


def test_build_segments_silence_and_filler():
    edl = Edl(
        recording_dir="x",
        source=SourceMedia(video_path="v.mp4", duration_s=20.0),
        utterances=[
            _utt("a", [("本編", 5.0, 6.0), ("えー", 6.05, 6.4), ("続き", 6.5, 7.0)]),
        ],
    )
    segs = build_segments(edl, pad_s=0.1, bridge_s=0.4, cut_fillers=True)
    # 先頭[0,~4.9]無音, 本編keep, フィラーinvalid, 末尾無音 が含まれる
    reasons = {s.reason for s in segs if s.invalid}
    assert "silence" in reasons and "filler" in reasons
    # フィラー区間が invalid
    assert any(s.invalid and s.reason == "filler" for s in segs)
