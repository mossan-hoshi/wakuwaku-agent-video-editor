import pytest

from wwedit.edl.schema import Chapter, Edl, Segment, SourceMedia
from wwedit.publish.description import build_description
from wwedit.publish.youtube import build_video_resource, tags_from_description


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


def test_build_description_format():
    edl = _edl()
    text = build_description(
        edl, agenda="のべつべ！開発の裏側",
        hashtags="#個人開発 #生成ai",
        links=[("発表資料", "https://example.com/slides")],
    )
    # 先頭は Agenda「」
    assert text.startswith("Agenda「のべつべ！開発の裏側」")
    # リンクは ラベル→URL
    assert "発表資料\nhttps://example.com/slides" in text
    # ハッシュタグ行
    assert "#個人開発 #生成ai" in text
    # タイムスタンプ: 00:00 - start + MM:SS - ラベル（00:00章はstartに集約）
    assert "00:00 - start" in text
    assert "02:00 - CVPR2026概要" in text
    assert "00:00 - 開会" not in text  # 00:00章はstartへ集約
    # 実投稿に無いものは入れない
    assert "チャプター" not in text
    assert "チャンネル:" not in text
    assert "AIが自動生成" not in text
    assert text.endswith("\n")


def test_build_description_hashtags_list_and_no_links():
    edl = _edl()
    text = build_description(edl, agenda="テーマ", hashtags=["個人開発", "ai"])
    assert text.startswith("Agenda「テーマ」")
    assert "#個人開発 #ai" in text  # # 自動付与
    assert "https://" not in text  # リンク無し


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
    # 構図はコードで持たない。付くのは破綻回避の下限だけ（test_character_expression.py 参照）
    assert "bust-up" not in p
    assert "mouth must be fully visible" in p and "16:9" in p


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
        assert line[-1] in "。！？をてにはがでともやへの、"


def test_intro_wrap_never_starts_line_with_punctuation():
    """行頭禁則: 、。）」等を次行の先頭に置かない。

    実際に踏んだ: 「MCP対応と」/「、ローカルLLMの…」と読点が行頭に落ちた（#102 イントロ）。
    """
    from wwedit.publish.intro_compose import wrap_script

    src = "ゆめです。今日はComfyUIのMCP対応と、ローカルLLMの速度検証です。詳しくは本編でどうぞ。"
    lines = wrap_script(src).split("\n")
    assert "".join(lines) == src  # 文字落ちなし
    for line in lines:
        assert line[0] not in "、。，．・…！？!?）)」』】〉》〕"


_INTRO_SRC = "ノアです。今日は小ネタと、タニグチさんのComfyUI MCP検討の続報です。では本編どうぞ"


def test_intro_wrap_keeps_alnum_token_intact():
    """英数字トークン(ComfyUI 等)を語中で割らない（割ると『Comfy / UI』になる）。"""
    from wwedit.publish.intro_compose import wrap_script

    lines = wrap_script(_INTRO_SRC).split("\n")
    assert "".join(lines) == _INTRO_SRC              # 文字落ちなし
    # ComfyUI / MCP がそれぞれ1行に収まっている（＝分断されていない）
    assert any("ComfyUI" in line for line in lines)
    assert any("MCP" in line for line in lines)


def test_intro_wrap_no_orphan_copula():
    """「…続報で / す。」のような孤立行を作らない（です/ます を割らない）。"""
    from wwedit.publish.intro_compose import wrap_script

    lines = wrap_script(_INTRO_SRC).split("\n")
    assert all(len(line) >= 3 for line in lines)      # 極端に短い行が出ない
    assert not any(line.strip() in ("す。", "す", "た。", "ます。") for line in lines)


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


_DESC = (
    "Agenda「テーマ」\n\n#生成AI #ComfyUI #個人開発 #わく枠べんきょ会\n\n"
    "00:00 - start\n01:38 - 章タイトル\n"
)


def test_tags_from_description():
    # ハッシュタグ行だけを拾い、# を外して順序どおり返す
    assert tags_from_description(_DESC) == ["生成AI", "ComfyUI", "個人開発", "わく枠べんきょ会"]
    # 大小違いの重複は1つに畳む
    assert tags_from_description("#AI #ai #Ai") == ["AI"]
    # ハッシュタグ行が無ければ空（＝タグ無し。チャンネル #99以前の実投稿と同じ）
    assert tags_from_description("Agenda「テーマ」\n\n00:00 - start\n") == []
    # 本文中に # があるだけの行は拾わない（タグだけの行が対象）
    assert tags_from_description("これは #ハッシュ を含む文です") == []


def test_tags_from_description_respects_total_limit():
    desc = "\n".join(["#" + "あ" * 20 for _ in range(50)])  # 合計上限を超える量
    tags = tags_from_description(desc)
    assert tags  # 何かは返る
    assert sum(len(t) + 1 for t in tags) <= 480  # API の合計上限内に収まる


def test_build_video_resource():
    body = build_video_resource("タイトル", _DESC, privacy="unlisted")
    assert body["snippet"]["title"] == "タイトル"
    assert body["snippet"]["description"] == _DESC
    # tags 未指定＝概要欄のハッシュタグ由来（内容と無関係な固定タグを付けない）
    assert body["snippet"]["tags"] == ["生成AI", "ComfyUI", "個人開発", "わく枠べんきょ会"]
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


def test_shrink_thumbnail_fits_api_limit(tmp_path):
    """2MB超のサムネは JPEG 縮小してから送る（thumbnails.set は 2MB 上限）。"""
    import numpy as np
    from PIL import Image

    from wwedit.publish.youtube import THUMBNAIL_MAX_BYTES, _shrink_thumbnail

    # ノイズ＝圧縮が効きにくい ⇒ 2K PNG で確実に 2MB 超になる
    rng = np.random.default_rng(0)
    src = tmp_path / "thumbnail.png"
    Image.fromarray(rng.integers(0, 255, (1440, 2560, 3), dtype="uint8")).save(src)
    assert src.stat().st_size > THUMBNAIL_MAX_BYTES

    out = _shrink_thumbnail(src)
    assert out.stat().st_size <= THUMBNAIL_MAX_BYTES
    assert Image.open(out).width == 1280
    assert src.stat().st_size > THUMBNAIL_MAX_BYTES  # 元ファイルは触らない


def test_build_video_resource_tags_override():
    # 明示指定が優先／空リストで「タグ無し」を明示できる
    assert build_video_resource("t", _DESC, tags=["手動"])["snippet"]["tags"] == ["手動"]
    assert build_video_resource("t", _DESC, tags=[])["snippet"]["tags"] == []


# ---- 冒頭ブロック（--intro-file）: その回だけの前置きを Agenda の前に置く ----
# 2026-08-06「概要欄冒頭に各動画をどう使ったか（簡潔に）」というユーザー指示で追加。


def test_intro_goes_above_the_agenda():
    intro = "この動画は全部Claude Codeで作りました。"
    text = build_description(_edl(), agenda="テーマ", intro=intro)
    assert text.startswith(f"{intro}\n\nAgenda「テーマ」")


def test_no_intro_keeps_the_usual_format():
    """通常回は従来と完全に同一（冒頭の空行も増えない）。"""
    edl = _edl()
    plain = build_description(edl, agenda="テーマ")
    assert plain == build_description(edl, agenda="テーマ", intro="")
    assert plain.startswith("Agenda「")


def test_whitespace_only_intro_is_dropped():
    edl = _edl()
    assert build_description(edl, agenda="テーマ", intro="   \n\n ").startswith("Agenda「")


def test_a_multi_line_intro_keeps_its_line_breaks():
    intro = "1行目\n2行目\n\n段落2"
    text = build_description(_edl(), agenda="テーマ", intro=intro)
    assert text.startswith(intro + "\n\nAgenda「")


def test_a_plain_intro_does_not_disturb_the_chapters():
    from wwedit.publish.description import chapter_problems

    text = build_description(
        _edl(), agenda="テーマ",
        intro="この動画はClaude Codeで自動編集しました。\n通常版: https://youtu.be/xxxx")
    assert "00:00 - start" in text
    assert not chapter_problems(text)


def test_a_timestamp_in_the_intro_is_caught_by_the_checker():
    """⚠️ 冒頭ブロックに時刻行を書くと YouTube が章を誤読する。検査が弾くこと。

    1つでも章の条件を破ると章リストが**丸ごと**無効化されるので、
    「気づかず投稿してしまう」のが一番まずい（#101 で全滅した）。
    """
    from wwedit.publish.description import chapter_problems

    text = build_description(_edl(), agenda="テーマ", intro="前置き\n00:00 章のつもりではない行")
    assert chapter_problems(text)


# ── イントロ合成: ジングル無しの経路（2026-08-08 実際に落ちた）────────────────
def _capture_intro_cmd(monkeypatch, tmp_path, *, jingle=None):
    from wwedit.publish import intro_compose as ic

    seen: dict = {}

    def fake_run(cmd, **_kw):
        seen["cmd"] = list(cmd)

        class R:
            returncode = 0
            stderr = ""
        return R()

    monkeypatch.setattr(ic, "_duration", lambda _p: 8.0)
    monkeypatch.setattr(ic, "_badge", lambda *_a, **_k: tmp_path / "badge.png")
    monkeypatch.setattr(ic.subprocess, "run", fake_run)
    (tmp_path / "in.mp4").write_bytes(b"x")
    ic.compose_intro(tmp_path / "in.mp4", "ノアです。", tmp_path / "out.mp4",
                     jingle=jingle)
    return seen["cmd"]


def test_intro_without_jingle_maps_the_raw_audio_stream(monkeypatch, tmp_path):
    """🚨 ``-map [0:a]`` は**フィルタ出力ラベル**の意味になり ffmpeg が落ちる。

    2026-08-08 実害: ジングル無しでイントロを作ろうとして
    「Output with label '0:a' does not exist」で失敗した（ジングル有りの経路しか
    通っていなかったので気づけていなかった）。素の入力は角括弧を付けない。
    """
    cmd = _capture_intro_cmd(monkeypatch, tmp_path)
    assert "[0:a]" not in cmd
    assert cmd[cmd.index("-map", cmd.index("-map") + 1) + 1] == "0:a"


def test_intro_with_jingle_still_maps_the_mixed_label(monkeypatch, tmp_path):
    (tmp_path / "jg.wav").write_bytes(b"x")
    cmd = _capture_intro_cmd(monkeypatch, tmp_path, jingle=tmp_path / "jg.wav")
    assert cmd[cmd.index("-map", cmd.index("-map") + 1) + 1] == "[aout]"
    assert "amix=inputs=2" in cmd[cmd.index("-filter_complex") + 1]


# ── 声のライセンス表記（CC-BY 等は概要欄への表示が義務）────────────────────
def test_voice_credit_for_borrowed_voice():
    """🚨 借りた声のクレジットを落とさない（2026-08-08 指摘・投稿後に発覚）。"""
    from wwedit.publish.character import voice_credits

    got = voice_credits(["souta", "noa"])
    assert len(got) == 1                       # noa は自前素材＝出さない
    label, url = got[0]
    assert "松風" in label and "CC-BY-4.0" in label and "月島颯太" in label
    assert url == "https://twitter.com/mochi_jin_voice"


def test_voice_credit_dedupes_and_skips_unknown():
    from wwedit.publish.character import voice_credits

    assert voice_credits(["souta", "souta"]) == voice_credits(["souta"])
    assert voice_credits(["unknown_char", ""]) == []


def test_description_renders_credit_as_a_link_block():
    """クレジットは既存の links ブロック（ラベル→URL）に載る＝独自フォーマットを作らない。"""
    from wwedit.publish.character import voice_credits

    edl = _edl()
    text = build_description(edl, agenda="テーマ", links=voice_credits(["souta"]),
                             hashtags="#動画編集")
    assert "月島颯太 (松風音声読み上げデータ / 松風 / CC-BY-4.0)\nhttps://twitter.com/mochi_jin_voice" in text
    assert text.index("Agenda") < text.index("松風") < text.index("#動画編集")
