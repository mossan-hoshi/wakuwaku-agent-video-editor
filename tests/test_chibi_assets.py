"""chibi.assets のテスト（画像生成・rembg は実行しない）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from wwedit.chibi.assets import (
    check_pair_alignment,
    chibi_emotion_prompt,
    compose_mouth_only,
    generate_mouth_image,
    missing_assets,
    sprite_path,
)


@pytest.fixture(autouse=True)
def _assets_root(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WWEDIT_CHIBI_ASSETS", str(tmp_path / "chibi"))
    yield tmp_path / "chibi"


def test_emotion_prompt_respects_mascot_rules():
    # yume は smile でも big grin にしない（ジト目維持）＝mascot.md 規約
    p = chibi_emotion_prompt("yume", "smile", "closed")
    assert "jito-me" in p and "NOT a big grin" in p
    assert "big grin" not in p.replace("NOT a big grin", "")
    # 通常キャラは標準プロンプト。表情は**目と眉で表す**（口の形を書くと口閉じ画像が笑い口になる）
    noa = chibi_emotion_prompt("noa", "smile", "closed")
    assert "closed-curve (^_^) eyes" in noa
    assert "smiling" not in noa.split("(character's baseline look:")[0]
    assert "Mouth small and fully CLOSED" in noa
    # open は「わずかに開く・歯は見えない・他は同一」だけを簡潔に指定する
    op = chibi_emotion_prompt("noa", "smile", "open")
    assert "slightly open" in op and "No teeth" in op
    assert "identical to the reference" in op
    # 背景抜きを容易にする白背景指定
    assert "white background" in chibi_emotion_prompt("noa", "normal", "open")


def test_generate_rejects_existing_without_force(_assets_root: Path):
    d = _assets_root / "noa" / "smile"
    d.mkdir(parents=True)
    (d / "mouth_closed.png").write_bytes(b"x")
    with pytest.raises(FileExistsError, match="1枚勝負"):
        generate_mouth_image("noa", "smile", "closed")


def test_generate_open_requires_closed(_assets_root: Path, monkeypatch):
    # base はダミーを置いて ensure_base をスキップ
    (_assets_root / "noa").mkdir(parents=True)
    (_assets_root / "noa" / "base_rgba.png").write_bytes(b"x")
    with pytest.raises(FileNotFoundError, match="mouth_closed"):
        generate_mouth_image("noa", "smile", "open")


def test_normal_closed_reuses_base_without_billing(_assets_root: Path):
    (_assets_root / "noa").mkdir(parents=True)
    (_assets_root / "noa" / "base_rgba.png").write_bytes(b"BASE")
    p = generate_mouth_image("noa", "normal", "closed")
    assert p.read_bytes() == b"BASE"  # コピーのみ＝課金なし


def test_missing_assets_enumeration(_assets_root: Path):
    (_assets_root / "noa").mkdir(parents=True)
    (_assets_root / "noa" / "base_rgba.png").write_bytes(b"x")
    d = _assets_root / "noa" / "normal"
    d.mkdir()
    (d / "mouth_closed.png").write_bytes(b"x")
    miss = missing_assets(["noa"], ["normal"])
    assert ("noa", "", "base") not in miss
    assert ("noa", "normal", "closed") not in miss
    assert ("noa", "normal", "open") in miss


def test_check_pair_alignment_detects_drift(tmp_path: Path):
    a = tmp_path / "a.png"
    b_same = tmp_path / "b.png"
    b_shift = tmp_path / "c.png"
    img = Image.new("RGBA", (100, 100), (255, 0, 0, 255))
    img.save(a)
    img.save(b_same)
    shifted = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    shifted.paste(img.crop((0, 0, 90, 100)), (10, 0))  # 10px 横ずれ
    shifted.save(b_shift)
    assert check_pair_alignment(a, b_same) == 0.0
    assert check_pair_alignment(a, b_shift) > 0.02


def test_sprite_path_resolution(_assets_root: Path):
    # 中間フレームは作らないので 0=閉 / 1=開 の2枚に直結する
    assert sprite_path("noa", "smile", 0) == _assets_root / "noa" / "smile" / "mouth_closed.png"
    assert sprite_path("noa", "smile", 1) == _assets_root / "noa" / "smile" / "mouth_open.png"
    # 第二弾（瞬き）の目インデックス付きは行列命名
    assert sprite_path("noa", "smile", 1, eye=1).name == "m1_e1.png"


def _face(tmp_path: Path, name: str, *, mouth_h: int, shift: int = 0) -> Path:
    """顔＋口だけのダミー画像（口は下寄り中央の黒い矩形）。"""
    img = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    for y in range(20, 190):
        for x in range(40, 160):
            img.putpixel((x + shift, y), (240, 220, 210, 255))
    for y in range(120, 120 + mouth_h):        # 口
        for x in range(90, 110):
            img.putpixel((x + shift, y), (20, 10, 10, 255))
    for y in range(60, 70):                    # 目（動かない目印）
        for x in range(60, 75):
            img.putpixel((x + shift, y), (20, 20, 20, 255))
    p = tmp_path / name
    img.save(p)
    return p


def test_compose_mouth_only_keeps_everything_but_mouth(tmp_path: Path):
    closed = _face(tmp_path, "closed.png", mouth_h=3)
    # 生成画像は口が大きく、かつ全体が 3px ずれている（生成AIの典型的なブレ）
    generated = _face(tmp_path, "gen.png", mouth_h=24, shift=3)
    out, frac = compose_mouth_only(closed, generated, tmp_path / "out.png")
    assert 0.0 < frac < 0.35                      # マスクは口周りに限定される
    assert check_pair_alignment(closed, out) < 0.01  # 口以外は口閉じ画像のまま
    a = Image.open(closed).convert("RGBA")
    b = Image.open(out).convert("RGBA")
    assert a.getpixel((66, 64)) == b.getpixel((66, 64))   # 目は不変
    assert a.getpixel((100, 135)) != b.getpixel((100, 135))  # 口は差し替わる
