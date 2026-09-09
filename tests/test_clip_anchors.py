"""読み上げクリップの時刻アンカーは、**本文照合で元の文字起こしの位置**に合わせる。

ターンの区間を按分するだけでは足りない。台本が相槌を空にして中身を先頭ターンへ吸収すると、
数秒の中に何分ぶんもの文が潰れ、**映像だけが先へ進む**（2026-08-07 実測: 中央62秒・最大196秒
→ 1:10 の映像に 0:45 の音声）。文の内容がどこにあるかを探せば、吸収されていても正しく散る。
"""

from __future__ import annotations

from wwedit.edl.schema import Edl, Segment, SourceMedia, SpeakerTrack, Utterance, Word
from wwedit.publish.voice_tts import anchor_clips, kept_char_times

SPEAKERS = "AB"


def _utt(speaker: str, text: str, t0: float, step: float = 0.5) -> Utterance:
    ws = [Word(text=c, start=t0 + i * step, end=t0 + (i + 1) * step)
          for i, c in enumerate(text)]
    return Utterance(speaker=speaker, text=text, start=t0, end=t0 + len(text) * step, words=ws)


def _edl(utts: list[Utterance]) -> Edl:
    return Edl(
        recording_dir="2026-08-06",
        source=SourceMedia(video_path="v.mp4", duration_s=3000.0, fps=25,
                           audio_tracks=[SpeakerTrack(speaker=s, path=f"{s}.m4a")
                                         for s in SPEAKERS]),
        segments=[Segment(id="s0", start=0.0, end=3000.0)],
        utterances=utts,
    )


def test_the_transcript_is_flattened_in_time_order():
    edl = _edl([_utt("A", "あいうえお", 100.0), _utt("B", "かきくけこ", 200.0)])
    text, times = kept_char_times(edl)
    assert text == "あいうえおかきくけこ"
    assert times[0] == 100.0 and times[5] == 200.0
    assert times == sorted(times)


def test_a_sentence_absorbed_from_a_later_turn_anchors_there():
    """**これが本命**。台本が3文とも uid0 に入っていても、2文目・3文目は先の時刻へ。"""
    edl = _edl([_utt("A", "きょうはいいてんきですね", 100.0),
                _utt("B", "そうですねあたたかいです", 300.0),
                _utt("A", "さくらがさいてきました", 500.0)])
    clips = [
        {"uid": 0, "sub": 0, "text": "今日はいいてんきですね。"},
        {"uid": 0, "sub": 1, "text": "そうですねあたたかいです。"},
        {"uid": 0, "sub": 2, "text": "さくらがさいてきました。"},
    ]
    got = anchor_clips(edl, clips)
    assert set(got) == {0, 1, 2}
    assert got[0][0] < got[1][0] < got[2][0], "吸収された文が同じ位置に潰れている"
    assert got[1][0] >= 300.0 and got[2][0] >= 500.0


def test_anchors_never_go_backwards():
    edl = _edl([_utt("A", "あいうえおかきくけこ", 100.0),
                _utt("B", "さしすせそたちつてと", 200.0)])
    clips = [{"uid": 0, "sub": 0, "text": "さしすせそたちつてと"},
             {"uid": 1, "sub": 0, "text": "あいうえおかきくけこ"}]
    got = anchor_clips(edl, clips)
    starts = [got[i][0] for i in sorted(got)]
    assert starts == sorted(starts), "後ろの文が前の時刻へ戻っている"


def test_a_sentence_with_no_counterpart_is_left_out():
    edl = _edl([_utt("A", "きょうはいいてんきですね", 100.0)])
    clips = [{"uid": 0, "sub": 0, "text": "まったくかんけいのないはなし"}]
    assert anchor_clips(edl, clips) == {}


def test_polished_wording_still_matches():
    """台本はフィラーを削って整形されている。完全一致は求めない。"""
    edl = _edl([_utt("A", "えーとですねきょうはいいてんきですね", 100.0)])
    clips = [{"uid": 0, "sub": 0, "text": "今日はいいてんきですね。"}]
    got = anchor_clips(edl, clips)
    assert got, "整形された文が照合できていない"
