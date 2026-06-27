"""subtitle.ass（二重枠ASS生成）のテスト。"""

from __future__ import annotations

from wwedit.edl.schema import Subtitle
from wwedit.subtitle.ass import ass_escape, ass_time, build_ass


def test_ass_time_format():
    assert ass_time(0) == "0:00:00.00"
    assert ass_time(75.5) == "0:01:15.50"
    assert ass_time(3661.25) == "1:01:01.25"


def test_ass_time_rounding_carry():
    # cs 丸めで 100 になる桁上がり
    assert ass_time(1.999) == "0:00:02.00"


def test_ass_escape_newline_and_braces():
    assert ass_escape("a\nb") == "a\\Nb"
    assert "{" not in ass_escape("{x}") and "}" not in ass_escape("{x}")


def test_build_ass_two_layers_double_border():
    from wwedit.subtitle.ass import WHITE_RING

    subs = [
        Subtitle(start=1.0, end=2.0, text="こんにちは", style="main"),
        Subtitle(start=3.0, end=4.0, text="イントロ", style="intro"),
    ]
    color = "&H00379614"
    ass = build_ass(subs, default_color=color)
    # 二重枠＝各字幕が L0(外色枠)/L1(色文字+白枠) の2行（2字幕で4 Dialogue）
    assert ass.count("Dialogue:") == 4
    assert ",1,9" in ass  # 外枠 Outline=9
    assert ",1,5" in ass  # 白の1次枠線 Outline=5
    # 本編の色スタイル: fill=色/色, outline=白（白文字ではない）。動的style名 c?L1
    l1 = next(
        ln for ln in ass.splitlines()
        if ln.startswith("Style: c") and "L1" in ln and color in ln
    )
    assert f"{color},{color},{WHITE_RING}" in l1


def test_build_ass_has_header_and_resolution():
    ass = build_ass(
        [Subtitle(start=0.0, end=1.0, text="x", style="main")],
        play_w=1920, play_h=1080, font="Meiryo",
    )
    assert "PlayResX: 1920" in ass and "PlayResY: 1080" in ass
    assert "Meiryo" in ass
    assert "[V4+ Styles]" in ass and "[Events]" in ass


def test_colored_text_white_ring_same_color_outer():
    from wwedit.subtitle.ass import INTRO_COLOR, MAIN_PALETTE, WHITE_RING

    color = MAIN_PALETTE["green"]
    assert color != INTRO_COLOR  # 本編/イントロは色で差別化
    assert WHITE_RING == "&H00FFFFFF"  # 1次枠線=白固定
    ass = build_ass([Subtitle(start=0.0, end=1.0, text="m", style="main")], default_color=color)
    # 色スタイル: fill=色/色, outline=白（白文字ではない）
    style_lines = [ln for ln in ass.splitlines() if ln.startswith("Style: c")]
    assert all(color in ln for ln in style_lines)
    assert WHITE_RING in ass


def test_speaker_colors_cool_warm_split():
    from wwedit.subtitle.ass import COOL_KEYS, MAIN_PALETTE, WARM_KEYS, assign_speaker_colors

    cm = assign_speaker_colors(["mossan-hoshi", "Taniguchi"], "2026-06-04")
    cool = {MAIN_PALETTE[k] for k in COOL_KEYS}
    warm = {MAIN_PALETTE[k] for k in WARM_KEYS}
    assert cm["mossan-hoshi"] in cool   # sakamoto系=寒色
    assert cm["Taniguchi"] in warm      # taniguchi=暖色
    # 同一動画(同一key)では同一話者は同色
    assert assign_speaker_colors(["Taniguchi"], "2026-06-04")["Taniguchi"] == cm["Taniguchi"]


def test_build_ass_colors_by_speaker():
    from wwedit.subtitle.ass import MAIN_PALETTE

    subs = [
        Subtitle(start=0.0, end=1.0, text="A", style="main", speaker="mossan-hoshi"),
        Subtitle(start=1.0, end=2.0, text="B", style="main", speaker="Taniguchi"),
    ]
    cm = {"mossan-hoshi": MAIN_PALETTE["blue"], "Taniguchi": MAIN_PALETTE["red"]}
    ass = build_ass(subs, color_map=cm)
    assert MAIN_PALETTE["blue"] in ass and MAIN_PALETTE["red"] in ass  # 2話者2色


def test_pick_main_color_deterministic_from_palette():
    from wwedit.subtitle.ass import MAIN_PALETTE, pick_main_color

    c = pick_main_color("2026-06-04")
    assert c in MAIN_PALETTE.values()  # 4色パレットから
    assert pick_main_color("2026-06-04") == c  # 同じ収録は同じ色
