from wwedit.cut.autocut import (
    build_segments,
    is_filler,
    keep_regions_from_utterances,
)
from wwedit.edl.schema import Edl, Segment, SourceMedia, Utterance, Word


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


def test_filler_candidate_duration_capped():
    # WhisperX が長音記号「ー」を後続無音まで引き伸ばす誤整列対策。
    # 「うーん」の末尾「ん」が 5.0→24.0s に整列しても候補は start+1.5s で打ち切る。
    from wwedit.cut.filler_llm import _MAX_FILLER_S, extract_candidates

    u = _utt("a", [("う", 5.0, 5.2), ("ー", 5.2, 5.5), ("ん", 5.5, 24.0)])
    cands = extract_candidates([u])
    assert len(cands) == 1
    c = cands[0]
    assert c.start == 5.0
    assert c.end == 5.0 + _MAX_FILLER_S  # 24.0 でなく打ち切り
    assert c.end - c.start <= _MAX_FILLER_S + 1e-9


def test_ngword_intervals_and_cut():
    from wwedit.cut.ngwords import apply_ngword_cuts, ng_intervals_from_utterances

    edl = Edl(
        recording_dir="x",
        source=SourceMedia(video_path="v.mp4", duration_s=30.0),
        segments=[Segment(id="k0", start=0.0, end=30.0, invalid=False)],
        utterances=[
            _utt("a", [("今日", 5.0, 5.5), ("は", 5.5, 5.7), ("マル秘", 5.8, 6.3)]),
            _utt("a", [("普通", 10.0, 10.5), ("の話", 10.5, 11.0)]),
        ],
    )
    # NG語を含む発話まるごとの区間（部分一致）
    iv = ng_intervals_from_utterances(edl.utterances, ["マル秘"])
    assert iv == [(5.0, 6.3)]
    assert ng_intervals_from_utterances(edl.utterances, []) == []  # 空NGは何もしない
    # segments に重ねると reason="ngword" の invalid が入り、前後は keep で残る
    segs, n = apply_ngword_cuts(edl, ["マル秘"])
    assert n == 1
    ng = [s for s in segs if s.reason == "ngword"]
    assert len(ng) == 1 and ng[0].start == 5.0 and ng[0].end == 6.3
    assert any(not s.invalid and s.start == 6.3 for s in segs)  # 直後はkeep


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
