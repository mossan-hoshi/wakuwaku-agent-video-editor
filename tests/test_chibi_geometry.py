"""ちびキャラ幾何（実効領域・頭部登録・領域合成・検査）の純関数テスト。

画像生成 API も rembg も呼ばない。すべて PIL で合成したダミー画像で検証する。
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageDraw

from wwedit.chibi import geometry as G


def _face(size=256, *, eye_y=90, eye_r=12, mouth_open=False, shift=(0, 0),
          faint_bg=True, scale=1.0):
    """目2つ・口1つの単純な顔。``faint_bg`` で rembg 相当の微小アルファ背景を撒く。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx, cy = size // 2 + shift[0], size // 2 + shift[1]
    r = int(size * 0.34 * scale)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(250, 226, 205, 255))
    ey = cy - r + int(eye_y * scale * size / 256) - 40
    for ex in (cx - int(r * 0.42), cx + int(r * 0.42)):
        rr = int(eye_r * scale)
        d.ellipse((ex - rr, ey - rr, ex + rr, ey + rr), fill=(40, 40, 48, 255))
    my = cy + int(r * 0.42)
    if mouth_open:
        d.ellipse((cx - 16, my - 14, cx + 16, my + 14), fill=(120, 40, 40, 255))
    else:
        d.line((cx - 16, my, cx + 16, my), fill=(120, 40, 40, 255), width=5)
    if faint_bg:
        a = np.asarray(img.split()[3]).copy()
        a[a == 0] = 3          # rembg は背景に微小アルファを残す（実測）
        img.putalpha(Image.fromarray(a))
    return img


def test_effective_bbox_ignores_faint_alpha():
    """``getbbox()`` は微小アルファでキャンバス全体を返す。閾値版だけが正しい箱を返す。"""
    img = _face()
    assert img.getbbox() == (0, 0, 256, 256)              # 実測された罠そのもの
    assert img.split()[3].getbbox() == (0, 0, 256, 256)
    x0, y0, x1, y1 = G.effective_bbox(img)
    assert 0 < x0 < 60 and 0 < y0 < 60
    assert 196 < x1 <= 256 and 196 < y1 <= 256


def test_effective_bbox_raises_when_empty():
    with pytest.raises(ValueError):
        G.effective_bbox(Image.new("RGBA", (32, 32), (0, 0, 0, 0)))


def test_register_to_head_recovers_shift_and_scale():
    """既知の平行移動・等方スケールを頭部テンプレート照合で復元できる。"""
    ref = _face(faint_bg=False)
    head = (60, 40, 196, 176)
    moved = _face(shift=(7, -5), faint_bg=False)
    dx, dy, s, _c, score = G.register_to_head(moved, ref, head)
    assert score > 0.5
    assert dx == pytest.approx(-7, abs=2.0)
    assert dy == pytest.approx(5, abs=2.0)
    assert s == pytest.approx(1.0, abs=0.02)


def test_apply_similarity_restores_alignment():
    """登録して変換を適用すると、残差がほぼ 0 になる。"""
    ref = _face(faint_bg=False)
    head = (60, 40, 196, 176)
    moved = _face(shift=(6, 4), faint_bg=False)
    dx, dy, s, c, _sc = G.register_to_head(moved, ref, head)
    fixed = G.apply_similarity(moved, dx, dy, s, c)
    dx2, dy2, ds2 = G.check_head_anchor(fixed, ref, head)
    assert abs(dx2) <= 2.0 and abs(dy2) <= 2.0 and abs(ds2) <= 0.01


def test_canvas_transform_hits_targets():
    """キャラの高さ比と下余白が目標値に寄る（キャラ間の見かけを揃えるため）。"""
    img = _face(scale=0.7)
    tf = G.canvas_transform(img)
    out = G.apply_canvas_transform(img, tf)
    x0, y0, x1, y1 = G.effective_bbox(out)
    assert (y1 - y0) / out.height == pytest.approx(G.TARGET_CHAR_FRAC, abs=0.05)
    assert (out.height - y1) / out.height == pytest.approx(
        G.TARGET_BOTTOM_FRAC, abs=0.03)


def test_eye_boxes_finds_two_and_symmetrizes():
    base = _face(faint_bg=False)
    blink = _face(eye_r=3, faint_bg=False)          # 目だけ小さく＝閉じた差分
    alpha = np.asarray(base.split()[3])
    boxes = G.eye_boxes(G.flat_gray(base), G.flat_gray(blink), alpha,
                        head_box=(60, 40, 196, 176))
    assert len(boxes) == 2
    (lx0, ly0, lx1, ly1), (rx0, ry0, rx1, ry1) = boxes
    assert lx1 < rx0                                  # 左右に分かれている
    assert (ly1 - ly0) == (ry1 - ry0)                 # 高さが揃っている（対称化）
    assert (lx1 - lx0) == (rx1 - rx0)


def test_compose_region_only_replaces_eyes_and_leaves_no_seam(tmp_path):
    """目だけ差し替わり、**楕円の外側は base と完全一致**（マスク漏れが無い証明）。"""
    base = _face(faint_bg=False)
    blink = _face(eye_r=3, faint_bg=False)
    bp, sp, dp = tmp_path / "b.png", tmp_path / "s.png", tmp_path / "d.png"
    base.save(bp)
    blink.save(sp)
    alpha = np.asarray(base.split()[3])
    boxes = G.eye_boxes(G.flat_gray(base), G.flat_gray(blink), alpha,
                        head_box=(60, 40, 196, 176))
    out_p, frac = G.compose_region_only(
        bp, sp, dp, boxes=boxes, pad=(0.08, 0.15, 0.15, 0.15),
        align_region=(60, 40, 196, 176))
    assert 0.0 < frac < 0.25
    out = Image.open(out_p).convert("RGBA")

    # 目の領域は変わった
    na, nb = np.asarray(base, np.int16), np.asarray(out, np.int16)
    ex0, ey0, ex1, ey1 = boxes[0]
    assert (np.abs(na - nb).max(axis=2)[ey0:ey1, ex0:ex1] > 24).any()

    # 貼り付け楕円の外側（十分離れた領域）は完全一致＝はみ出していない
    m = np.ones(na.shape[:2], bool)
    for x0, y0, x1, y1 in boxes:
        pad = 40
        m[max(0, y0 - pad):y1 + pad, max(0, x0 - pad):x1 + pad] = False
    assert np.array_equal(na[m], nb[m])

    # アルファは base のまま（生成側の縁ノイズを持ち込まない）
    assert np.array_equal(np.asarray(base.split()[3]), np.asarray(out.split()[3]))


def test_compose_region_only_requires_boxes(tmp_path):
    p = tmp_path / "a.png"
    _face().save(p)
    with pytest.raises(ValueError):
        G.compose_region_only(p, p, tmp_path / "o.png")


def test_pose_delta_and_mouth_checks_detect_changes():
    ref = _face(faint_bg=False)
    head = (60, 40, 196, 176)
    # 同じ絵ならポーズ差は無い（=1.0 に近い＝「変わっていない」）
    assert G.pose_delta(_face(faint_bg=False), ref, head) == pytest.approx(1.0, abs=1e-6)
    # 口が開けば mouth_open 比が上がる（_face の口は y≈164 中心・幅32）
    mouth = (108, 146, 148, 182)
    opened = _face(mouth_open=True, faint_bg=False)
    assert G.check_mouth_closed(opened, ref, mouth) > 1.5
    assert G.check_mouth_closed(_face(faint_bg=False), ref, mouth) == pytest.approx(
        1.0, abs=0.2)


def test_regions_roundtrip(tmp_path):
    G.save_regions(tmp_path, {"head_box": [1, 2, 3, 4]})
    assert G.load_regions(tmp_path)["head_box"] == [1, 2, 3, 4]
    assert G.load_regions(tmp_path / "nope") == {}


# ── 白背景からのマット起こし ──────────────────────────────────

def _white_bg_art(size=256):
    """白地に、黒い輪郭線で囲まれた**白い服**を持つキャラ（塗り分けの罠を再現）。"""
    img = Image.new("RGB", (size, size), (255, 255, 255))
    d = ImageDraw.Draw(img)
    # 体：黒枠＋白塗り（＝背景と同じ色。外周から塗ると輪郭線で止まる）
    d.ellipse((60, 40, 196, 176), fill=(255, 255, 255), outline=(0, 0, 0), width=6)
    d.rectangle((90, 150, 166, 230), fill=(255, 255, 255), outline=(0, 0, 0), width=6)
    return img


def test_white_bg_matte_keeps_white_clothes_and_cuts_only_outside():
    """白い服は**背景と同じ色**だが、輪郭線で囲われているので抜けてはいけない。"""
    img = _white_bg_art()
    out = G.white_bg_matte(img)
    assert out is not None
    a = np.asarray(out)[..., 3]
    assert a[10, 10] == 0                 # 外は透明
    assert a[100, 128] > 250              # 顔の中の白は残る
    assert a[190, 128] > 250              # 服の白も残る
    # 輪郭線上は不透明（線を削っていない）
    assert a[40 + 3, 128] > 200


def test_white_bg_matte_returns_none_for_non_white_background():
    """白背景でない絵では None を返して rembg へ譲る（無理に抜かない）。"""
    img = Image.new("RGB", (128, 128), (40, 90, 160))
    ImageDraw.Draw(img).ellipse((30, 30, 98, 98), fill=(255, 220, 200))
    assert G.white_bg_matte(img) is None


def test_white_bg_matte_despills_the_white_background_out_of_edge_pixels():
    """半透明画素の色から**白背景の寄与を引く**（引かないと暗い映像で白フチになる）。

    黒い線を白地に置くと、境界の画素は「白と黒の中間」で観測される。α だけ付けて
    そのまま使うと、暗い背景に重ねたとき縁が明るく浮く。``F=(C-(1-a)·255)/a`` で
    本来の前景色（＝ほぼ黒）へ戻す。
    """
    img = Image.new("RGB", (64, 64), (255, 255, 255))
    ImageDraw.Draw(img).rectangle((16, 16, 47, 47), fill=(0, 0, 0))
    # 境界を作るため、わざと中間調の1画素を置く（アンチエイリアス相当）
    img.putpixel((15, 30), (128, 128, 128))
    assert np.asarray(img)[30, 15].max() == 128     # 入力は「白と黒の中間」
    out = G.white_bg_matte(img)
    a = np.asarray(out)[..., 3]
    rgb = np.asarray(out)[..., :3]
    assert 90 < a[30, 15] < 160                     # 半透明になる
    assert rgb[30, 15].max() < 40                   # 色は白が抜けて黒へ寄る


def test_pillow_resize_is_alpha_correct():
    """⚠️ 自前のプリマルチプライ縮小を足さないこと。**Pillow が既にやっている**。

    透明画素の色を真っ赤にしても、縮小後の半透明画素に赤が出ない＝α を掛けて補間して
    いる。ここが将来のバージョンで変わったら（＝この検査が落ちたら）自前実装が要る。
    """
    src = Image.new("RGBA", (128, 128), (255, 0, 0, 0))   # 透明だが色は真っ赤
    ImageDraw.Draw(src).ellipse((21, 21, 106, 106), fill=(0, 0, 0, 255))
    n = np.asarray(src.resize((64, 64), Image.LANCZOS))
    band = (n[..., 3] > 16) & (n[..., 3] < 239)
    assert band.any()
    assert n[..., 0][band].max() < 8


def test_drop_specks_removes_floating_dust_but_keeps_attached_marks():
    """本体から離れた小さな塊は落とす。**髪に接した汗/怒りマークは残す**。

    生成画像はキャラから離れた場所に点や小塊を残すことがある（実測: thinking で143px）。
    320px に縮めても背景に浮いたゴミとして見える。連結成分の最大に対する割合で切るので、
    本体に接している装飾は連結成分が同じになり巻き添えにならない。
    """
    img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((60, 60, 195, 195), fill=(200, 40, 40, 255))     # 本体
    d.ellipse((188, 70, 205, 96), fill=(80, 160, 255, 255))    # 本体に接した汗マーク
    d.ellipse((14, 220, 22, 228), fill=(0, 0, 0, 255))         # 浮遊ゴミ
    out = np.asarray(G.drop_specks(img, min_frac=0.01))[..., 3]
    assert out[128, 128] > 200        # 本体は残る
    assert out[80, 196] > 200         # 接している装飾も残る
    assert out[224, 18] == 0          # 浮遊ゴミは消える
    # 実測比（noa/thinking: 本体449010px に対しゴミ143px）は既定の閾値で落ちる
    assert 143 / 449010 < G.SPECK_MIN_FRAC


def test_drop_specks_is_a_noop_on_a_single_blob():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle((10, 10, 53, 53), fill=(1, 2, 3, 255))
    assert np.array_equal(np.asarray(G.drop_specks(img)), np.asarray(img))


def test_eye_boxes_band_is_anchored_to_the_mouth_not_the_head_top():
    """⚠️ 目の探索帯は **口からの相対**で取る。頭頂からの割合だと目を外す。

    `head_box` は口と実効領域から組み立てた粗い箱で、ちび絵は髪（アホ毛）が大きいぶん
    上へ伸びる。実測（noa）では目の重心が head_box の 0.71 の高さにあり、旧実装の帯
    0.25〜0.65 から外れて**髪の端の点を目として拾っていた**。
    """
    base = _face(faint_bg=False)
    blink = _face(eye_r=3, faint_bg=False)
    alpha = np.asarray(base.split()[3])
    # 髪が上に大きい想定で、頭の箱だけを上へ伸ばす（目は箱の下寄りになる）
    tall_head = (60, -260, 196, 176)
    mouth = (108, 146, 148, 182)

    # 口を渡さないと（＝旧挙動）目が帯から外れて取れない
    with pytest.raises(RuntimeError):
        G.eye_boxes(G.flat_gray(base), G.flat_gray(blink), alpha, head_box=tall_head)

    boxes = G.eye_boxes(G.flat_gray(base), G.flat_gray(blink), alpha,
                        head_box=tall_head, mouth_box=mouth)
    assert len(boxes) == 2
    for _x0, y0, _x1, y1 in boxes:
        cy = (y0 + y1) / 2
        assert mouth[1] - 120 < cy < mouth[1]      # 口のすぐ上に来ている
