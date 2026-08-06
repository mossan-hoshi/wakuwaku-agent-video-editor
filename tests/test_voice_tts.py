"""publish.voice_tts（[V] 方式B）のテスト。TTS/ffmpeg は実行しない。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wwedit.edl.schema import (
    Edl,
    Freeze,
    Segment,
    SourceMedia,
    SpeakerTrack,
    Subtitle,
    TimeRange,
    Utterance,
)
from wwedit.publish.voice_tts import (
    eligible_utterances,
    fit_plan,
    load_decisions,
    out_to_sigma_segments,
    place_clip,
    subtitles_from_reading,
    wrap_two_lines,
    write_tts_input,
)


def _edl() -> Edl:
    return Edl(
        recording_dir="2026-01-01",
        source=SourceMedia(
            video_path="v.mp4", duration_s=100.0,
            audio_tracks=[
                SpeakerTrack(speaker="A", path="a.m4a"),
                SpeakerTrack(speaker="B", path="b.m4a"),
                SpeakerTrack(speaker="B", path="pc.m4a", is_desktop_audio=True),
            ],
        ),
        segments=[
            Segment(id="s0", start=0.0, end=20.0),
            Segment(id="s1", start=20.0, end=30.0, invalid=True),
            Segment(id="s2", start=30.0, end=100.0),
        ],
        utterances=[
            Utterance(speaker="A", text="こんにちは", start=1.0, end=4.0),
            Utterance(speaker="B", text="はい", start=5.0, end=6.0),
            Utterance(speaker="A", text="カット内", start=22.0, end=25.0),  # 完全カット
            Utterance(speaker="A", text="再開です", start=31.0, end=33.0),
        ],
        character_cast={"A": "noa", "B": "suzu"},
    )


def test_eligible_excludes_cut_and_desktop():
    idxs = [i for i, _ in eligible_utterances(_edl())]
    assert idxs == [0, 1, 3]  # idx2 は完全カットで除外


def test_write_tts_input_slot_and_gap(tmp_path: Path):
    tsv = tmp_path / "in.tsv"
    n = write_tts_input(_edl(), tsv)
    assert n == 3
    lines = tsv.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("idx\t")
    # idx0: slot=3.0（1..4）、次の発話(idx1)開始5.0まで gap=1.0
    idx0 = lines[1].split("\t")
    assert idx0[0] == "0" and idx0[2] == "noa"
    assert float(idx0[3]) == pytest.approx(3.0)
    assert float(idx0[4]) == pytest.approx(1.0)
    # idx3: 出力時刻では 31.0→21.0。slot=2.0、末尾なので gap=総尺90-23=67
    idx3 = lines[3].split("\t")
    assert float(idx3[3]) == pytest.approx(2.0)
    assert float(idx3[4]) == pytest.approx(67.0)


def test_load_decisions_keeps_intentional_blanks(tmp_path: Path):
    """空文字は「読み上げない」という意図なので残す（キー無しだけが未決定）。"""
    p = tmp_path / "d.json"
    p.write_text(json.dumps({"lines": [
        {"idx": 0, "text": "こんにちは"},
        {"idx": 3, "text": "  "},          # 隣のターンへ文をまとめた＝読み上げない
    ]}), encoding="utf-8")
    assert load_decisions(p) == {0: "こんにちは", 3: ""}


def test_fit_plan_priority_order():
    # 無加工: budget=3+1-0.15=3.85 に収まる
    assert fit_plan(3.0, 1.0, 3.5)["action"] == "keep"
    # atempo: 3.85 < 4.2 <= 3.85*1.12
    fit = fit_plan(3.0, 1.0, 4.2)
    assert fit["action"] == "atempo" and 1.0 < fit["atempo"] <= 1.12
    # 短縮(未試行)
    assert fit_plan(3.0, 1.0, 6.0)["action"] == "shorten"
    # freeze(短縮済み): extra = 6.0 - 3.85
    fit = fit_plan(3.0, 1.0, 6.0, shorten_tried=True)
    assert fit["action"] == "freeze"
    assert fit["extra"] == pytest.approx(6.0 - 3.85, abs=1e-3)


def test_out_to_sigma_segments_no_freeze():
    ranges = [TimeRange(start=1.0, end=2.5), TimeRange(start=10.0, end=12.0)]
    segs = out_to_sigma_segments(ranges, [])
    assert segs == [(0.0, 1.5, 1.0), (1.5, 3.5, 10.0)]


def test_out_to_sigma_segments_with_freeze():
    ranges = [TimeRange(start=1.0, end=2.5), TimeRange(start=10.0, end=12.0)]
    segs = out_to_sigma_segments(ranges, [Freeze(at=1.5, extra=2.0)])
    # piece0: out[0,0.5+2.0) σ=1.0 / piece1: out[2.5,3.5) σ=1.5+2.0 / piece2: out[3.5,5.5) σ=12.0
    assert segs[0] == (0.0, 2.5, 1.0)
    assert segs[1] == (2.5, 3.5, 3.5)
    assert segs[2] == (3.5, 5.5, 12.0)


def test_place_clip_spans_hole():
    ranges = [TimeRange(start=0.0, end=10.0), TimeRange(start=20.0, end=30.0)]
    segs = out_to_sigma_segments(ranges, [])
    # 出力 8.0 から 5秒のクリップ → 前半2秒は σ8.0、後半3秒はカット穴を跨いで σ20.0
    pieces = place_clip(segs, 8.0, 5.0)
    assert pieces == [(0.0, 8.0, 2.0), (2.0, 20.0, 3.0)]


def test_place_clip_through_freeze():
    ranges = [TimeRange(start=0.0, end=10.0)]
    frz = [Freeze(at=6.0, extra=2.0)]
    segs = out_to_sigma_segments(ranges, frz)
    # freeze 境界で2pieceに分かれるが σ 座標は連続（5.0+3.0 → 8.0）＝音は途切れない
    pieces = place_clip(segs, 5.0, 4.0)
    assert pieces == [(0.0, 5.0, 3.0), (3.0, 8.0, 1.0)]
    assert pieces[0][1] + pieces[0][2] == pytest.approx(pieces[1][1])


def test_wrap_two_lines_splits_at_punctuation():
    assert wrap_two_lines("短い文", line_chars=20) == "短い文"       # 1行に収まれば改行しない
    t = wrap_two_lines("これは前半の文です、そしてこれが後半の文です", line_chars=20)
    assert t.count("\n") == 1                                        # 必ず2行以内
    assert t.split("\n")[0].endswith("、")                            # 句読点で割る
    # 句読点が無ければ機械分割。どちらの行も line_chars を超えない
    t2 = wrap_two_lines("あ" * 30, line_chars=20)
    assert t2.count("\n") == 1 and all(len(x) <= 20 for x in t2.split("\n"))


def test_subtitles_from_reading_uses_reading_text_and_clip_window():
    """方式Bは読み上げ文が確定しているので、字幕はWhisper由来ではなく読み上げ文そのもの。"""
    ranges = [TimeRange(start=0.0, end=100.0)]
    rows = [{"idx": 3, "speaker": "A", "u_start": 10.0, "u_end": 14.0,
             "tts_s": 4.0, "atempo": 1.0}]
    decisions = {3: "これは読み上げ文です、二枚に分かれる程度の長さがあります"}
    subs = subtitles_from_reading(rows, decisions, ranges, (), line_chars=10)
    assert subs, "字幕が生成されない"
    assert all(s.speaker == "A" for s in subs)
    # 読み上げ文の文字がそのまま出る（改行を除いて連結すると元文に一致）
    joined = "".join(s.text.replace("\n", "") for s in subs)
    assert joined == decisions[3].replace(" ", "")
    # クリップが鳴っている区間（10.0〜14.0）に収まる
    assert subs[0].start == pytest.approx(10.0, abs=0.05)
    assert subs[-1].end == pytest.approx(14.0, abs=0.05)
    assert all(s.text.count("\n") <= 1 for s in subs)   # 2行まで


def test_subtitles_from_reading_last_line_covers_freeze():
    """フリーズ中はソース時刻が進まないので、最後の字幕がフリーズ位置まで伸びる。"""
    ranges = [TimeRange(start=0.0, end=20.0)]
    frz = [Freeze(at=13.95, extra=3.0)]
    rows = [{"idx": 0, "speaker": "A", "u_start": 10.0, "u_end": 14.0,
             "tts_s": 7.0, "atempo": 1.0}]     # 4秒枠に7秒 → 3秒フリーズ
    subs = subtitles_from_reading(rows, {0: "あ" * 40}, ranges, frz, line_chars=10)
    assert subs[-1].end == pytest.approx(13.95, abs=0.06)  # フリーズ位置で頭打ち
    assert subs[-1].end > subs[-1].start


def test_resolve_overlaps_later_wins_and_drops_slivers():
    """話者違いの字幕が重なったら**後から始まる方が勝ち**、前は打ち切る。"""
    from wwedit.publish.voice_tts import resolve_overlaps

    subs = [
        Subtitle(start=0.0, end=20.0, text="長い方", speaker="A"),
        Subtitle(start=5.0, end=9.0, text="割り込み", speaker="B"),
        Subtitle(start=9.0, end=12.0, text="続き", speaker="B"),
        Subtitle(start=9.1, end=15.0, text="すぐ次", speaker="A"),   # 前を0.1秒に潰す
    ]
    out = resolve_overlaps(subs)
    assert [(s.start, s.end, s.speaker) for s in out] == [
        (0.0, 5.0, "A"),      # 割り込みの開始で打ち切り
        (5.0, 9.0, "B"),
        (9.1, 15.0, "A"),     # 9.0-12.0 の「続き」は 0.1秒に潰れるので捨てる
    ]


def test_subtitles_from_reading_has_no_overlap():
    """発話が重なっていても、出来上がる字幕は同時に1枚だけ。"""
    ranges = [TimeRange(start=0.0, end=200.0)]
    rows = [
        {"idx": 0, "speaker": "A", "u_start": 4.0, "u_end": 27.0, "tts_s": 20.0, "atempo": 1.0},
        {"idx": 1, "speaker": "B", "u_start": 6.0, "u_end": 32.0, "tts_s": 18.0, "atempo": 1.0},
    ]
    decisions = {0: "あ" * 120, 1: "い" * 100}
    subs = subtitles_from_reading(rows, decisions, ranges, ())
    assert subs
    for a, b in zip(subs, subs[1:], strict=False):
        assert b.start >= a.end - 1e-6, f"重なっている: {a} / {b}"


# ---- ターン分割（読み上げ単位）------------------------------------------------


def _edl_turns() -> Edl:
    """A が長い塊で喋り、その中に B のターンが挟まる（Whisper の話者別 utterance の実態）。"""
    from wwedit.edl.schema import Word

    def w(t, s, e):
        return Word(text=t, start=s, end=e)

    return Edl(
        recording_dir="2026-01-01",
        source=SourceMedia(
            video_path="v.mp4", duration_s=100.0,
            audio_tracks=[SpeakerTrack(speaker="A", path="a.m4a"),
                          SpeakerTrack(speaker="B", path="b.m4a")],
        ),
        segments=[Segment(id="s0", start=0.0, end=40.0),
                  Segment(id="s1", start=40.0, end=60.0, invalid=True),
                  Segment(id="s2", start=60.0, end=100.0)],
        utterances=[
            # A: 0-30 の塊。3語＋長い無音＋1語（カット区間 40-60 に1語）
            Utterance(speaker="A", text="ああいいうう", start=0.0, end=45.0, words=[
                w("あ", 0.0, 0.4), w("あい", 0.4, 1.0), w("いう", 1.0, 1.6),
                w("うえお", 20.0, 20.7),
                w("カットされる", 44.0, 45.0),      # invalid 区間なので落ちる
            ]),
            # B: A の塊の中に入るターン
            Utterance(speaker="B", text="かきくけ", start=5.0, end=6.5, words=[
                w("かき", 5.0, 5.5), w("くけ", 5.5, 6.5),
            ]),
        ],
        character_cast={"A": "noa", "B": "suzu"},
    )


def test_tts_units_splits_turns_and_drops_cut_words():
    from wwedit.publish.voice_tts import tts_units

    units = tts_units(_edl_turns())
    # A(0-1.6) → B(5.0-6.5) → A(20.0-) の3ターン。カット区間の語は入らない
    assert [u["speaker"] for u in units] == ["A", "B", "A"]
    assert [u["uid"] for u in units] == [0, 1, 2]
    assert "カットされる" not in "".join(u["text"] for u in units)
    assert units[0]["text"] == "ああいいう"
    assert units[2]["text"] == "うえお"


def test_tts_units_merges_same_speaker_when_nobody_interrupts():
    """相手が挟まらない同一話者の連続は繋ぐ（文の途中で割らない）。"""
    from wwedit.publish.voice_tts import tts_units

    edl = _edl_turns()
    edl.utterances = [edl.utterances[0]]          # B を消す＝A だけが喋る
    units = tts_units(edl)
    assert len(units) == 1
    assert units[0]["text"] == "ああいいううえお"


def test_kept_text_uses_only_surviving_words():
    from wwedit.publish.voice_tts import kept_text

    edl = _edl_turns()
    assert "カットされる" not in kept_text(edl.utterances[0], edl.kept_ranges())


def test_write_tts_input_is_turn_level(tmp_path: Path):
    tsv = tmp_path / "in.tsv"
    n = write_tts_input(_edl_turns(), tsv)
    assert n == 3
    rows = [ln.split("\t") for ln in tsv.read_text(encoding="utf-8").splitlines()[1:]]
    assert [r[0] for r in rows] == ["0", "1", "2"]
    assert [r[1] for r in rows] == ["A", "B", "A"]


# ---- 直列スケジュール ---------------------------------------------------------


def test_schedule_clips_removes_overlap_and_keeps_order():
    from wwedit.publish.voice_tts import schedule_clips

    # 希望位置が重なる3本（A の長い読みの中に B が入る）
    out = schedule_clips([(0.0, 10.0, "a"), (5.0, 3.0, "b"), (6.0, 2.0, "c")], gap=0.5)
    starts = [s for s, _, _ in out]
    assert starts[0] == 0.0
    for (s0, d0, _), (s1, _, _) in zip(out, out[1:], strict=False):
        assert abs(s1 - (s0 + d0 + 0.5)) < 1e-9   # 重ならない＋**固定**の間
    assert [k for _, _, k in out] == ["a", "b", "c"]


def test_schedule_clips_closes_long_silences():
    """元の会話の長い沈黙は残さない（間は固定・詳細は test_voice_tts_schedule.py）。"""
    from wwedit.publish.voice_tts import CLIP_GAP, schedule_clips

    out = schedule_clips([(0.0, 1.0, "a"), (30.0, 1.0, "b")])
    assert [s for s, _, _ in out] == [0.0, 1.0 + CLIP_GAP]


# ---- 用語表記（読みはカタカナ / 字幕は正式表記）--------------------------------


def test_load_terms_orders_longest_read_first(tmp_path: Path):
    from wwedit.publish.voice_tts import load_terms

    p = tmp_path / "t.json"
    p.write_text(json.dumps({"terms": [
        {"read": "リリア", "display": "Lyria"},
        {"read": "リリア3.5", "display": "Lyria 3.5"},
        {"read": "", "display": "捨てる"},
    ]}, ensure_ascii=False), encoding="utf-8")
    assert load_terms(p) == [("リリア3.5", "Lyria 3.5"), ("リリア", "Lyria")]
    assert load_terms(tmp_path / "ない.json") == []


def test_apply_terms_prefers_longer_reading():
    from wwedit.publish.voice_tts import apply_terms

    terms = [("リリア3.5", "Lyria 3.5"), ("リリア", "Lyria")]
    assert apply_terms("リリア3.5とリリアの比較", terms) == "Lyria 3.5とLyriaの比較"


def test_subtitles_from_reading_shows_canonical_notation():
    """読み上げはカタカナのまま、字幕だけ正式表記に戻す。"""
    ranges = [TimeRange(start=0.0, end=100.0)]
    rows = [{"idx": 0, "speaker": "A", "out_start": 10.0, "tts_s": 4.0, "atempo": 1.0}]
    decisions = {0: "リリア3.5とスノーAIを比較しました"}
    subs = subtitles_from_reading(
        rows, decisions, ranges, (),
        terms=[("リリア3.5", "Lyria 3.5"), ("スノーAI", "Suno AI")])
    joined = "".join(s.text.replace("\n", "") for s in subs)
    assert "Lyria 3.5" in joined and "Suno AI" in joined
    assert "リリア" not in joined and "スノー" not in joined
