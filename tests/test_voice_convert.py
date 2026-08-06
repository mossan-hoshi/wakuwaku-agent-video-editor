"""publish.voice_convert / publish.seedvc（[V] 方式A）のテスト。ffmpeg/Seed-VC は実行しない。"""

from __future__ import annotations

import json
from pathlib import Path

from wwedit.edl.schema import Edl, SourceMedia, SpeakerTrack, Utterance, Word
from wwedit.publish.seedvc import plan_ref_concat
from wwedit.publish.voice_convert import pending_chunks, speech_spans


def _edl(utts: list[Utterance], duration: float = 100.0) -> Edl:
    return Edl(
        recording_dir="2026-01-01",
        source=SourceMedia(
            video_path="v.mp4", duration_s=duration,
            audio_tracks=[SpeakerTrack(speaker="A", path="a.m4a")],
        ),
        utterances=utts,
    )


def _u(speaker: str, start: float, end: float, words: list[tuple[float, float]] = ()) -> Utterance:
    return Utterance(
        speaker=speaker, text="x", start=start, end=end,
        words=[Word(text="w", start=s, end=e) for s, e in words],
    )


def test_speech_spans_pad_and_merge():
    edl = _edl([_u("A", 10.0, 12.0), _u("A", 13.0, 15.0), _u("A", 30.0, 31.0)])
    spans = speech_spans(edl, "A", pad=0.5, merge_gap=1.5)
    # 10-12 と 13-15 は pad 込みで隙間 <1.5s → マージ。30 は別チャンク
    assert len(spans) == 2
    assert spans[0].start == 9.5 and spans[0].end == 15.5
    assert spans[1].start == 29.5 and spans[1].end == 31.5


def test_speech_spans_clamps_to_source():
    edl = _edl([_u("A", 0.2, 1.0), _u("A", 99.5, 100.0)], duration=100.0)
    spans = speech_spans(edl, "A", pad=0.5)
    assert spans[0].start == 0.0          # 頭は0でクランプ
    assert spans[-1].end == 100.0         # 尻は尺でクランプ


def test_speech_spans_ignores_other_speaker():
    edl = _edl([_u("A", 1.0, 2.0), _u("B", 5.0, 6.0)])
    assert len(speech_spans(edl, "A")) == 1
    assert speech_spans(edl, "C") == []


def test_speech_spans_splits_long_utterance_at_word_gap():
    # 60秒の1発話（語間に 30.0-30.8 の大ギャップ）→ max_len=45 で2分割
    words = [(i, i + 0.8) for i in range(0, 30)] + [(i, i + 0.8) for i in range(31, 60)]
    edl = _edl([_u("A", 0.0, 60.0, words)])
    spans = speech_spans(edl, "A", pad=0.0, max_len=45.0)
    assert len(spans) >= 2
    assert all(s.duration <= 45.0 for s in spans)


def test_speech_spans_merge_respects_max_len():
    # 隣接発話でも合計が max_len を超えるならマージしない
    edl = _edl([_u("A", 0.0, 40.0, [(0.0, 40.0)]), _u("A", 41.0, 80.0, [(41.0, 80.0)])])
    spans = speech_spans(edl, "A", pad=0.5, merge_gap=1.5, max_len=45.0)
    assert len(spans) == 2


def test_plan_ref_concat_stops_at_target():
    assert plan_ref_concat([11.5, 7.7, 9.9, 12.0], target=24.0) == 3  # 11.5+7.7+9.9=29.1>=24
    assert plan_ref_concat([30.0], target=24.0) == 1                  # 1本で足りる
    assert plan_ref_concat([5.0, 5.0], target=24.0) == 2              # 足りなくても全部


def test_pending_chunks_by_out_existence(tmp_path: Path):
    done = tmp_path / "done.wav"
    done.write_bytes(b"x")
    manifest = {"chunks": [
        {"id": "a", "out": str(done)},
        {"id": "b", "out": str(tmp_path / "missing.wav")},
    ]}
    assert [c["id"] for c in pending_chunks(manifest)] == ["b"]


def test_convert_batch_spec_shape(monkeypatch, tmp_path: Path):
    """convert_batch が jobs.json を正しく組み、results.json を読み戻すこと（subprocess注入）。"""
    from wwedit.publish import seedvc

    captured: dict = {}

    def fake_run(cmd, **kw):
        spec_path = Path(cmd[3])
        res_path = Path(cmd[4])
        captured["spec"] = json.loads(spec_path.read_text(encoding="utf-8"))
        captured["cwd"] = kw.get("cwd")
        res_path.write_text(json.dumps(
            [{"out": j["out"], "duration_sec": 1.0} for j in captured["spec"]["jobs"]]
        ), encoding="utf-8")

        class R:
            returncode = 0
            stderr = stdout = ""
        return R()

    monkeypatch.setattr(seedvc.subprocess, "run", fake_run)
    monkeypatch.setattr(seedvc.Path, "exists", lambda self: True)
    results = seedvc.convert_batch(
        [{"source": "s.wav", "target": "t.wav", "out": "o.wav"}], diffusion_steps=25)
    assert captured["spec"]["diffusion_steps"] == 25
    assert captured["spec"]["jobs"][0]["source"] == "s.wav"
    assert captured["cwd"] == captured["spec"]["seedvc_dir"]  # CWD=seed-vcルート必須
    assert results[0]["duration_sec"] == 1.0
