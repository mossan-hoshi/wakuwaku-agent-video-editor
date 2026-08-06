"""chibi.timeline / compose.chibi_overlay のテスト（ffmpeg 非実行）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from wwedit.chibi.timeline import (
    SpriteInterval,
    build_side_timeline,
    emotion_track,
    mouth_track,
    speaking_spans_from_report,
    speaking_spans_from_words,
    write_ffconcat,
)
from wwedit.edl.schema import (
    ChibiConfig,
    Edl,
    Segment,
    SourceMedia,
    SpeakerTrack,
    TimeRange,
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
                SpeakerTrack(speaker="B", path="b.m4a"),
            ],
        ),
        segments=[Segment(id="s0", start=0.0, end=100.0)],
        utterances=[
            Utterance(speaker="A", text="こん にちは", start=1.0, end=2.0,
                      words=[Word(text="こん", start=1.0, end=1.4),
                             Word(text="にちは", start=1.5, end=2.0)]),
            Utterance(speaker="A", text="続き", start=30.0, end=31.0, emotion="smile",
                      words=[Word(text="続き", start=30.0, end=31.0)]),
        ],
        character_cast={"A": "noa", "B": "suzu"},
        chibi=ChibiConfig(enabled=True),
    )


def test_speaking_spans_merges_word_gaps():
    spans = speaking_spans_from_words(_edl(), _edl().kept_ranges(), "A")
    # word間ギャップ0.1s(<0.25)はマージ → [1.0,2.0]。
    # 「続き」は2文字なので 2*0.22=0.44s で打ち切る（word end は次の語まで伸びている）
    assert spans == [(1.0, 2.0), (30.0, pytest.approx(30.44))]
    assert speaking_spans_from_words(_edl(), _edl().kept_ranges(), "B") == []


def test_voiced_word_spans_drops_punctuation_and_caps_length():
    """Whisper の word は隙間ゼロで、無音は句読点トークンに吸われる（実データで確認済み）。"""
    from wwedit.chibi.timeline import voiced_word_spans

    words = [
        Word(text="あい", start=0.0, end=0.4),
        Word(text="。", start=0.4, end=4.0),     # 無音を吸った句読点 → 捨てる
        Word(text="うえお", start=4.0, end=9.0),  # 3文字なのに5秒 → 0.66s で打ち切る
    ]
    spans = voiced_word_spans(words)
    assert spans[0] == (0.0, 0.4)
    assert len(spans) == 2
    assert spans[1][0] == 4.0 and spans[1][1] == pytest.approx(4.66)
    # 音声変換では打ち切りを大幅に緩める（小さい声・ゆっくりの発話を切らない側に倒す）
    from wwedit.edl.schema import VOICE_SEC_PER_CHAR

    loose = voiced_word_spans(words, max_sec_per_char=VOICE_SEC_PER_CHAR)
    assert loose[1][1] == pytest.approx(7.0)   # 3文字 × 1.0s
    assert len(loose) == 2                     # 句読点は緩めても捨てる


def test_speaking_spans_from_report_uses_clip_duration():
    rows = [
        {"speaker": "A", "u_start": 1.0, "tts_s": 3.0, "atempo": 1.0},
        {"speaker": "A", "u_start": 30.0, "tts_s": 2.2, "atempo": 1.1},
        {"speaker": "B", "u_start": 5.0, "tts_s": 1.0, "atempo": 1.0},
    ]
    spans = speaking_spans_from_report(rows, _edl().kept_ranges(), "A")
    assert spans[0] == (1.0, 4.0)
    assert spans[1] == (30.0, pytest.approx(32.0))  # 2.2/1.1=2.0s


def test_mouth_track_snaps_between_two_states():
    track = mouth_track([(1.0, 2.0)], total=3.0, step=0.1)
    assert track[0] == (0.0, 1.0, 0)          # スパン前=閉
    assert track[-1] == (2.0, 3.0, 0)         # スパン後=閉
    inner = [m for s, e, m in track if 1.0 <= s < 2.0]
    assert set(inner) == {0, 1}               # 中間フレームは作らない（閉/開のみ）
    assert inner[:3] == [1, 0, 1]             # MOUTH_WAVE の先頭
    assert inner[-1] == 0                     # スパン終端は閉に戻す
    # 全区間の隙間なし
    for (_s1, e1, _), (s2, _e2, _) in zip(track, track[1:], strict=False):
        assert e1 == pytest.approx(s2)


def test_emotion_track_is_short_reaction_not_persistent():
    """感情は発話の頭から数秒のリアクション。持続すると1つの surprised が何分も続く。"""
    from wwedit.chibi.timeline import EMOTION_HOLD_S

    edl = _edl()
    track = emotion_track(edl, edl.kept_ranges(), "A", 100.0)
    assert track[0][2] == "normal" and track[0][0] == 0.0
    smile = next(t for t in track if t[2] == "smile")
    assert smile[0] == pytest.approx(30.0)                      # 発話の頭から
    assert smile[1] == pytest.approx(min(31.0, 30.0 + EMOTION_HOLD_S))  # 発話終端で頭打ち
    assert [t[2] for t in track] == ["normal", "smile", "normal"]
    assert track[-1][2] == "normal" and track[-1][1] == pytest.approx(100.0)


def test_emotion_track_hold_clips_to_hold_seconds():
    edl = _edl()
    edl.utterances[1].end = 60.0                 # 長い発話でも hold 秒で normal に戻る
    edl.utterances[1].words = []
    track = emotion_track(edl, edl.kept_ranges(), "A", 100.0, hold=2.5)
    smile = next(t for t in track if t[2] == "smile")
    assert smile == (pytest.approx(30.0), pytest.approx(32.5), "smile")


def test_build_side_timeline_total_coverage():
    edl = _edl()
    total = 100.0
    ivs = build_side_timeline(edl, edl.kept_ranges(), "A", total=total, step=0.1)
    assert ivs[0].start == 0.0
    assert ivs[-1].end == pytest.approx(total)
    for a, b in zip(ivs, ivs[1:], strict=False):
        assert a.end == pytest.approx(b.start)  # 隙間なし
    # 発話中に感情とmouthが乗る
    assert any(iv.mouth > 0 for iv in ivs)
    assert any(iv.emotion == "smile" for iv in ivs)
    assert all(iv.eye is None for iv in ivs)  # 第一弾は eye 未使用


def test_write_ffconcat_durations_and_tail(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WWEDIT_CHIBI_ASSETS", str(tmp_path))
    ivs = [
        SpriteInterval(0.0, 1.0, "normal", 0),
        SpriteInterval(1.0, 1.5, "smile", 1),
        SpriteInterval(1.5, 3.0, "normal", 0),
    ]
    out = write_ffconcat(ivs, "noa", tmp_path / "c.ffconcat",
                         available_emotions={"normal", "smile"})
    text = out.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == "ffconcat version 1.0"
    durs = [float(ln.split()[1]) for ln in lines if ln.startswith("duration")]
    assert sum(durs) == pytest.approx(3.0)   # 合計=出力尺
    files = [ln for ln in lines if ln.startswith("file")]
    assert files[-1] == files[-2]            # 末尾フレーム重複
    assert "smile/mouth_open.png" in text and "normal/mouth_closed.png" in text


def test_write_ffconcat_falls_back_to_normal(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WWEDIT_CHIBI_ASSETS", str(tmp_path))
    ivs = [SpriteInterval(0.0, 1.0, "angry", 3)]
    out = write_ffconcat(ivs, "noa", tmp_path / "c.ffconcat",
                         available_emotions={"normal"})
    assert "/normal/" in out.read_text(encoding="utf-8")  # angry未生成→normalへ


def test_chibi_sides_default_and_override():
    from wwedit.compose.chibi_overlay import chibi_sides

    edl = _edl()
    sides = chibi_sides(edl)
    assert sides == [("left", "A", "noa"), ("right", "B", "suzu")]
    edl.chibi.sides = {"left": "B", "right": "A"}
    sides = chibi_sides(edl)
    assert sides == [("left", "B", "suzu"), ("right", "A", "noa")]


# ---- 方式B: 口パク・感情は「読み上げクリップの位置」に合わせる -------------------


def _report_rows():
    """finalize が書く形（out_start = 直列スケジュール後の確定位置）。"""
    return [
        {"idx": 0, "u_idx": 0, "speaker": "A", "out_start": 10.0, "tts_s": 3.0},
        {"idx": 1, "u_idx": 1, "speaker": "B", "out_start": 13.5, "tts_s": 2.0},
        {"idx": 2, "u_idx": 0, "speaker": "A", "out_start": 16.0, "tts_s": 2.0},
    ]


def test_speaking_spans_from_report_uses_scheduled_position():
    """元発話ではなく out_start を使う（方式Bは声と元タイミングが一致しない）。"""
    from wwedit.chibi.timeline import speaking_spans_from_report

    ranges = [TimeRange(start=0.0, end=100.0)]
    spans = speaking_spans_from_report(_report_rows(), ranges, "A")
    assert spans[0][0] == pytest.approx(10.0)
    assert spans[-1][1] == pytest.approx(18.0)
    b = speaking_spans_from_report(_report_rows(), ranges, "B")
    assert b == [(pytest.approx(13.5), pytest.approx(15.5))]


def test_emotion_track_from_report_anchors_on_clip_start():
    """感情も読み上げクリップの頭から hold 秒だけ（元発話の頭ではない）。"""
    from wwedit.chibi.timeline import emotion_track_from_report

    edl = Edl(
        recording_dir="2026-01-01",
        source=SourceMedia(video_path="v.mp4", duration_s=100.0),
        segments=[Segment(id="s0", start=0.0, end=100.0)],
        utterances=[
            Utterance(speaker="A", text="a", start=0.0, end=5.0, emotion="surprised"),
            Utterance(speaker="B", text="b", start=1.0, end=2.0),
        ],
    )
    ranges = [TimeRange(start=0.0, end=100.0)]
    track = emotion_track_from_report(edl, _report_rows(), ranges, "A", 30.0, hold=2.5)
    # 0-10 normal → 10-12.5 surprised → 12.5-30 normal
    assert track[0] == (0.0, pytest.approx(10.0), "normal")
    assert track[1] == (pytest.approx(10.0), pytest.approx(12.5), "surprised")
    assert track[-1][2] == "normal" and track[-1][1] == pytest.approx(30.0)
