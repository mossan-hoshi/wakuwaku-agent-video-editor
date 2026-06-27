from fractions import Fraction

from wwedit.common.timecode import format_rational, frames_to_seconds, parse_rational


def test_parse_rational():
    assert parse_rational("108/25s") == Fraction(108, 25)
    assert parse_rational("0s") == Fraction(0, 1)
    assert parse_rational("46962000/30000s") == Fraction(46962000, 30000)
    assert parse_rational("75135936/48000s") == Fraction(75135936, 48000)


def test_format_rational():
    assert format_rational(Fraction(108, 25), 25) == "108/25s"
    assert format_rational(1.0, 25) == "25/25s"
    # 30fps系の秒を25fpsタイムラインへ量子化
    assert format_rational(Fraction(46962000, 30000), 25) == "39135/25s"


def test_frames_to_seconds():
    assert frames_to_seconds(108, 25) == Fraction(108, 25)
