"""概要欄のチャプター条件検査（#101 は先頭章9秒で章が全滅した）。"""

from __future__ import annotations

from wwedit.publish.description import (
    MIN_CHAPTER_SECONDS,
    chapter_problems,
    parse_timestamps,
)

VALID = """Agenda「テーマ」

#タグ

00:00 - start
00:21 - 章A
03:09 - 章B
1:05:34 - 章C
"""


def test_parse_timestamps_reads_mmss_and_hmmss():
    got = parse_timestamps(VALID)
    assert [s for s, _ in got] == [0, 21, 189, 3934]
    assert [lbl for _, lbl in got] == ["start", "章A", "章B", "章C"]


def test_valid_description_has_no_problems():
    assert chapter_problems(VALID) == []


def test_short_chapter_is_rejected():
    """#101 の実際の壊れ方: 00:00 → 00:09 が9秒で章リスト全体が無効化された。"""
    text = VALID.replace("00:21 - 章A", "00:09 - 章A")
    problems = chapter_problems(text)
    assert any(f"{MIN_CHAPTER_SECONDS} 秒以上必要" in p for p in problems)
    assert any("9 秒" in p for p in problems)


def test_boundary_ten_seconds_is_allowed():
    assert chapter_problems(VALID.replace("00:21 - 章A", "00:10 - 章A")) == []


def test_first_timestamp_must_be_zero():
    text = VALID.replace("00:00 - start", "00:05 - start")
    assert any("先頭が 00:00" in p for p in chapter_problems(text))


def test_fewer_than_three_chapters_is_rejected():
    text = "00:00 - start\n01:00 - 章A\n"
    assert any("個以上必要" in p for p in chapter_problems(text))


def test_descending_timestamps_are_rejected():
    text = VALID.replace("03:09 - 章B", "00:15 - 章B")
    assert any("昇順" in p for p in chapter_problems(text))


def test_fullwidth_timestamp_is_a_format_error():
    """全角数字/全角コロンは YouTube が時刻として読まない。"""
    text = VALID.replace("03:09 - 章B", "０３：０９ - 章B")
    assert any("書式が不正" in p for p in chapter_problems(text))


def test_plain_numeric_body_line_is_not_flagged():
    """数字始まりでもコロンが無ければ時刻行と誤判定しない。"""
    text = VALID.replace("Agenda「テーマ」", "2026年の話")
    assert chapter_problems(text) == []


def test_no_timestamps_at_all():
    assert chapter_problems("Agenda「テーマ」\n\n#タグ\n") == ["タイムスタンプ行がありません"]


def test_label_separator_variants_are_stripped():
    """``MM:SS ラベル``（ハイフン無し・ユーザーが Studio で直した形）も読む。"""
    got = parse_timestamps("00:00 start\n00:21 章A\n03:09 章B\n")
    assert got == [(0, "start"), (21, "章A"), (189, "章B")]
