"""chibi.emotion（感情割当の入出力）のテスト。"""

from __future__ import annotations

import json
from pathlib import Path

from wwedit.chibi.emotion import apply_emotion_decisions, write_emotion_input
from wwedit.edl.schema import Edl, Segment, SourceMedia, SpeakerTrack, Utterance


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
        segments=[
            Segment(id="s0", start=0.0, end=10.0),
            Segment(id="s1", start=10.0, end=20.0, invalid=True),
            Segment(id="s2", start=20.0, end=100.0),
        ],
        utterances=[
            Utterance(speaker="A", text="こんにちは", start=1.0, end=3.0),
            Utterance(speaker="A", text="カット内", start=12.0, end=15.0),
            Utterance(speaker="A", text="続きです", start=61.0, end=63.0),
        ],
    )


def test_write_emotion_input_kept_only(tmp_path: Path):
    tsv = tmp_path / "in.tsv"
    n = write_emotion_input(_edl(), tsv)
    assert n == 2  # カット内発話は除外
    lines = tsv.read_text(encoding="utf-8").splitlines()
    assert lines[0].split("\t") == ["key", "time", "speaker", "audio", "text"]
    # key は <utterance添字>:<有声区間番号>。word が無い発話は発話まるごとで1区間
    assert lines[1].split("\t")[0] == "0:0"
    # idx2 の出力時刻: 61.0 → 10 + (61-20) = 51.0 → "00:51"
    row2 = lines[2].split("\t")
    assert row2[0] == "2:0" and row2[1] == "00:51"
    assert row2[3] == "-"          # 音声判定が無ければハイフン


def test_apply_emotion_decisions(tmp_path: Path):
    edl = _edl()
    dec = tmp_path / "d.json"
    dec.write_text(json.dumps({"emotions": [
        {"utt": 0, "emotion": "smile"},
        {"utt": 2, "emotion": "normal"},      # normal 明示 → None に戻す
        {"utt": 99, "emotion": "smile"},      # 範囲外 → 無視
        {"utt": 1, "emotion": "banana"},      # 未知 → 無視
    ]}), encoding="utf-8")
    edl.utterances[2].emotion = "angry"
    n = apply_emotion_decisions(edl, dec)
    assert n == 2
    assert edl.utterances[0].emotion == "smile"
    assert edl.utterances[1].emotion is None
    assert edl.utterances[2].emotion is None  # normal で明示リセット


def test_apply_emotion_decisions_writes_cues_with_timestamps(tmp_path: Path):
    """key 形式の決定は**時刻付きキュー**になる（実際に驚いた瞬間に表情が動く）。"""
    edl = _edl()
    dec = tmp_path / "d.json"
    dec.write_text(json.dumps({"emotions": [
        {"key": "0:0", "emotion": "surprised", "source": "audio", "score": 0.82},
        {"key": "2:0", "emotion": "normal"},        # normal はキューにしない
        {"key": "9:9", "emotion": "smile"},         # 存在しない区間 → 無視
    ]}), encoding="utf-8")
    n = apply_emotion_decisions(edl, dec)
    assert n == 2
    assert len(edl.emotion_cues) == 1
    c = edl.emotion_cues[0]
    assert (c.at, c.speaker, c.emotion, c.source) == (1.0, "A", "surprised", "audio")
