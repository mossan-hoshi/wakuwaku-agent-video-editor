"""アイキャッチのタイトルは**英単語の途中で改行しない**。

由来: 2026-09-08 #107。章「崩壊現象とhappy-collapse-makerの誕生」が
「崩壊現象とhappy-co / llapse-makerの誕生」と割れた。均等分割のスコアだけで選ぶと
英字の途中が最も均等になることがある。
"""

from PIL import Image, ImageDraw, ImageFont

from wwedit.publish.eyecatch import _MEIRYO, _split2_balanced


def _draw():
    return ImageDraw.Draw(Image.new("RGB", (10, 10)))


def _has_midword_break(lines):
    """行末が ASCII 英数字で、次の行頭も ASCII 英数字なら語中改行。"""
    for a, b in zip(lines, lines[1:]):
        if a and b and a[-1].isascii() and a[-1].isalnum() \
                and b[0].isascii() and b[0].isalnum():
            return True
    return False


def test_no_break_inside_latin_word():
    title = "崩壊現象とhappy-collapse-makerの誕生"
    d = _draw()
    font = ImageFont.truetype(_MEIRYO, 84)
    # 1行では入らない幅を与えて 2行分割を強制する
    max_w = d.textlength(title, font=font) * 0.62
    lines = _split2_balanced(title, font, d, max_w)
    assert lines is not None and len(lines) == 2
    assert not _has_midword_break(lines), lines
    assert "".join(lines) == title


def test_still_splits_japanese_anywhere():
    title = "参照音声を破壊する崩壊デモ実演"
    d = _draw()
    font = ImageFont.truetype(_MEIRYO, 84)
    max_w = d.textlength(title, font=font) * 0.62
    lines = _split2_balanced(title, font, d, max_w)
    assert lines is not None and len(lines) == 2
    assert "".join(lines) == title


def test_falls_back_when_only_midword_split_fits():
    # 英字だけの長い語は割らないと入らない。**はみ出すより割る**方を選ぶ。
    title = "abcdefghijklmnopqrstuvwxyz"
    d = _draw()
    font = ImageFont.truetype(_MEIRYO, 84)
    max_w = d.textlength(title, font=font) * 0.55
    lines = _split2_balanced(title, font, d, max_w)
    assert lines is not None and "".join(lines) == title
