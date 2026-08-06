"""イントロ連結の章時刻シフトのテスト。

章をずらし忘れると概要欄の章が**全部早くなる**（#100 で実際に投稿してしまった）。
"""

from __future__ import annotations

from wwedit.publish.concat import shift_chapter_lines


def test_shift_moves_every_line_but_pins_the_first_to_zero() -> None:
    lines = [
        "00:00 オープニング",
        "00:20 比較紹介",
        "02:37 試聴と音質チェック",
        "14:17 まとめ",
    ]
    got = shift_chapter_lines(lines, 10.0)
    assert got == [
        "00:00 オープニング",  # 先頭は 00:00 固定（YouTubeの条件）
        "00:30 比較紹介",
        "02:47 試聴と音質チェック",
        "14:27 まとめ",
    ]


def test_shift_truncates_fractional_offset() -> None:
    # 切り捨て＝マーカーは章頭のわずか手前（アイキャッチのタイトルカードが見える）
    assert shift_chapter_lines(["00:00 a", "01:00 b"], 9.32) == ["00:00 a", "01:09 b"]


def test_shift_rolls_minutes_over() -> None:
    assert shift_chapter_lines(["00:00 a", "00:55 b"], 10.0) == ["00:00 a", "01:05 b"]


def test_non_timestamp_lines_pass_through() -> None:
    lines = ["00:00 a", "", "# メモ", "01:00 b"]
    assert shift_chapter_lines(lines, 5.0) == ["00:00 a", "", "# メモ", "01:05 b"]


def test_hh_mm_ss_form_is_supported() -> None:
    assert shift_chapter_lines(["00:00 a", "1:02:03 b"], 10.0) == ["00:00 a", "62:13 b"]
