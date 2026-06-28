import pytest

from wwedit.edl.schema import Chapter, Edl, Segment, SourceMedia
from wwedit.publish.description import AI_DISCLAIMER, build_description
from wwedit.publish.youtube import DEFAULT_TAGS, build_video_resource


def _edl():
    return Edl(
        recording_dir="2026-06-04",
        source=SourceMedia(video_path="v.mp4", fps=30, width=1920, height=1080, duration_s=600.0),
        segments=[Segment(id="s0", start=0.0, end=600.0, invalid=False)],
        chapters=[
            Chapter(start_at=0.0, chapter_title="開会"),
            Chapter(start_at=120.0, chapter_title="CVPR2026概要"),
            Chapter(start_at=300.0, chapter_title="まとめ"),
        ],
    )


def test_build_description_structure():
    edl = _edl()
    text = build_description(
        edl, title="【勉強会】CVPR2026の注目論文",
        summary="今回はCVPR2026の論文を紹介します。\nFlashVSRやSAM3Dなど。",
        extra_links=[("発表資料", "https://example.com/slides")],
    )
    assert text.startswith("【勉強会】CVPR2026の注目論文")
    assert "今回はCVPR2026の論文を紹介します。" in text
    # チャプター節（先頭00:00必須）
    assert "チャプター" in text
    assert "00:00 開会" in text
    assert "02:00 CVPR2026概要" in text  # 全区間残存なので source=output
    # リンク節
    assert "・発表資料 https://example.com/slides" in text
    # フッター（AI免責＋チャンネル）
    assert AI_DISCLAIMER in text
    assert "@mossan_hoshi" in text
    assert text.endswith("\n")


def test_build_description_no_summary_no_links():
    edl = _edl()
    text = build_description(edl, title="タイトルのみ", summary="")
    assert "タイトルのみ" in text
    assert "チャプター" in text
    assert "リンク" not in text  # extra_links 無し
    assert AI_DISCLAIMER in text


def test_character_ref_and_prompt(tmp_path):
    from wwedit.publish.character import (
        IDENTITY_CONSTRAINT,
        build_prompt,
        resolve_character_ref,
    )

    (tmp_path / "noa_a-XYZ.webp").write_bytes(b"x")
    (tmp_path / "noa_chibi_normal.webp").write_bytes(b"x")  # chibi=除外
    ref = resolve_character_ref("noa", tmp_path)
    assert ref.name == "noa_a-XYZ.webp"  # _a を選び chibi は除外
    with pytest.raises(FileNotFoundError):
        resolve_character_ref("yume", tmp_path)
    p = build_prompt("early summer outfit, hydrangea")
    assert p.startswith(IDENTITY_CONSTRAINT)
    assert "early summer outfit, hydrangea" in p
    assert "bust-up" in p and "16:9" in p  # リップシンク構図


def test_aivis_default_style():
    from wwedit.publish.aivis import DEFAULT_STYLE

    assert DEFAULT_STYLE["noa"] == "normal"


def test_full_name():
    from wwedit.publish.character import full_name

    assert full_name("noa") == "文月 乃亜"  # mascot.md 本名
    assert full_name("tsukasa") == "御影 司"
    assert full_name("unknown") == "Unknown"  # 未登録は先頭大文字ID


def test_intro_wrap_script():
    from wwedit.publish.intro_compose import wrap_script

    # 文(。)で改行・長文は助詞境界で折る（語中で切らない）
    src = "こんにちは。今日は最新AI論文をまとめて紹介してみました。本編でどうぞ。"
    lines = wrap_script(src).split("\n")
    assert lines[0] == "こんにちは。"
    assert all(line for line in lines)  # 空行なし
    assert "".join(lines) == src  # 文字落ちなし
    assert max(len(line) for line in lines) <= 16  # 各行 max_line 以下
    for line in lines:  # 行末は句点/助詞/読点＝語中で切れない
        assert line[-1] in "。！？をてにはがでともやへ、"


def test_script_to_subtitles_two_lines():
    from wwedit.publish.intro_compose import script_to_subtitles

    src = "こんにちは。今日は最新AI論文をまとめて紹介してみました。本編でどうぞ。"
    subs = script_to_subtitles(src, 9.0)
    # 各キューは最大2行
    assert all(s.text.count("\n") <= 1 for s in subs)
    assert all(s.style == "intro" for s in subs)
    # 時間は連続・末尾は total、単調増加
    assert subs[0].start == 0.0
    assert subs[-1].end == 9.0
    for a, b in zip(subs, subs[1:], strict=False):
        assert a.end == b.start and a.end > a.start
    # 全文が落ちない（行を連結すると元に戻る）
    assert "".join(s.text.replace("\n", "") for s in subs) == src


def test_parse_emphasis():
    from wwedit.publish.thumbnail import parse_emphasis

    segs = parse_emphasis("[CVPR2026] 最新AI論文", (255, 230, 60))
    assert segs == [("CVPR2026", (255, 230, 60)), (" 最新AI論文", (255, 255, 255))]
    # 複数強調＋区切り
    segs2 = parse_emphasis("[動画超解像]・[3D復元]を解説", (255, 80, 80))
    emph = [t for t, c in segs2 if c == (255, 80, 80)]
    assert emph == ["動画超解像", "3D復元"]
    # 強調なし＝全部 base
    assert parse_emphasis("ふつう", (1, 2, 3)) == [("ふつう", (255, 255, 255))]


def test_eyecatch_ink_colors_valid():
    from wwedit.publish.eyecatch import INK_COLORS

    assert len(INK_COLORS) >= 5  # ロゴ配色（多色）
    for col in INK_COLORS:
        assert len(col) == 3
        assert all(0 <= ch <= 255 for ch in col)  # RGB 0..255


def test_eyecatch_title_card(tmp_path):
    # ffmpeg を使わない PIL 部分（タイトルカード）だけ検証
    from wwedit.publish.eyecatch import _title_card

    out = _title_card("FlashVSR 動画超解像", tmp_path / "t.png", w=640, h=360)
    assert out.exists()
    from PIL import Image

    im = Image.open(out)
    assert im.size == (640, 360)
    assert im.mode == "RGBA"  # 透過（背景に重畳するため）


def test_build_video_resource():
    body = build_video_resource("タイトル", "概要欄テキスト", privacy="unlisted")
    assert body["snippet"]["title"] == "タイトル"
    assert body["snippet"]["description"] == "概要欄テキスト"
    assert body["snippet"]["tags"] == DEFAULT_TAGS
    assert body["snippet"]["categoryId"] == "28"
    assert body["status"]["privacyStatus"] == "unlisted"
    assert body["status"]["selfDeclaredMadeForKids"] is False


def test_build_video_resource_limits_and_validation():
    # title 100字・description 5000字でトリム
    body = build_video_resource("あ" * 200, "い" * 6000)
    assert len(body["snippet"]["title"]) == 100
    assert len(body["snippet"]["description"]) == 5000
    with pytest.raises(ValueError):
        build_video_resource("t", "d", privacy="draft")
