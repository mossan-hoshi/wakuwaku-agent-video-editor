"""[E] 元の収録音声からの感情判定（chibi/audio_emotion.py）のテスト。

推論そのものは別プロセス（emotion2vec）なので、ここでは**区間の切り出しと写像**を見る。
"""

from __future__ import annotations

import json

from wwedit.chibi.audio_emotion import (
    EMOTION_FROM_AUDIO,
    audio_spans,
    load_audio_emotions,
    to_chibi_emotion,
)
from wwedit.edl.schema import (
    Edl,
    Segment,
    SourceMedia,
    SpeakerTrack,
    Utterance,
    Word,
)


def _edl() -> Edl:
    return Edl(
        recording_dir="2026-01-01",
        source=SourceMedia(
            video_path="v.mp4", duration_s=100.0,
            audio_tracks=[
                SpeakerTrack(speaker="A", path="a.m4a"),
                SpeakerTrack(speaker="A", path="pc.m4a", is_desktop_audio=True),
            ],
        ),
        segments=[Segment(id="s0", start=0.0, end=100.0)],
        utterances=[
            Utterance(speaker="A", text="こんにちは", start=1.0, end=6.0, words=[
                Word(text="こん", start=1.0, end=2.0),
                Word(text="にちは", start=2.0, end=3.0),
                Word(text="えっ", start=5.0, end=5.2),      # 短すぎ→捨てる
            ]),
            Utterance(speaker="B", text="別話者", start=10.0, end=12.0),   # トラック無し
        ],
    )


def test_audio_spans_are_voiced_spans_not_whole_utterances():
    """utterance まるごとだと相槌をまたぐ塊になり、頭で1回しか判定できない。"""
    got = audio_spans(_edl())
    assert [g["key"] for g in got] == ["0:0"]
    # 隣接 word は1区間に繋がる。end が 3.0 でなく 2.66 なのは voiced_word_spans が
    # word を「文字数×0.22秒」で切り詰めるため（見積りなので語尾を食わない側に倒す）
    assert (got[0]["start"], got[0]["end"]) == (1.0, 2.66)
    assert got[0]["speaker"] == "A"
    assert got[0]["wav"] == "a.m4a"                         # PC音声は対象外


def test_audio_spans_skip_spans_shorter_than_the_minimum():
    assert all(g["end"] - g["start"] >= 0.6 for g in audio_spans(_edl()))


def test_audio_spans_fall_back_to_the_whole_utterance_without_words():
    edl = _edl()
    edl.utterances[0].words = []
    got = audio_spans(edl)
    assert (got[0]["key"], got[0]["start"], got[0]["end"]) == ("0:0", 1.0, 6.0)


def test_to_chibi_emotion_maps_nine_classes_to_six():
    assert to_chibi_emotion({"top": "surprised", "score": 0.8}) == "surprised"
    assert to_chibi_emotion({"top": "fearful", "score": 0.8}) == "troubled"
    assert to_chibi_emotion({"top": "happy", "score": 0.8}) == "smile"
    assert to_chibi_emotion({"top": "neutral", "score": 0.9}) is None   # normal は付けない
    assert to_chibi_emotion({"top": "angry", "score": 0.3}) is None     # 弱い＝付けない
    assert "thinking" not in EMOTION_FROM_AUDIO.values()  # 音に対応物が無い＝テキスト側で拾う


def test_load_audio_emotions_is_keyed_and_tolerates_a_missing_file(tmp_path):
    p = tmp_path / "a.json"
    assert load_audio_emotions(p) == {}
    p.write_text(json.dumps([{"key": "1:0", "top": "happy", "score": 0.7}]),
                 encoding="utf-8")
    assert load_audio_emotions(p)["1:0"]["top"] == "happy"


def test_audio_spans_merge_nearby_pieces_so_there_is_enough_material():
    """word を切り詰めた細切れのままだと材料が足りない。0.6秒以内の隙間は繋ぐ。"""
    edl = _edl()
    alone = audio_spans(edl, merge_gap=0.0)[0]
    merged = audio_spans(edl)[0]
    assert alone["end"] - alone["start"] < 0.7            # 繋がないと 0.66秒しかない
    assert merged["end"] - merged["start"] > 1.6          # 繋ぐと材料が2.5倍になる
