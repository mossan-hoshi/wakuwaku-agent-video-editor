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


def test_too_long_description_is_a_problem():
    """**概要欄の5,000字上限**。章を細かく刻むと本文より先にここへ当たる。

    由来: 2026-09-09、400話者の動画に1人1章を付けようとしたら章行だけで 6,876字になり、
    本文を1字も書かずに上限を超えた。条件（00:00/3個以上/昇順/10秒以上）は全部満たすので、
    ここで見ないと**投稿時まで気づけない**。
    """
    from wwedit.publish.description import MAX_DESCRIPTION_CHARS

    head = "Agenda「テーマ」\n\n#タグ\n\n00:00 - start\n"
    # 20秒間隔＝10秒条件は満たす章を並べる（引っかかるのは長さだけ、という状況を作る）
    lines = [f"{t // 60:02d}:{t % 60:02d} - 章{t}" for t in range(20, 20 * 30, 20)]
    text = head + "\n".join(lines) + "\n"
    assert chapter_problems(text) == []          # まだ短い

    text_long = text + "あ" * MAX_DESCRIPTION_CHARS
    problems = chapter_problems(text_long)
    assert any("上限" in p and "字ぶん減らす" in p for p in problems)


def test_length_check_counts_japanese_as_one_char():
    from wwedit.publish.description import MAX_DESCRIPTION_CHARS

    just_under = "あ" * (MAX_DESCRIPTION_CHARS - len(VALID))
    assert chapter_problems(VALID + just_under) == []
    assert any("上限" in p for p in chapter_problems(VALID + just_under + "あ"))
