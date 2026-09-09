"""chibi.assets のテスト（画像生成・rembg は実行しない）。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

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
    # suzu はベースが両目を開けた中立表情なので、normal の口閉じはコピーで済む
    (_assets_root / "suzu").mkdir(parents=True)
    (_assets_root / "suzu" / "base_rgba.png").write_bytes(b"BASE")
    p = generate_mouth_image("suzu", "normal", "closed")
    assert p.read_bytes() == b"BASE"  # コピーのみ＝課金なし


def test_normal_closed_is_redrawn_for_chars_whose_base_is_not_neutral(_assets_root: Path):
    """⚠️ noa は**片目ウインク**・priya は**笑い口**なので、ベースを normal に流用しない。

    流用すると平常時ずっと片目をつぶった絵になり、瞬きの目パッチも左右非対称に壊れる
    （基準の片目が既に閉じているので差分が片目しか出ない）。
    """
    from wwedit.chibi.assets import REDRAW_CLOSED_CHARS

    assert {"noa", "priya"} <= set(REDRAW_CLOSED_CHARS)
    (_assets_root / "noa").mkdir(parents=True)
    (_assets_root / "noa" / "base_rgba.png").write_bytes(b"BASE")
    # ベース流用なら即コピーで返るが、描き直し扱いなので生成APIへ進む（キー無しで落ちる）
    with pytest.raises(Exception) as ei:
        generate_mouth_image("noa", "normal", "closed")
    assert not isinstance(ei.value, FileExistsError)
    assert (_assets_root / "noa" / "normal" / "mouth_closed.png").read_bytes() != b"BASE" \
        if (_assets_root / "noa" / "normal" / "mouth_closed.png").exists() else True


def test_normal_prompt_requires_both_eyes_open():
    """ベースがウインクでも、描き直しで両目が開くよう明示する。"""
    from wwedit.chibi.assets import chibi_emotion_prompt

    p = chibi_emotion_prompt("noa", "normal", "closed")
    assert "BOTH EYES OPEN" in p and "never winking" in p


def test_base_prefers_high_res_character_refs(tmp_path: Path, monkeypatch):
    """ベースは **character_refs の 1024²** を使う（tts_chibi の 480² は最後の手段）。

    低解像度を参照に投げると生成AIが線と塗りを補完し直し、頬のチークが消え・目が細くなり・
    口が一文字になった（＝別人化）。同じ絵の高解像度版がある限りそちらを使う。
    """
    from wwedit.chibi.assets import resolve_chibi_base

    refs, drawable = tmp_path / "refs", tmp_path / "drawable"
    refs.mkdir()
    drawable.mkdir()
    monkeypatch.setenv("WWEDIT_NOVTUBE_CHARACTER_REFS", str(refs))
    monkeypatch.setenv("WWEDIT_NOVTUBE_DRAWABLE", str(drawable))
    (drawable / "tts_chibi_noa.webp").write_bytes(b"lowres")
    assert resolve_chibi_base("noa").name == "tts_chibi_noa.webp"  # refs が無ければ従来通り
    (refs / "noa_00.webp").write_bytes(b"hires")
    assert resolve_chibi_base("noa") == refs / "noa_00.webp"


def test_readable_hint_only_on_gesture_emotions():
    """「320pxで違いが明白に」は**身振りのある感情だけ**。

    `normal` は他の全感情の登録基準なので、ここで誇張を促すと基準ごと別人になる。
    """
    marker = "difference must be obvious"
    assert marker not in chibi_emotion_prompt("noa", "normal", "closed")
    assert marker in chibi_emotion_prompt("noa", "surprised", "closed")


def test_prompt_pins_face_features_that_drifted():
    """頬・目の形・まつげは名指しで固定する（放っておくと絵柄を整え直される）。

    ⚠️ 固定するのは「参照から変えるな」であって、「頬をピンクに」ではない
    （[[test_prompts_never_add_looks_the_reference_does_not_have]]）。
    """
    p = chibi_emotion_prompt("noa", "surprised", "closed")
    assert "the face outline and the cheeks exactly as they are drawn" in p
    assert "never narrower" in p and "do not add heavy or long lashes" in p
    # 口は「閉じる」だけを要求し、形は参照から受け継ぐ（一文字の険しい口を防ぐ）
    assert "do not flatten it into a" in p


def test_base_prompt_forbids_lower_body_and_full_body_framing():
    """ベース描き起こしは**腰まで**。全身で返ると実寸320pxで顔が他キャラの半分になる。

    「waist-up」とだけ書いても太ももまで描かれた（実測）。画像の下辺が腰を横切ると言い切り、
    枠外に出すものを名指しで数え上げる。1枚目の参照を「full-body art」と紹介すると構図まで
    引っ張られるので、役割を identity に限定する文言も必須。
    """
    from wwedit.chibi.assets import chibi_base_prompt

    p = chibi_base_prompt("noa")
    assert "BOTTOM EDGE OF THE IMAGE CUTS ACROSS THE CHARACTER'S WAIST" in p
    for part in ("thighs", "knees", "legs", "feet", "shoes", "skirt", "shorts"):
        assert part in p, part
    assert "IGNORE its framing" in p
    assert "full-body art" not in p.lower()
    # 「standing pose」は脚を要求してしまうので使わない
    assert "standing pose" not in p
    # 頭身の指定が無いと3.5頭身くらいで返る
    assert "ABOUT HALF of the" in p


def test_trim_base_cuts_bottom_and_is_reversible(_assets_root: Path):
    """脚まで描かれたベースは**切って詰める**（引き直すより安い）。

    切る前の絵を `base_untrimmed.png` に残し、常にそこから切り直すので、フラクションを
    振り直しても劣化しない（切った絵をさらに切ると戻せなくなる）。
    """
    from wwedit.chibi.assets import base_meta, trim_chibi_base
    from wwedit.chibi.geometry import effective_bbox

    d = _assets_root / "noa"
    d.mkdir(parents=True)
    img = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    # 上=頭(幅広) / 下=脚(細い) の縦長シルエット
    for y in range(100, 600):
        for x in range(300, 700):
            img.putpixel((x, y), (255, 0, 0, 255))
    for y in range(600, 900):
        for x in range(450, 550):
            img.putpixel((x, y), (0, 0, 255, 255))
    img.save(d / "base_rgba.png")

    def _ratio() -> float:
        x0, y0, x1, y1 = effective_bbox(Image.open(d / "base_rgba.png").convert("RGBA"))
        return (x1 - x0) / (y1 - y0)

    before = _ratio()
    trim_chibi_base("noa", 0.6)                      # 脚を落とす
    assert (d / "base_untrimmed.png").exists()
    cut = _ratio()
    assert cut > before                              # 縦に詰まった＝横長寄りになる
    assert base_meta("noa")["trim_bottom_frac"] == 0.6

    # 切り直しは**退避元**から。1.0 に戻せば元の比率に戻る＝二重に切れていない
    trim_chibi_base("noa", 1.0)
    assert _ratio() == pytest.approx(before, abs=0.01)


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


def test_remove_bg_keeps_hand_cut_alpha(_assets_root: Path, tmp_path: Path):
    """手で切り抜いた透過PNGを置いたら、**抜き直さない**。

    ユーザーが `base_gen.png` を手修正して透過で保存した場合に、こちらが白背景マットや
    rembg を掛けて台無しにしないためのガード。
    """
    from wwedit.chibi.assets import remove_bg

    src = tmp_path / "hand.png"
    img = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    # 手で切った想定：中央だけ不透明・境界に中間αを残す
    ImageDraw.Draw(img).ellipse((24, 24, 103, 103), fill=(200, 30, 40, 255))
    img.putpixel((23, 64), (200, 30, 40, 128))
    img.save(src)
    out = remove_bg(src, tmp_path / "out.png")
    a = np.asarray(Image.open(out).convert("RGBA"))[..., 3]
    assert a[64, 23] == 128        # 中間αがそのまま残る＝抜き直していない
    assert a[64, 64] == 255
    assert a[2, 2] == 0


def test_gesture_never_brings_both_hands_together():
    """⚠️ 両手を体の前で寄せる指示を書かない。**手が団子になる**（smile で実測）。

    新ベースは手に何も持っていないので、「胸元へ寄せる」と左右の手が重なって塊になる。
    手は体の横・背中・握り拳のように**左右が触れ合わない位置**へ置く。
    あわせて、口パクの差し替え領域を隠さないことも毎回明示する。
    """
    from wwedit.chibi.assets import EMOTION_GESTURE, GESTURE_HAND_RULE

    for emo, g in EMOTION_GESTURE.items():
        if not g:
            continue
        assert "toward the chest" not in g, emo
        assert "against the chest" not in g, emo
        # 旧版の「持っている物」前提の言い回しは廃止済み
        assert "whatever the character holds" not in g, emo
    p = chibi_emotion_prompt("noa", "smile", "closed")
    assert GESTURE_HAND_RULE in p
    assert "NEVER touch, overlap or interlock" in p
    assert "nothing may cover the mouth" in p
    # normal は身振り無しなので手の制約も付かない（余計な指示で平常顔を崩さない）
    assert GESTURE_HAND_RULE not in chibi_emotion_prompt("noa", "normal", "closed")


def test_prompts_never_add_looks_the_reference_does_not_have():
    """⚠️ **参照画像が持っている見た目を、こちらから指定し直さない**（2026-08-06 ユーザー指摘）。

    「頬っぺたピンク」をプロンプトで足していたせいで、公式ちび絵に**チークが無い**
    颯太・司・霞が別人になった（公式ちび絵は9キャラ全員チーク無し）。
    プロンプトに書いてよいのは

    * **下流の工程が要求する状態**（両目を開ける＝瞬きパッチ／口を閉じる＝口パクの土台）
    * **維持**の指示（参照から変えるな）
    * mascot.md 由来の**性格**（`expression_of`）

    だけで、色・肌・髪型・装飾のような**見た目の追加**は書かない。
    """
    from wwedit.chibi.assets import (
        CHIBI_EMOTIONS,
        EMOTION_PROMPT,
        chibi_base_prompt,
        chibi_emotion_prompt,
        eyes_closed_prompt,
    )

    # 見た目を"足す"言い回し。維持側（"if the reference has no blush, do NOT add any"）は別に見る。
    added_look = ("pink blush on both cheeks", "blushing cheeks", "add blush",
                  "rosy cheeks", "light pink blush")
    prompts = [chibi_base_prompt("souta"), eyes_closed_prompt("souta")]
    prompts += [chibi_emotion_prompt("souta", e, m)
                for e in CHIBI_EMOTIONS for m in ("closed", "open")]
    for p in prompts:
        for phrase in added_look:
            assert phrase not in p, phrase
    for emo, expr in EMOTION_PROMPT.items():
        assert "blush" not in expr, emo

    # 維持は「参照どおり」と一息で言い切る（長く書くと変更点まで効かなくなる）。
    keep = chibi_emotion_prompt("souta", "smile", "closed")
    assert "the cheeks exactly as they are drawn in the reference" in keep
