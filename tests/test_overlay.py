"""ユーザー配置オーバーレイ: 色解決・出力時刻変換・二重縁取りASS生成のテスト。"""
import re

import pytest

from wwedit.compose.overlay import (
    Placed,
    build_overlay_ass,
    image_overlays,
    overlays_to_output,
    place_overlays,
    resolve_color,
)
from wwedit.edl.schema import Overlay, TimeRange
from wwedit.subtitle.ass import MAIN_PALETTE, WHITE_RING


def _ov(**kw):
    base = dict(id="a", kind="text", start=10.0, end=20.0, text="テスト")
    base.update(kw)
    return Overlay(**base)


def _pl(o, *, mag=1.0, out_w=1920, out_h=1080):
    """crop 無し（ソース＝出力・mag=1）で配置した Placed。座標写像を挟まない素の見え方。"""
    return Placed(o=o, start=o.start, end=o.end, x=o.x * out_w, y=o.y * out_h,
                  w=o.w * out_w, h=o.h * out_h, mag=mag)


def _pls(ovs, **kw):
    return [_pl(o, **kw) for o in ovs]


def test_resolve_color_palette_and_hex():
    assert resolve_color("blue") == MAIN_PALETTE["blue"]
    assert resolve_color("purple") == MAIN_PALETTE["purple"]
    # #RRGGBB は ASS の BGR 順(&H00BBGGRR)へ
    assert resolve_color("#FF8000") == "&H000080FF"
    assert resolve_color("ff8000") == "&H000080FF"
    # 未知指定は青へフォールバック（合成を止めない）
    assert resolve_color("なにこれ") == MAIN_PALETTE["blue"]


def test_overlays_to_output_shifts_by_cuts():
    # [0,10) をカット → keep は [10,60)。ソース20〜30秒は出力10〜20秒になる。
    ranges = [TimeRange(start=10.0, end=60.0)]
    out = overlays_to_output([_ov(start=20.0, end=30.0)], ranges)
    assert len(out) == 1
    assert round(out[0].start, 3) == 10.0
    assert round(out[0].end, 3) == 20.0
    # 元オブジェクトは変更しない（非破壊）
    assert _ov(start=20.0, end=30.0).start == 20.0


def test_overlays_fully_inside_cut_are_dropped():
    # keep が [50,60) だけ → [20,30) は完全にカット域なので出力尺ゼロ＝除外
    ranges = [TimeRange(start=50.0, end=60.0)]
    assert overlays_to_output([_ov(start=20.0, end=30.0)], ranges) == []


def test_build_overlay_ass_double_border_two_layers():
    ass = build_overlay_ass([_pl(_ov(color="purple", size=80))], play_w=1920, play_h=1080)
    # 二重枠＝L0(同色の外枠)とL1(白の1次枠)の2スタイル・2イベント
    assert "o0L0" in ass and "o0L1" in ass
    assert ass.count("Dialogue:") == 2
    assert WHITE_RING in ass                      # 1次枠線は白固定
    assert MAIN_PALETTE["purple"] in ass          # 文字色・外枠は指定色
    assert "Dialogue: 0," in ass and "Dialogue: 1," in ass


def test_build_overlay_ass_single_border_when_disabled():
    ass = build_overlay_ass([_pl(_ov(double_border=False))])
    assert ass.count("Dialogue:") == 1
    assert "o0L0" not in ass


def test_build_overlay_ass_positions_with_an7():
    ass = build_overlay_ass([_pl(_ov(x=0.25, y=0.5))], play_w=1920, play_h=1080)
    assert "\\pos(480,540)" in ass   # 正規化座標×解像度
    # Alignment=7（左上基準）でスタイル行が出ている
    assert ",7,0,0,0,1" in ass


def test_build_overlay_ass_shares_style_for_same_look():
    ovs = [_ov(id="a", color="blue", size=64), _ov(id="b", color="blue", size=64)]
    ass = build_overlay_ass(_pls(ovs))
    assert "o1L1" not in ass          # 同じ見た目はスタイルを共有
    assert ass.count("Dialogue:") == 4  # 2件×2レイヤー


def test_empty_text_and_image_are_excluded_from_ass():
    ovs = [_ov(text="   "), _ov(id="i", kind="image", path="x.png")]
    assert "Dialogue:" not in build_overlay_ass(_pls(ovs))


def test_image_overlays_requires_path():
    ovs = [
        Overlay(id="i", kind="image", start=0, end=5, path="/tmp/a.png"),
        Overlay(id="j", kind="image", start=0, end=5, path=""),
        _ov(),
    ]
    got = image_overlays(_pls(ovs))
    assert [p.o.id for p in got] == ["i"]


def test_outline_widths_are_per_overlay():
    """枠の太さは Overlay ごとに反映される（0.01刻みの調整が合成へ届く）。"""
    ass = build_overlay_ass([_pl(_ov(white_ring=3.25, outer_outline=12.5))])
    # Style行の Outline 欄（BorderStyle=1 の次）に指定値が入る
    assert ",1,12.5,0," in ass    # L0 = 外枠
    assert ",1,3.25,0," in ass    # L1 = 白の1次枠


def test_outline_widths_split_styles():
    """同色・同サイズでも枠太さが違えば別スタイルになる。"""
    ovs = [_ov(id="a", white_ring=5.0), _ov(id="b", white_ring=2.0)]
    ass = build_overlay_ass(_pls(ovs))
    assert "o1L1" in ass          # 共有されず2スタイル
    assert ass.count("Dialogue:") == 4


def test_outer_outline_ignored_when_single_border():
    ass = build_overlay_ass([_pl(_ov(double_border=False, outer_outline=30.0))])
    assert ",1,30,0," not in ass and ",1,30.0,0," not in ass
    assert ass.count("Dialogue:") == 1


def _ys(lines):
    """Dialogue 行から \\pos の y 座標を順に取り出す。"""
    return [int(re.search(r"\\pos\(\d+,(\d+)\)", ln).group(1)) for ln in lines]


def test_multiline_text_splits_per_line_with_advance():
    """複数行は行ごとに別イベントになり、y が一定の行送りで増える（枠の被り回避）。"""
    ass = build_overlay_ass([_pl(_ov(text="1行目\n2行目\n3行目", double_border=False))])
    body = [x for x in ass.splitlines() if x.startswith("Dialogue:")]
    assert len(body) == 3                       # 行ごとに1イベント
    ys = _ys(body)
    d = ys[1] - ys[0]
    assert d > 0 and abs((ys[2] - ys[1]) - d) <= 1   # ほぼ等間隔（整数丸め±1px）
    assert all(t in ass for t in ("1行目", "2行目", "3行目"))


def test_line_spacing_scales_advance():
    """line_spacing を上げると行送りが比例して広がる。"""
    base = _ys([x for x in build_overlay_ass(
        [_pl(_ov(text="a\nb", line_spacing=1.0))]).splitlines() if x.startswith("Dialogue:")])
    wide = _ys([x for x in build_overlay_ass(
        [_pl(_ov(text="a\nb", line_spacing=2.0))]).splitlines() if x.startswith("Dialogue:")])
    # 二重枠なので各行2レイヤー→[L0行0,L1行0,L0行1,L1行1]。行送り=行1のy-行0のy
    assert (wide[2] - wide[0]) == pytest.approx((base[2] - base[0]) * 2, abs=2)


def test_line_advance_accounts_for_border():
    """1.0倍でも枠の太さぶん行送りが増える（枠を織り込んで被らない）。"""
    from wwedit.compose.overlay import line_advance
    thin = line_advance(64, border=5.0, spacing=1.0)
    thick = line_advance(64, border=25.0, spacing=1.0)
    assert thick > thin + 30      # 太い枠ほど行送りが広い


def test_text_align_maps_to_ass_alignment():
    for al, an in (("left", 7), ("center", 8), ("right", 9)):
        ass = build_overlay_ass([_pl(_ov(text="x", align=al, double_border=False))])
        style = [x for x in ass.splitlines() if x.startswith("Style: o0L1")][0]
        # Alignment は Style 行の末尾側 ...,BorderStyle,Outline,Shadow,Alignment,...
        assert f",1,5,0,{an}," in style


# ── モザイク（最上位・bbox形式） ─────────────────────────────────────
def _mos(**kw):
    base = dict(id="m1", kind="mosaic", start=1.0, end=5.0, x=0.3, y=0.2, w=0.25, h=0.2)
    base.update(kw)
    return Overlay(**base)


def test_mosaic_region_px_clamps_inside_frame():
    from wwedit.compose.overlay import mosaic_region_px
    o = _mos(x=0.9, y=0.9, w=0.5, h=0.5)          # 右下にはみ出す指定
    rx, ry, rw, rh = mosaic_region_px(_pl(o), 1920, 1080)
    assert rx + rw <= 1920 and ry + rh <= 1080     # フレーム内へ収める
    assert rw >= 2 and rh >= 2


def test_mosaic_effect_pixelate_vs_gaussian():
    from wwedit.compose.overlay import mosaic_effect_filter
    # pixelate は 元寸(600×400)へ拡大し直す（縮小後サイズのままにしない）
    f = mosaic_effect_filter(_mos(mosaic_type="pixelate", strength=20), 600, 400)
    assert "flags=neighbor" in f and "scale=600:400:flags=neighbor" in f
    g = mosaic_effect_filter(_mos(mosaic_type="gaussian", strength=8), 600, 400)
    assert "gblur=sigma=" in g


def test_build_mosaic_chains_rect_splits_and_overlays():
    from wwedit.compose.overlay import build_mosaic_chains
    chains, last = build_mosaic_chains([_pl(_mos())], "outv", 1920, 1080)
    joined = "\n".join(chains)
    assert "split[mb0][mc0]" in joined
    assert "crop=" in joined and "overlay=" in joined
    assert "enable='between(t,1.000,5.000)'" in joined
    assert last == "mout0"


def test_build_mosaic_chains_ellipse_uses_mask_alphamerge():
    from wwedit.compose.overlay import build_mosaic_chains
    chains, _ = build_mosaic_chains(
        [_pl(_mos(shape="ellipse"))], "outv", 1920, 1080, mask_input_of={0: 3})
    joined = "\n".join(chains)
    assert "[3:v]format=gray" in joined and "alphamerge" in joined
    assert "format=rgba" in joined


def test_mosaic_chains_empty_returns_prev():
    from wwedit.compose.overlay import build_mosaic_chains
    assert build_mosaic_chains([], "outv", 1920, 1080) == ([], "outv")


# ── レイヤー順（合成の filtergraph）─────────────────────────────────────────
# モザイクを最上位に置くと収録日リボンや字幕までぼける。掛かってよいのは
# 「映像＋ユーザー画像」だけで、文字情報/UI はモザイクより上に来ること。
def _layer_edl():
    from wwedit.edl.schema import Chapter, Edl, Segment, SourceMedia, Subtitle

    return Edl(
        recording_dir="2026-07-23",
        source=SourceMedia(video_path="src.mp4", fps=25, width=1920, height=1080,
                           duration_s=60.0),
        segments=[Segment(id="s0", start=0.0, end=60.0, invalid=False)],
        subtitles=[Subtitle(start=1.0, end=5.0, text="字幕", speaker="mossan-hoshi")],
        chapters=[Chapter(start_at=0.0, chapter_title="章", speaker="mossan-hoshi")],
        overlays=[
            Overlay(id="im", kind="image", start=1.0, end=5.0, path="pic.png",
                    x=0.1, y=0.1),
            Overlay(id="mo", kind="mosaic", start=1.0, end=5.0,
                    x=0.2, y=0.2, w=0.3, h=0.3),
            Overlay(id="tx", kind="text", start=1.0, end=5.0, text="重ね文字"),
        ],
    )


def _capture_filter_script(monkeypatch, tmp_path, **kw):
    """ffmpeg を実行せず、compose_kept が組んだ filtergraph 本文を取り出す。"""
    from wwedit.compose import ffmpeg_compose as fc

    seen: dict[str, str] = {}

    def fake_run(cmd, **_kw):
        path = cmd[cmd.index("-filter_complex_script") + 1]
        seen["script"] = open(path, encoding="utf-8").read()
        seen["vmap"] = cmd[cmd.index("-map") + 1]

        class R:
            returncode = 0
            stderr = ""
        return R()

    monkeypatch.setattr(fc.subprocess, "run", fake_run)
    fc.compose_kept(_layer_edl(), tmp_path / "out.mp4", audio="embedded", **kw)
    return seen


def test_mosaic_is_below_text_ui_layers(monkeypatch, tmp_path):
    seen = _capture_filter_script(
        monkeypatch, tmp_path,
        subtitles=True, chapter_ribbon=True, ribbon_date="7/23収録", overlays=True)
    s = seen["script"]
    order = [s.index(m) for m in ("[ovo0]", "[mout0]", "ass=subs.ass", "[outvr]",
                                  "ass=overlays.ass")]
    # 画像 → モザイク → 字幕 → リボン → テキスト重ね の順に積まれている
    assert order == sorted(order), s
    assert seen["vmap"] == "[outvo]"  # 最終出力はテキスト重ねの後


def test_layers_still_chain_without_overlays(monkeypatch, tmp_path):
    # 重ねが無くても字幕→リボンの連結が切れない（ラベルの受け渡し回帰）
    seen = _capture_filter_script(
        monkeypatch, tmp_path,
        subtitles=True, chapter_ribbon=True, ribbon_date="7/23収録", overlays=False)
    assert "ass=subs.ass" in seen["script"]
    assert seen["vmap"] == "[outvr]"


# ── 座標系: 重ねは**ソースフレーム基準**で、crop 後の出力へ写像される ──────────
# エディタはソース映像の上に置くので、crop で寄っても素材の同じ場所に貼り付く。
def _crop_edl(bbox, *, dur=60.0):
    from wwedit.edl.schema import Edl, FramingRegion, Segment, SourceMedia

    return Edl(
        recording_dir="d",
        source=SourceMedia(video_path="v.mp4", width=1920, height=1080, duration_s=dur),
        segments=[Segment(id="s0", start=0.0, end=dur, invalid=False)],
        framing=[FramingRegion(start=0.0, end=dur, kind="static", bbox=bbox)],
    )


def test_place_overlays_identity_without_crop():
    """crop 無しなら ソース正規化 × 出力解像度＝そのまま（従来と同じ見え方）。"""
    segs = [(0.0, 60.0, None)]
    p = place_overlays([_ov(x=0.25, y=0.5)], segs,
                       src_w=1920, src_h=1080, out_w=1920, out_h=1080)[0]
    assert (p.x, p.y) == (480.0, 540.0)
    assert p.mag == 1.0


def test_place_overlays_maps_through_crop():
    """crop=(480,270,960,540) は中央を2倍に寄せる。ソース中央の点は出力中央へ。"""
    segs = [(0.0, 60.0, (480, 270, 960, 540))]
    p = place_overlays([_ov(x=0.5, y=0.5)], segs,
                       src_w=1920, src_h=1080, out_w=1920, out_h=1080)[0]
    assert (round(p.x), round(p.y)) == (960, 540)   # ソース中央 = 出力中央
    assert p.mag == 2.0                              # 2倍に拡大される
    # crop 左上のソース座標は出力の原点へ
    q = place_overlays([_ov(x=480 / 1920, y=270 / 1080)], segs,
                       src_w=1920, src_h=1080, out_w=1920, out_h=1080)[0]
    assert (round(q.x), round(q.y)) == (0, 0)


def test_place_overlays_drops_offscreen_after_crop():
    """crop で画面外へ出た重ねは落とす（描いても見えない）。"""
    segs = [(0.0, 60.0, (960, 540, 960, 540))]   # 右下だけを使う
    assert place_overlays([_ov(x=0.05, y=0.05)], segs,
                          src_w=1920, src_h=1080) == []


def test_place_overlays_splits_per_crop_segment():
    """crop が変わる区間をまたぐ重ねは、区間ごとに別の配置へ分割される。"""
    segs = [(0.0, 5.0, None), (5.0, 10.0, (480, 270, 960, 540))]
    got = place_overlays([_ov(start=0.0, end=10.0, x=0.5, y=0.5)], segs,
                         src_w=1920, src_h=1080)
    assert len(got) == 2
    assert [(p.start, p.end) for p in got] == [(0.0, 5.0), (5.0, 10.0)]
    assert got[0].mag == 1.0 and got[1].mag == 2.0


def test_output_crop_segments_merges_same_bbox():
    """隣接 keep 区間の crop が同じならフィルタを増やさないよう1つに畳む。"""
    from wwedit.compose.overlay import output_crop_segments

    edl = _crop_edl((0, 0, 960, 540))
    segs = output_crop_segments(edl, [TimeRange(start=0.0, end=10.0),
                                      TimeRange(start=20.0, end=30.0)])
    assert segs == [(0.0, 20.0, (0, 0, 960, 540))]   # 出力上は連続＝1区間


def test_crop_magnifies_text_and_mosaic_strength():
    """ソース基準の見かけを保つため、文字サイズ・枠・モザイクの粗さにも mag が掛かる。"""
    from wwedit.compose.overlay import mosaic_effect_filter

    ass = build_overlay_ass([_pl(_ov(size=40, white_ring=4.0), mag=2.0)])
    assert ",80," in ass          # Fontsize 40 → 80
    assert ",1,8,0," in ass       # 白1次枠 4.0 → 8
    # pixelate のブロックも2倍（crop で寄っても見た目の粗さが変わらない）
    f = mosaic_effect_filter(_mos(strength=10), 600, 400, 2.0)
    assert "scale=30:20:flags=neighbor" in f   # 600/20, 400/20
