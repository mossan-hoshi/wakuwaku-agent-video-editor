"""開始フレームの解像度は**下見 0.5K → 本番 2K の2段**であること。

由来: 2026-09-09。`generate_character_image` が `image_size="2K"` をベタ書きしていて、
CLI からも下げられなかったため、構図の当たりを取る1枚目まで最高解像度で焼いていた。
ユーザー指摘「0.5kであたりをつけてから2kで作れや」「なんで nano banana 2 を2kで作ってんだよ」。

🚨 **解像度の値はドキュメントを信じない。** 公式ドキュメントは「0.5K」と書いているが
エンドポイントは 400 で拒否し、`Supported values are: 1K, 2K, 4K, 512, 512P, 512PX` を返す
（2026-09-09 実測）。下見は **`512`**。
"""

from pathlib import Path

from wwedit.publish import character


def _assets(tmp_path: Path) -> Path:
    d = tmp_path / "assets"
    d.mkdir()
    (d / "noa_a-small.webp").write_bytes(b"a" * 100)
    return d


def _capture(monkeypatch) -> dict:
    seen: dict = {}

    def fake_generate_image(prompt, **kw):
        seen.update(kw)
        return b"PNG"

    monkeypatch.setattr(character, "generate_image", fake_generate_image)
    monkeypatch.setattr(character, "save_image", lambda data, out: Path(out))
    return seen


#: エンドポイントが実際に受理する値（400 のエラー本文より・2026-09-09 実測）。
ACCEPTED = {"1K", "2K", "4K", "512", "512P", "512PX"}


def test_sizes_are_actually_accepted_by_the_api():
    """🚨 ドキュメントの「0.5K」は**通らない**。実測で通る値だけを持つこと。"""
    assert character.DRAFT_SIZE in ACCEPTED
    assert character.FINAL_SIZE in ACCEPTED
    assert character.DRAFT_SIZE != character.FINAL_SIZE
    assert "0.5K" not in (character.DRAFT_SIZE, character.FINAL_SIZE)


def test_default_is_draft_not_final(tmp_path, monkeypatch):
    """🚨 既定は下見。**いきなり 2K を焼かない。**"""
    seen = _capture(monkeypatch)
    character.generate_character_image(
        "noa", "夕暮れの縁側", tmp_path / "o.png", assets_dir=_assets(tmp_path))
    assert seen["image_size"] == character.DRAFT_SIZE


def test_final_size_is_passed_through(tmp_path, monkeypatch):
    seen = _capture(monkeypatch)
    character.generate_character_image(
        "noa", "夕暮れの縁側", tmp_path / "o.png", assets_dir=_assets(tmp_path),
        image_size=character.FINAL_SIZE)
    assert seen["image_size"] == "2K"
    assert seen["aspect_ratio"] == "16:9"      # 16:9 は据え置き


def test_expression_override_does_not_change_character_setting():
    """その絵だけ表情を変えられるが、**キャラ設定（mascot.md 由来）は書き換えない**。

    由来: 2026-09-09「笑顔止めろ」。司の既定は `calm confident expression, faint smile` で、
    黙って `EXPRESSION` を書き換えると以後の全イントロから笑みが消えてしまう。
    """
    before = character.expression_of("tsukasa")
    assert "faint smile" in before

    over = "calm composed expression, NO smile at all, lips closed"
    got = character.build_prompt("x", "tsukasa", "", over)
    assert over in got
    assert "faint smile" not in got

    # 既定は据え置き。上書きは呼び出しごと。
    assert character.expression_of("tsukasa") == before
    assert "faint smile" in character.build_prompt("x", "tsukasa")


def test_tsukasa_intro_subtitle_is_grey_not_pink():
    """司のイントロ字幕は**灰色**。未登録のままだと既定のピンクへ落ちる。

    由来: 2026-09-09「ピンク字幕止めよう。灰色で」。ピンクは noa の配色であって
    司のものではない。`CHARACTER_COLORS` に無いキャラは `INTRO_COLOR`(ピンク)になる。
    """
    from wwedit.subtitle.ass import INTRO_COLOR, ass_to_rgb, intro_color_for

    col = intro_color_for("tsukasa")
    assert col != INTRO_COLOR
    r, g, b = ass_to_rgb(col)
    assert max(r, g, b) - min(r, g, b) <= 20      # 無彩色に近い＝灰色
    assert 60 <= (r + g + b) / 3 <= 130           # 白背景で沈まず、白枠にも溶けない
