"""[I] 本編冒頭の要約インフォグラフィック（プロンプト＋配置）のテスト。API/ffmpeg は叩かない。"""

from __future__ import annotations

from pathlib import Path

import pytest

from wwedit.compose.infographic_overlay import (
    build_infographic_chain,
    fit_contain,
    safe_box,
)
from wwedit.edl.schema import (
    Chapter,
    Edl,
    InfographicConfig,
    Segment,
    SourceMedia,
    Subtitle,
)
from wwedit.publish.infographic import (
    SOURCE_MAX_RUNES,
    aspect_layout,
    build_prompt,
    build_source_text,
    subtitles_text,
)


def _edl() -> Edl:
    return Edl(
        recording_dir="2026-01-01",
        source=SourceMedia(video_path="v.mp4", duration_s=600.0),
        segments=[Segment(id="s0", start=0.0, end=600.0)],
        chapters=[
            Chapter(start_at=0.0, chapter_title="オープニング"),
            Chapter(start_at=120.0, chapter_title="Claude Code の新機能"),
            Chapter(start_at=400.0, chapter_title="まとめ"),
        ],
        subtitles=[
            Subtitle(start=1.0, end=3.0, text="今日は\nエージェントの話をします"),
            Subtitle(start=3.0, end=5.0, text="よろしくお願いします"),
        ],
    )


# ---- 入力テキストの組み立て ------------------------------------------------


def test_subtitles_text_flattens_newlines():
    assert subtitles_text(_edl()) == "今日は エージェントの話をします よろしくお願いします"


def test_build_source_text_sections_and_order():
    src = build_source_text(_edl(), title="AIエージェント入門", description="Agenda「入門」")
    # 骨子を決める順（タイトル→章→概要欄→字幕）で並ぶ
    order = [src.index(h) for h in ("# 動画タイトル", "# チャプター", "# 概要欄", "# 字幕全文")]
    assert order == sorted(order)
    assert "00:00 - オープニング" in src
    assert "Claude Code の新機能" in src


def test_build_source_text_truncates_from_the_tail():
    """上限で切られるのは末尾＝字幕側。タイトル・章立ては必ず残る。"""
    edl = _edl()
    edl.subtitles = [Subtitle(start=0.0, end=1.0, text="あ" * 500) for _ in range(40)]
    src = build_source_text(edl, title="タイトル", description="概要")
    assert len(src) == SOURCE_MAX_RUNES
    assert src.startswith("# 動画タイトル\nタイトル")
    assert "# チャプター" in src and "# 概要欄" in src


def test_build_source_text_empty_raises():
    edl = _edl()
    edl.chapters, edl.subtitles = [], []
    with pytest.raises(ValueError):
        build_source_text(edl)


# ---- プロンプト -------------------------------------------------------------


def test_aspect_layout_wide_vs_tall():
    assert "横長" in aspect_layout(1568, 672)
    assert "縦長" in aspect_layout(864, 1536)
    assert "正方形" in aspect_layout(1024, 1024)


def test_build_prompt_embeds_source_last_and_is_wide():
    src = build_source_text(_edl(), title="T")
    p = build_prompt(src, width=1568, height=672)
    assert "横長のキャンバス" in p
    assert p.index("# テキスト") < p.index("# 動画タイトル")   # 本文はテンプレ末尾
    assert "1枚の連続した情景として描かない" in p               # 図として組ませる指示


def test_build_prompt_truncates_whole_prompt():
    src = "あ" * 5000
    p = build_prompt(src, max_runes=1200)
    assert len(p) == 1200


# ---- 配置（上部UI/ちび/字幕に被らない） -------------------------------------


def test_safe_box_avoids_top_ui_and_bottom_widgets():
    cfg = InfographicConfig(path="x.png")
    x, y, w, h = safe_box(cfg, 1920, 1080)
    assert (x, y) == (48, 78)
    assert x + w == 1920 - 48
    assert y + h == 1080 - 352            # ちびキャラ(320+24)＋余白より上で終わる
    # ちびキャラの占有域（下端から344px）と重ならない
    assert y + h <= 1080 - (320 + 24)


def test_safe_box_scales_with_resolution():
    cfg = InfographicConfig(path="x.png")
    _, y, _, h = safe_box(cfg, 3840, 2160)
    assert y == 156 and y + h == 2160 - 704   # 1080p基準の予約値を高さ比で拡大


def test_fit_contain_keeps_aspect_and_centers():
    box = (48, 78, 1824, 650)
    x, y, w, h = fit_contain(2100, 900, box)          # 21:9 相当
    assert abs((w / h) - (2100 / 900)) < 0.02          # 縦横比を保つ
    assert w <= box[2] and h <= box[3]                 # 枠に収まる
    assert w % 2 == 0 and h % 2 == 0                   # yuv420 対策で偶数
    assert abs((x - box[0]) - (box[0] + box[2] - (x + w))) <= 1   # 左右中央


def test_fit_contain_does_not_upscale():
    x, y, w, h = fit_contain(400, 200, (0, 0, 1920, 1080))
    assert (w, h) == (400, 200)


# ---- filtergraph ------------------------------------------------------------


def test_build_infographic_chain_enable_window_and_fade():
    cfg = InfographicConfig(path="x.png", start_s=0.0, duration_s=10.0, fade_s=0.4)
    chains = build_infographic_chain(cfg, (150, 78, 1560, 650), 3, "base", "igo")
    assert chains[0].startswith("[3:v]scale=1560:650,format=rgba")
    assert "fade=t=in:st=0:d=0.4:alpha=1" in chains[0]
    assert "fade=t=out:st=9.6:d=0.4:alpha=1" in chains[0]
    assert chains[1].startswith("[base][igi]overlay=150:78")
    assert "enable='between(t,0.000,10.000)'" in chains[1]


def test_build_infographic_chain_fade_uses_absolute_output_time():
    """start_s があってもフェードは出力タイムラインの絶対秒（静止画入力の pts も0始まり）。"""
    cfg = InfographicConfig(path="x.png", start_s=5.0, duration_s=10.0, fade_s=1.0)
    chains = build_infographic_chain(cfg, (0, 0, 100, 100), 2, "b", "o")
    assert "fade=t=in:st=5:d=1:alpha=1" in chains[0]
    assert "fade=t=out:st=14:d=1:alpha=1" in chains[0]
    assert "enable='between(t,5.000,15.000)'" in chains[1]


def test_build_infographic_chain_no_fade():
    cfg = InfographicConfig(path="x.png", fade_s=0.0)
    chains = build_infographic_chain(cfg, (0, 0, 10, 10), 1, "b", "o")
    assert "fade" not in chains[0]


def test_edl_infographic_defaults_to_none():
    """既存EDLとの互換: 未設定なら None＝表示しない。"""
    assert _edl().infographic is None


# ---- compose_kept 統合（ffmpeg は subprocess 注入で非実行）--------------------


def _capture_script(monkeypatch) -> dict:
    from wwedit.compose import ffmpeg_compose

    captured: dict = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = list(cmd)
        for i, a in enumerate(cmd):
            if a == "-filter_complex_script":
                captured["script"] = Path(cmd[i + 1]).read_text(encoding="utf-8")

        class R:
            returncode = 0
            stderr = ""

        return R()

    monkeypatch.setattr(ffmpeg_compose.subprocess, "run", fake_run)
    return captured


def _wide_png(path: Path) -> Path:
    from PIL import Image

    Image.new("RGB", (2100, 900), (255, 255, 255)).save(path)
    return path


def test_compose_kept_places_infographic_below_chibi_and_subtitles(tmp_path, monkeypatch):
    """図解はモザイクより上・ちびキャラ/字幕より下（はみ出しても隠されない並び）。"""
    from wwedit.compose import ffmpeg_compose
    from wwedit.edl.schema import ChibiConfig, SpeakerTrack, Utterance, Word

    monkeypatch.setenv("WWEDIT_CHIBI_ASSETS", str(tmp_path / "assets"))
    for char in ("noa", "suzu"):
        d = tmp_path / "assets" / char / "normal"
        d.mkdir(parents=True)
        (d / "mouth_closed.png").write_bytes(b"x")
        (d / "mouth_open.png").write_bytes(b"x")

    edl = _edl()
    edl.source.audio_tracks = [SpeakerTrack(speaker="A", path="a.m4a"),
                               SpeakerTrack(speaker="B", path="b.m4a")]
    edl.utterances = [Utterance(speaker="A", text="やあ", start=1.0, end=2.0,
                                words=[Word(text="やあ", start=1.0, end=2.0)])]
    edl.character_cast = {"A": "noa", "B": "suzu"}
    edl.chibi = ChibiConfig(enabled=True)
    edl.infographic = InfographicConfig(
        path=str(_wide_png(tmp_path / "ig.png")), duration_s=10.0)

    captured = _capture_script(monkeypatch)
    ffmpeg_compose.compose_kept(edl, tmp_path / "out.mp4", audio="embedded",
                                subtitles=True, chibi=True, infographic=True)
    script = captured["script"]
    assert "scale=1516:650" in script                    # 21:9 が安全枠に内接
    assert "overlay=202:78" in script                    # 中央寄せ・リボンの下
    assert script.index("[igi]overlay") < script.index("overlay=24:H-h-24")   # ちびより下
    assert script.index("[igi]overlay") < script.index("ass=")                # 字幕より下


def test_compose_kept_infographic_off_keeps_script_clean(tmp_path, monkeypatch):
    from wwedit.compose import ffmpeg_compose

    edl = _edl()
    edl.infographic = InfographicConfig(path=str(_wide_png(tmp_path / "ig.png")))
    captured = _capture_script(monkeypatch)
    ffmpeg_compose.compose_kept(edl, tmp_path / "out.mp4", audio="embedded")
    assert "igi" not in captured["script"]     # infographic=False では一切混ざらない


def test_chapters_inside_flags_eyecatch_split_risk():
    """表示中に章境界があると `--eyecatch` が図解を分断する → CLI が警告する。"""
    from wwedit.publish.cli import _chapters_inside

    edl = _edl()   # 章は 0 / 120 / 400 秒（カット無しなので出力秒と同じ）
    assert _chapters_inside(edl, 0.0, 10.0) == []        # 先頭章(0秒)は図解の前なので対象外
    hits = _chapters_inside(edl, 115.0, 125.0)
    assert [t for t, _ in hits] == [120.0]
