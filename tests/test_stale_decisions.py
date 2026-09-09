"""カットを直したあとの台本・要約を、**いまのターン番号へ貼り直す**。

決定JSONは idx だけがキーなので、G2 でターンが増減すると**そこから後ろの番号が全部ずれ**、
**隣の発話の文を喋る**（2026-08-06 に実際に起きた。214→197ターンで133件が食い違い、
うち110件は「同じ文が別番号に居るだけ」だった）。
prepare 時の原文は入力TSVに残っているので、本文で照合すれば貼り直せる。

**落とすのは最後の手段**。対応が付かないターンは無音にせず、いまの kept 文字起こしを
読ませる（カット後に残った語だけで出来ているので、切った内容は原理的に入らない）。
"""

from __future__ import annotations

from wwedit.common.staleness import prepared_texts, realign, stale_indices
from wwedit.edl.schema import (
    Edl,
    Segment,
    SourceMedia,
    SpeakerTrack,
    Utterance,
    Word,
)
from wwedit.publish.voice_tts import realign_decisions, stale_turns, tts_units
from wwedit.subtitle.summarize import apply_captions, build_caption_windows, caption_remap


def _words(text: str, start: float, step: float = 0.4) -> list[Word]:
    return [Word(text=c, start=start + i * step, end=start + (i + 1) * step)
            for i, c in enumerate(text)]


#: 話者を A/B/C で回す。`tts_units` は**間に相手が居ない同一話者を繋ぎ直す**ので、
#: 2人で交互にすると1つ抜いた瞬間に隣どうしが合体してしまい、番号ずれを再現できない。
SPEAKERS = "ABC"


def _edl(texts: list[tuple[str, float]]) -> Edl:
    """全区間 kept の最小EDL（`_cut` で後からカットを足す）。"""
    utts = [Utterance(speaker=SPEAKERS[i % len(SPEAKERS)], start=t0,
                      end=t0 + len(tx) * 0.4, text=tx, words=_words(tx, t0))
            for i, (tx, t0) in enumerate(texts)]
    return Edl(
        recording_dir="2026-08-06",
        source=SourceMedia(video_path="v.mp4", duration_s=600.0, fps=25,
                           audio_tracks=[SpeakerTrack(speaker=sp, path=f"{sp}.m4a")
                                         for sp in SPEAKERS]),
        segments=[Segment(id="s0", start=0.0, end=600.0)],
        utterances=utts,
        character_cast={sp: "souta" for sp in SPEAKERS},
    )


def _cut(edl: Edl, spans: list[tuple[float, float]]) -> None:
    """指定区間を invalid にする（G2 の手修正に相当）。"""
    segs, t = [], 0.0
    for i, (lo, hi) in enumerate(spans):
        segs.append(Segment(id=f"k{i}", start=t, end=lo))
        segs.append(Segment(id=f"x{i}", start=lo, end=hi, invalid=True))
        t = hi
    segs.append(Segment(id="kz", start=t, end=600.0))
    edl.segments = [s for s in segs if s.end > s.start]


def _write_tsv(path, rows: dict[int, str], header: str) -> None:
    path.write_text(header + "\n" + "\n".join(f"{i}\tA\t{t}" for i, t in sorted(rows.items()))
                    + "\n", encoding="utf-8")


# --- 突き合わせと貼り直し -----------------------------------------------------

def test_matching_text_is_not_stale(tmp_path):
    tsv = tmp_path / "in.tsv"
    _write_tsv(tsv, {0: "きょうは いい 天気"}, "idx\tspeaker\ttext")
    assert stale_indices(prepared_texts(tsv), {0: "きょうはいい天気"}) == []


def test_changed_text_is_stale(tmp_path):
    tsv = tmp_path / "in.tsv"
    _write_tsv(tsv, {0: "有給を取って家族の予定があるんですよ"}, "idx\tspeaker\ttext")
    assert stale_indices(prepared_texts(tsv), {0: "あ、はい。"}) == [0]


def test_a_comment_line_is_not_a_row(tmp_path):
    """TSV の末尾には画面OCRの文脈が `#` 付きでぶら下がる。"""
    tsv = tmp_path / "in.tsv"
    tsv.write_text("# idx\tspeaker\ttext\n0\tA\tこんにちは\n\n# --- 画面テキスト ---\nLyria 3.5\n",
                   encoding="utf-8")
    assert prepared_texts(tsv) == {0: "こんにちは"}


def test_a_removed_turn_shifts_the_rest_and_realign_puts_them_back():
    """**これが本命**。途中の1ターンが消えると後ろが全部繰り上がる。"""
    prepared = {0: "おはようございます", 1: "きょうは雨ですね",
                2: "はい、すいません、以上です", 3: "ではまた来週"}
    current = {0: "おはようございます", 1: "はい、すいません、以上です", 2: "ではまた来週"}
    assert stale_indices(prepared, current) == [1, 2]      # 素の比較では2件が「古い」
    assert realign(prepared, current) == {0: 0, 1: 2, 2: 3}  # 貼り直せば取り違えない


def test_realign_never_crosses():
    """時間順どうしの対応なので、**後ろの文が前のターンへ**は行かない。"""
    prepared = {0: "あああああ", 1: "いいいいい", 2: "ううううう"}
    current = {0: "ううううう", 1: "あああああ"}
    m = realign(prepared, current)
    assert sorted(m) == sorted(set(m)) and list(m.values()) == sorted(m.values())


def test_a_turn_with_no_counterpart_is_left_unmapped():
    prepared = {0: "おはようございます", 1: "きょうは雨ですね"}
    current = {0: "おはようございます", 1: "まったく別の話をします"}
    assert realign(prepared, current) == {0: 0}


# --- 読み上げ（方式B） --------------------------------------------------------

def test_a_shifted_script_is_pasted_back_instead_of_dropped(tmp_path):
    edl = _edl([("おはようございます", 10.0), ("きょうはあめですね", 30.0),
                ("はいすいませんいじょうです", 50.0), ("ではまたらいしゅう", 70.0)])
    tsv = tmp_path / "voice_tts_input.tsv"
    _write_tsv(tsv, {u["uid"]: u["text"] for u in tts_units(edl)}, "idx\tspeaker\ttext")
    decisions = {0: "おはようございます。", 1: "今日は雨ですね。",
                 2: "はい、すいません、以上です。", 3: "ではまた来週。"}

    _cut(edl, [(29.0, 45.0)])                       # 2番目のターンを丸ごと切る
    units = tts_units(edl)
    assert [u["uid"] for u in units] == [0, 1, 2]
    assert stale_turns(units, tsv) == [1, 2]        # 番号だけがずれている

    got, lost, moved = realign_decisions(units, decisions, tsv)
    assert lost == [] and moved == 2
    assert got == {0: "おはようございます。", 1: "はい、すいません、以上です。",
                   2: "ではまた来週。"}, "隣の文を喋ってはいけない"


def test_a_turn_that_was_cut_away_falls_back_to_the_transcript(tmp_path):
    """発話のほとんどを切ったターン。**無音にせず**いまの文字起こしを読む。"""
    edl = _edl([("ゆうきゅうをとってかぞくのよていがあるんですよ", 10.0)])
    tsv = tmp_path / "voice_tts_input.tsv"
    _write_tsv(tsv, {u["uid"]: u["text"] for u in tts_units(edl)}, "idx\tspeaker\ttext")
    decisions = {0: "有給を取って家族の予定があるんですよ。"}

    _cut(edl, [(11.0, 600.0)])                      # 「ゆうきゅ」以外を切る
    units = tts_units(edl)
    got, lost, _moved = realign_decisions(units, decisions, tsv)
    assert lost == [u["uid"] for u in units]
    assert got == {}, "古い台本が残っている"
    # キーが無い＝tts_clips が「いまの kept 文字起こし」で読む（無音にはしない）
    from wwedit.publish.voice_tts import tts_clips
    assert "".join(c["text"] for c in tts_clips(units, got)) == units[0]["text"]


def test_keep_stale_leaves_the_numbering_alone(tmp_path):
    edl = _edl([("おはようございます", 10.0), ("きょうはあめですね", 30.0)])
    tsv = tmp_path / "voice_tts_input.tsv"
    _write_tsv(tsv, {u["uid"]: u["text"] for u in tts_units(edl)}, "idx\tspeaker\ttext")
    decisions = {0: "おはようございます。", 1: "今日は雨ですね。"}
    _cut(edl, [(9.0, 25.0)])
    got, lost, moved = realign_decisions(tts_units(edl), decisions, tsv, keep=True)
    assert (got, lost, moved) == (decisions, [], 0)


def test_a_missing_tsv_changes_nothing(tmp_path):
    """照合できないときに全部落とすと、TSV を消しただけで無音の動画が出来上がる。"""
    edl = _edl([("こんにちはこんにちは", 10.0)])
    units = tts_units(edl)
    decisions = {u["uid"]: "こんにちは。" for u in units}
    got, lost, moved = realign_decisions(units, decisions, tmp_path / "nope.tsv")
    assert (got, lost, moved) == (decisions, [], 0)


# --- 要約字幕 -----------------------------------------------------------------

def test_a_shifted_caption_window_is_pasted_back(tmp_path):
    edl = _edl([("あああああああああ", 10.0), ("いいいいいいいいい", 40.0),
                ("うううううううう", 70.0)])
    tsv = tmp_path / "caption_input.tsv"
    _write_tsv(tsv, {w["idx"]: w["text"] for w in build_caption_windows(edl)},
               "# idx\tout\tspeaker\ttext")

    _cut(edl, [(9.0, 39.0)])                        # 最初の窓を丸ごと切る
    remap = caption_remap(edl, tsv)
    assert remap == {1: 0, 2: 1}, "窓番号が繰り上がったぶんを吸収する"

    dec = tmp_path / "caption_decisions.json"
    dec.write_text('{"captions":[{"utt":0,"text":"最初の話"},{"utt":1,"text":"次の話"}]}',
                   encoding="utf-8")
    apply_captions(edl, dec, remap=remap, disclaimer=None)
    assert [s.text for s in edl.subtitles] == ["次の話"], "消えた窓の要約は捨てる"
    apply_captions(edl, dec, disclaimer=None)       # --keep-stale 相当
    assert [s.text for s in edl.subtitles] == ["最初の話", "次の話"]
