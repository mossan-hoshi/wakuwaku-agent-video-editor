"""開始フレームの**参照画像を明示できる**こと。

由来: 2026-09-09。`publish character-image --char tsukasa` が自動で選ぶのは
`tsukasa_a-*.webp`＝565x1024・38KB の**LP用に縮小した立ち姿**で、顔は縦120px程度しかない。
引きの構図＋風景を頼んだら絵柄が再現しきれず「安っぽい水彩」になった。
同じフォルダに 2502x2000・206KB の `tsukasa_c-*.webp` があるのに `_a*` 決め打ちで選ばれない。

既定は変えない（他キャラの実績を壊さない）。**明示的に渡せる道**を足したのが本件。
"""

from pathlib import Path

import pytest

from wwedit.publish import character


def _assets(tmp_path: Path) -> Path:
    d = tmp_path / "assets"
    d.mkdir()
    (d / "noa_a-small.webp").write_bytes(b"a" * 100)
    (d / "noa_c-big.webp").write_bytes(b"c" * 5000)
    (d / "noa_chibi_normal.webp").write_bytes(b"x" * 9000)   # chibi は候補外
    return d


def test_available_refs_sorted_by_size_and_excludes_chibi(tmp_path):
    d = _assets(tmp_path)
    got = character.available_character_refs("noa", d)
    assert [p.name for p in got] == ["noa_c-big.webp", "noa_a-small.webp"]


def test_default_resolution_is_unchanged(tmp_path):
    """既定は従来どおり `_a*`。**勝手に大きい方へ切り替えない。**"""
    d = _assets(tmp_path)
    assert character.resolve_character_ref("noa", d).name == "noa_a-small.webp"


def test_explicit_ref_images_are_used(tmp_path, monkeypatch):
    d = _assets(tmp_path)
    seen = {}

    def fake_generate_image(prompt, **kw):
        seen["refs"] = kw["reference_images"]
        return b"PNG"

    monkeypatch.setattr(character, "generate_image", fake_generate_image)
    monkeypatch.setattr(character, "save_image", lambda data, out: Path(out))

    character.generate_character_image(
        "noa", "夕暮れの縁側", tmp_path / "o.png", assets_dir=d,
        ref_images=[d / "noa_c-big.webp", d / "noa_a-small.webp"])
    assert [len(b) for _, b in seen["refs"]] == [5000, 100]      # 渡した順
    assert [m for m, _ in seen["refs"]] == ["image/webp", "image/webp"]


def test_missing_explicit_ref_raises(tmp_path):
    d = _assets(tmp_path)
    with pytest.raises(FileNotFoundError):
        character.generate_character_image(
            "noa", "x", tmp_path / "o.png", assets_dir=d,
            ref_images=[d / "nope.webp"])


def test_mime_follows_extension(tmp_path):
    assert character._mime_of(Path("a.png")) == "image/png"
    assert character._mime_of(Path("a.JPG")) == "image/jpeg"
    assert character._mime_of(Path("a.webp")) == "image/webp"
