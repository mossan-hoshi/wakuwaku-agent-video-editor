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


# ---- キャラテーマ色（キャラ声差し替え）----

def test_hex_to_ass_bgr_order():
    from wwedit.subtitle.ass import hex_to_ass

    assert hex_to_ass("#3FA9B5") == "&H00B5A93F"  # ASSは BGR 順
    assert hex_to_ass("EC4899") == "&H009948EC"   # #なしも可


def test_ass_to_rgb_round_trip():
    from wwedit.subtitle.ass import ass_to_rgb, hex_to_ass

    assert ass_to_rgb(hex_to_ass("#E0701F")) == (0xE0, 0x70, 0x1F)


def test_ensure_legible_lifts_dark_keeps_bright():
    import colorsys

    from wwedit.subtitle.ass import ensure_legible

    # kasumi の暗赤(#6B0716)は明度が引き上がる
    lifted = ensure_legible("#6B0716")
    assert lifted != "#6B0716"
    r, g, b = (int(lifted[i:i + 2], 16) / 255 for i in (1, 3, 5))
    _h, lightness, _s = colorsys.rgb_to_hls(r, g, b)
    assert lightness >= 0.40
    # noa の明るいティール(#3FA9B5)は不変
    assert ensure_legible("#3FA9B5").upper() == "#3FA9B5"


def test_resolve_color_key_all_forms():
    from wwedit.subtitle.ass import (
        CHAR_THEME_HEX,
        MAIN_PALETTE,
        char_subtitle_color,
        resolve_color_key,
    )

    assert resolve_color_key("blue") == MAIN_PALETTE["blue"]        # パレットキー
    assert resolve_color_key("noa") == char_subtitle_color("noa")   # キャラid
    assert resolve_color_key("#123456") == "&H00563412"             # 生hex
    assert resolve_color_key("unknown") is None                     # 未知
    assert resolve_color_key("") is None
    # 全キャラが解決できる
    for char in CHAR_THEME_HEX:
        assert resolve_color_key(char) is not None


def test_build_ass_with_char_color_double_border():
    from wwedit.edl.schema import Subtitle
    from wwedit.subtitle.ass import WHITE_RING, char_subtitle_color

    color = char_subtitle_color("suzu")
    ass = build_ass(
        [Subtitle(start=0.0, end=1.0, text="す", style="main", speaker="Taniguchi")],
        color_map={"Taniguchi": color},
    )
    # キャラ色でも二重枠仕様（色文字+白1次枠+同色外枠）は維持される
    l1 = next(ln for ln in ass.splitlines() if ln.startswith("Style: c") and "L1" in ln)
    assert f"{color},{color},{WHITE_RING}" in l1


def test_scheme_from_ass_same_hue_three_tones():
    from wwedit.compose.chapter_ribbon import scheme_from_ass
    from wwedit.subtitle.ass import char_subtitle_color

    dark, top, bottom = scheme_from_ass(char_subtitle_color("noa"))
    # 暗→明の3トーン（明度順: dark < bottom < top）
    assert sum(dark) < sum(bottom) < sum(top)
