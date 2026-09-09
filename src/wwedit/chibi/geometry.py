"""ちびキャラ画像の幾何（実効領域・頭部登録・領域合成・検査）。

ポーズを含む感情差分を作れるようにした結果、**「キャンバスの固定割合で帯を切る」やり方が
成立しなくなった**。腕が動けば char bbox が動くし、頭の中心と char bbox の中心は実測で
150px ずれる（yume は頭が細く左寄り、priya は頭が広く中央）。そこで幾何はキャラごとの
``regions.json`` に一元化し、後処理も検査もすべてそこを見る。

なぜ「頭部テンプレートで**登録**する」のか（検出ではなく）:

- 合成は ``scale=-1:{height}`` で**キャンバス高さ**を揃える。キャンバス内でキャラが
  どれだけの大きさ・どの位置に描かれるかがブレると、そのまま画面上のブレになる。
  実測で yume は char_h/canvas_h=96.2%、priya は 90.4% と既に6%ずれている。
- 頭部矩形の中身（髪シルエット・顔輪郭・眼鏡・フード）はプロンプトで固定される要素が
  支配的で、変化する目と眉は面積として少数派。``TM_CCOEFF_NORMED`` は明度オフセットに
  不変なので、表情の描き直しに強い。
- 参照は常に**同一キャラの normal** なので、キャラ横断で効く汎用の頭部検出器を作らずに済む。

⚠️ ``Image.getbbox()`` は使えない。rembg(isnet-anime) の出力は背景にも微小なアルファが
残るので、``getbbox()`` はキャンバス全体を返す（yume/priya の両方で実測）。必ず
``effective_bbox()``（``alpha > ALPHA_ON`` の二値化）を使うこと。
"""

from __future__ import annotations

import json
from pathlib import Path

__all__ = [
    "ALPHA_ON", "alpha_mask", "effective_bbox", "flat_gray", "best_shift",
    "diff_components", "mouth_bbox", "eye_boxes", "head_box_from_eyes",
    "head_box_from_mouth", "register_to_head", "apply_similarity",
    "canvas_transform", "apply_canvas_transform", "compose_region_only",
    "WHITE_BG_TH", "white_bg_matte", "SPECK_MIN_FRAC", "drop_specks",
    "regions_path", "load_regions", "save_regions",
    "check_head_anchor", "check_face_identity", "pose_delta",
    "check_mouth_closed", "check_patch_seam",
]

#: アルファがこれを超える画素を「キャラの実体」とみなす。rembg は背景に微小アルファを
#: 残すので、0 では背景まで拾ってしまう（既存 ``_mouth_bbox`` の直書き値をここへ集約）。
ALPHA_ON = 16

#: 幾何の基準キャンバス。羽根幅などの絶対値はこの一辺を基準にスケールする。
REF_CANVAS = 1024

#: ``normalize_canvas`` の目標値。キャラ間で見かけの大きさと足元位置を揃える。
TARGET_CHAR_FRAC = 0.94      # char 高さ / キャンバス高さ
TARGET_BOTTOM_FRAC = 0.015   # 下余白 / キャンバス高さ


#: 「背景の白」とみなす下限（RGB の最小チャンネル）。生成プロンプトが
#: ``Plain solid white background`` を要求しているので、背景はほぼ 255 で返る。
WHITE_BG_TH = 248


# ── 白背景からのマット起こし ──────────────────────────────────

def white_bg_matte(img, *, white_th: int = WHITE_BG_TH, band: int = 2):
    """**純白背景**の絵から、輪郭線を壊さずにアルファを起こす（rembg より綺麗）。

    rembg(isnet-anime) は低解像度マスクを引き伸ばすので、境界が階段状になり、
    キャラの黒い輪郭線を1〜2px 削って**暗背景で白いハロ**が出る（実測）。
    背景が純白と分かっているならニューラルマスクは要らない。

    手順:

    1. 画像の**外周から** near-white を4連結で塗りつぶす → 背景領域。
       輪郭線が閉じているので、**服の白は塗られない**（外周と繋がっていない）。
    2. 境界の帯だけ、白背景に載った合成式 ``C = a·F + (1-a)·255`` を逆に解いて
       ``a = 1 - min(C)/255`` とする（輪郭線は黒＝F≈0 なのでこれで正しい）。
    3. 同じ式で**色から白背景の寄与を引く**（デスピル）: ``F = (C - (1-a)·255) / a``。
       これをやらないと半透明画素が「白と混ざった色」のまま残り、暗い映像に重ねたとき
       **白フチ**として見える。

    背景が白くない絵（塗りつぶしが極端に小さい/大きい、中心まで届く）では
    ``None`` を返す。呼び出し側が rembg へ落ちること。

    ⚠️ 縮小は Pillow に任せてよい。**Pillow 12 の ``Image.resize`` は RGBA を
    プリマルチプライして補間する**ので、透明画素の色は縁へ滲まない（実測で確認済み・
    ``test_pillow_resize_is_alpha_correct``）。自前のプリマルチプライ縮小は要らない。
    """
    import numpy as np
    from PIL import Image
    from scipy import ndimage as ndi

    rgb = np.asarray(img.convert("RGB")).astype(np.float32)
    mn = rgb.min(axis=2)
    lbl, _ = ndi.label(mn >= white_th)
    edge_labels = (set(lbl[0].tolist()) | set(lbl[-1].tolist())
                   | set(lbl[:, 0].tolist()) | set(lbl[:, -1].tolist()))
    edge_labels.discard(0)
    if not edge_labels:
        return None
    bg = np.isin(lbl, list(edge_labels))
    h, w = bg.shape
    # 白背景でない絵（写真的な背景・塗りが中心まで達する）を弾く
    if not 0.10 < bg.mean() < 0.95 or bg[h // 2, w // 2]:
        return None
    inside = ~bg
    edge = (ndi.binary_dilation(bg, iterations=band)
            & ndi.binary_dilation(inside, iterations=band))
    a = inside.astype(np.float32)
    a[edge] = np.clip(1.0 - mn[edge] / 255.0, 0.0, 1.0)
    a[bg & ~edge] = 0.0
    # 白背景の寄与を色から引く（半透明画素が白と混ざったままだと暗い映像で白フチになる）
    a3 = a[..., None]
    fg = np.where(a3 > 1e-3, (rgb - (1.0 - a3) * 255.0) / np.maximum(a3, 1e-3), rgb)
    return Image.fromarray(
        np.dstack([np.clip(fg, 0, 255).astype(np.uint8),
                   (a * 255).round().astype(np.uint8)]), "RGBA")


#: 本体に対してこの割合より小さい浮遊塊はゴミとみなして消す。
#: 汗マークや怒りマークは髪に接していて本体と連結するので巻き添えにならない。
#: 実測（noa・1024²）: 本体≈45万px に対しゴミは 1〜143px（=0.03%以下）だった。
SPECK_MIN_FRAC = 0.0005


def drop_specks(img, *, min_frac: float = SPECK_MIN_FRAC):
    """本体から離れた**小さな浮遊塊**を透明にする。

    生成画像には、キャラから数十px離れたところに点や小さな塊が残ることがある
    （実測: thinking で 143px の塊、他の感情でも 1〜8px の埃）。320px に縮めても
    背景に浮いたゴミとして見えるので、連結成分の最大（＝キャラ本体）に対して
    ``min_frac`` 未満の成分を落とす。
    """
    import numpy as np
    from PIL import Image
    from scipy import ndimage as ndi

    img = img.convert("RGBA")
    arr = np.asarray(img).copy()
    lbl, n = ndi.label(arr[..., 3] > ALPHA_ON)
    if n <= 1:
        return img
    sizes = np.bincount(lbl.ravel())
    sizes[0] = 0
    keep = sizes >= max(sizes.max() * min_frac, 2)
    keep[0] = False
    arr[..., 3] = np.where(keep[lbl], arr[..., 3], 0)
    return Image.fromarray(arr, "RGBA")


# ── 実効領域 ────────────────────────────────────────────────

def alpha_mask(img, th: int = ALPHA_ON):
    """``alpha > th`` の bool 配列（キャラの実体マスク）。"""
    import numpy as np

    return np.asarray(img.convert("RGBA").split()[3]) > th


def effective_bbox(img, th: int = ALPHA_ON) -> tuple[int, int, int, int]:
    """実効的な不透明領域 ``(x0, y0, x1, y1)``（右下は排他・PIL 準拠）。

    ``Image.getbbox()`` は rembg の微小アルファでキャンバス全体を返すので使わない。
    """
    import numpy as np

    m = alpha_mask(img, th)
    ys, xs = np.where(m)
    if len(ys) == 0:
        raise ValueError("不透明画素が無い（背景抜きに失敗している）")
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def flat_gray(img):
    """RGBA を白背景に平坦化したグレースケール配列（位置合わせ・差分用）。"""
    import numpy as np
    from PIL import Image

    rgb = Image.new("RGB", img.size, (255, 255, 255))
    rgb.paste(img, mask=img.split()[3])
    return np.asarray(rgb.convert("L"), dtype=np.int16)


def best_shift(base, moving, *, max_shift: int = 12, region=None) -> tuple[int, int]:
    """``moving`` を ``base`` に重ねる整数平行移動量 (dx, dy) を粗探索する。

    ``region``=(x0,y0,x1,y1) を渡すとその矩形だけで評価する。**ポーズが変わる画像では
    必ず頭部矩形を渡すこと**。全画面で平均を取ると、動いた腕と小物に引っ張られて頭が合わない。
    """
    import numpy as np

    if region is not None:
        x0, y0, x1, y1 = region
        base, moving = base[y0:y1, x0:x1], moving[y0:y1, x0:x1]
    s = 4
    b = base[::s, ::s].astype(np.float32)
    m = moving[::s, ::s].astype(np.float32)
    r = max(1, max_shift // s)
    h, w = b.shape
    best, best_d = (0, 0), None
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            bb = b[max(0, dy):h + min(0, dy), max(0, dx):w + min(0, dx)]
            mm = m[max(0, -dy):h + min(0, -dy), max(0, -dx):w + min(0, -dx)]
            d = float(np.abs(bb - mm).mean())
            if best_d is None or d < best_d:
                best_d, best = d, (dx * s, dy * s)
    return best


# ── 差分から領域を取る ──────────────────────────────────────

def diff_components(
    gray_a, gray_b, alpha, *, band: tuple[int, int, int, int],
    thresh: int = 24, close_ksize: int = 7, min_area: int = 16,
) -> list[tuple[tuple[int, int, int, int], float]]:
    """2枚の差分の連結成分を ``[(bbox, 差分量), ...]`` で強い順に返す。

    ``band`` は重心が入っていなければならない矩形（探索帯）。生成AIは目や輪郭も微妙に
    描き直すので、単純な行列和では眼鏡まで巻き込む。
    """
    import cv2
    import numpy as np

    mag = np.abs(gray_a - gray_b).astype(np.float32)
    binary = ((mag > thresh) & (alpha > ALPHA_ON)).astype(np.uint8)
    k = np.ones((close_ksize, close_ksize), np.uint8)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, k)
    n, labels, stats, cent = cv2.connectedComponentsWithStats(binary, connectivity=8)
    bx0, by0, bx1, by1 = band
    out: list[tuple[tuple[int, int, int, int], float]] = []
    for i in range(1, n):
        cx, cy = cent[i]
        if not (bx0 <= cx <= bx1 and by0 <= cy <= by1):
            continue
        if int(stats[i, cv2.CC_STAT_AREA]) < min_area:
            continue
        x, y, w, h = (int(v) for v in stats[i, :4])
        out.append(((x, y, x + w, y + h), float(mag[labels == i].sum())))
    out.sort(key=lambda t: t[1], reverse=True)
    return out


def mouth_bbox(closed_g, open_g, alpha, *, head_box=None) -> tuple[int, int, int, int]:
    """口が動いた領域の bbox（差分の最大成分）。

    ``head_box`` があれば顔の下寄りの帯を頭部相対で取る。無ければ実効 bbox 相対
    （ポーズが動かない旧アセット向けの後方互換）。
    """
    import numpy as np

    ys, xs = np.where(alpha > ALPHA_ON)
    if head_box is not None:
        hx0, hy0, hx1, hy1 = head_box
        hw, hh = hx1 - hx0, hy1 - hy0
        band = (hx0 + hw * 0.20, hy0 + hh * 0.55, hx0 + hw * 0.80, hy0 + hh * 1.00)
    else:
        y0, y1, x0, x1 = ys.min(), ys.max(), xs.min(), xs.max()
        ch, cw = y1 - y0, x1 - x0
        band = (x0 + cw * 0.25, y0 + ch * 0.40, x0 + cw * 0.75, y0 + ch * 0.85)
    comps = diff_components(closed_g, open_g, alpha, band=band)
    if not comps:
        raise RuntimeError("口の差分が検出できない（生成画像が参照と同じ／位置が想定外）")
    return comps[0][0]


def eye_boxes(base_g, blink_g, alpha, *, head_box,
              mouth_box=None) -> list[tuple[int, int, int, int]]:
    """目が閉じた差分から**左右2つ**の目領域を返す（左→右の順）。

    口版（最大1成分）からの一般化。目は2つあるので、上位2成分を取ったうえで
    「水平に離れ、垂直にほぼ揃う」ことを検査する。満たさない場合の分岐:

    - 1成分に融合していた（``MORPH_CLOSE`` が鼻梁越しに橋渡しした）→ 頭部の垂直中心で割る
    - それでも駄目 → ``RuntimeError``（呼び出し側で ``regions.json`` を手で直す）

    ⚠️ 探索帯は **``mouth_box`` からの相対**で取る。``head_box`` は口と実効領域から
    組み立てた粗い箱で、髪（アホ毛）まで含むため、頭頂からの割合で帯を切ると
    キャラによって目が帯の外へ落ちる。実測（noa）: 目の重心は head_box の 0.71 の高さに
    あり、旧実装の帯 0.25〜0.65 から外れて**髪の端の点を目として拾っていた**。
    目と口の位置関係のほうがずっと安定している。
    """
    hx0, hy0, hx1, hy1 = head_box
    hw, hh = hx1 - hx0, hy1 - hy0
    if mouth_box is not None:
        my0 = mouth_box[1]
        band = (hx0 + hw * 0.05, max(hy0, my0 - hh * 0.60),
                hx0 + hw * 0.95, my0 - hh * 0.02)
    else:
        band = (hx0 + hw * 0.05, hy0 + hh * 0.25, hx0 + hw * 0.95, hy0 + hh * 0.65)
    comps = diff_components(base_g, blink_g, alpha, band=band)
    if not comps:
        raise RuntimeError("目の差分が検出できない（閉じ目画像が元と同じ）")

    boxes = [b for b, _ in comps[:2]]
    if len(boxes) == 2 and _is_eye_pair(boxes[0], boxes[1], hw, hh):
        return _symmetrize(sorted(boxes, key=lambda b: b[0]), (hx0 + hx1) // 2)

    # 融合ケース: 幅が頭幅の 45% を超える単一成分は、頭部の垂直中心線で2つに割る
    x0, y0, x1, y1 = boxes[0]
    if (x1 - x0) > hw * 0.45:
        mid = (hx0 + hx1) // 2
        if x0 + 4 < mid < x1 - 4:
            return _symmetrize([(x0, y0, mid - 2, y1), (mid + 2, y0, x1, y1)], mid)
    raise RuntimeError(f"目の左右ペアが取れない: {boxes}")


def _is_eye_pair(a, b, head_w: float, head_h: float) -> bool:
    acy, bcy = (a[1] + a[3]) / 2, (b[1] + b[3]) / 2
    acx, bcx = (a[0] + a[2]) / 2, (b[0] + b[2]) / 2
    return abs(acy - bcy) < head_h * 0.25 and abs(acx - bcx) > head_w * 0.15


def _symmetrize(boxes, mid_x: int) -> list[tuple[int, int, int, int]]:
    """左右の箱の高さを揃え、眼間中心に対して対称な大きさへスナップする。

    片目だけ膨張率が違うと、閉じ目の左右で見え方が変わって不自然になる。
    """
    (lx0, ly0, lx1, ly1), (rx0, ry0, rx1, ry1) = boxes
    h = max(ly1 - ly0, ry1 - ry0)
    w = max(lx1 - lx0, rx1 - rx0)
    lcy, rcy = (ly0 + ly1) // 2, (ry0 + ry1) // 2
    lcx, rcx = (lx0 + lx1) // 2, (rx0 + rx1) // 2
    cy = (lcy + rcy) // 2
    d = max(abs(lcx - mid_x), abs(rcx - mid_x))
    out = []
    for cx in (mid_x - d, mid_x + d):
        out.append((cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2))
    return out


def head_box_from_eyes(boxes, canvas_size) -> tuple[int, int, int, int]:
    """両目の位置と眼間距離から頭部矩形を組む（頭部検出器を持たずに済ませる）。"""
    (lx0, ly0, lx1, ly1), (rx0, ry0, rx1, ry1) = boxes
    cx = ((lx0 + lx1) + (rx0 + rx1)) / 4
    cy = ((ly0 + ly1) + (ry0 + ry1)) / 4
    d = abs(((rx0 + rx1) / 2) - ((lx0 + lx1) / 2))
    if d <= 1:
        raise ValueError("眼間距離が取れない")
    w, h = canvas_size
    return (max(0, int(cx - 1.35 * d)), max(0, int(cy - 1.5 * d)),
            min(w, int(cx + 1.35 * d)), min(h, int(cy + 1.4 * d)))


def head_box_from_mouth(eff_bbox, mouth_box, canvas_size) -> tuple[int, int, int, int]:
    """口の位置と実効領域の上端から頭部矩形を組む（目つむり画像がまだ無い段階の暫定）。

    厳密な頭部である必要はない。この矩形に求められるのは3つだけ:

    - 感情間で不変な領域を多く含む（登録テンプレートとしての安定性）
    - 目と口を含む（探索帯の基準になる）
    - **腕と小物を含まない**（ポーズ変化に引っ張られない）

    ⚠️ 口幅からの比率で頭を推定するのは駄目（口幅/キャラ幅が yume 4.9%・priya 6.1% と
    安定しない）。シルエットの行ランから頭幅を測る方法も、うさ耳フード（yume）や
    2頭身（priya）で破綻する。実効領域の上端を頭頂とみなすのが最も素直。
    """
    ex0, _ey0, ex1, _ey1 = eff_bbox
    top = eff_bbox[1]
    mcx, mcy = (mouth_box[0] + mouth_box[2]) / 2.0, (mouth_box[1] + mouth_box[3]) / 2.0
    mh = mouth_box[3] - mouth_box[1]
    bottom = mcy + mh * 1.5           # 顎の少し下まで
    half = (bottom - top) * 0.55
    w, h = canvas_size
    return (max(0, int(min(mcx - half, ex1))), max(0, int(top)),
            min(w, int(max(mcx + half, ex0))), min(h, int(bottom)))


# ── 頭部登録（平行移動＋等方スケール）────────────────────────

def register_to_head(
    img, ref, head_box, *, scales=None, use_edges: bool = True,
) -> tuple[float, float, float, tuple[float, float], float]:
    """``img`` を ``ref`` に重ねる相似変換を求める。

    返り値 ``(dx, dy, s, center, score)``。``center`` を中心に ``s`` 倍してから
    ``(dx, dy)`` 平行移動すると、``img`` の頭が ``ref`` の頭と同じ位置・大きさになる。
    回転は扱わない（頭の向きは固定する方針。B の目パッチが成立する条件でもある）。
    """
    import cv2
    import numpy as np

    if scales is None:
        scales = [1.0 + i * 0.005 for i in range(-12, 13)]   # 0.94〜1.06
    hx0, hy0, hx1, hy1 = (int(v) for v in head_box)
    ref_g = flat_gray(ref).astype(np.uint8)
    img_g = flat_gray(img).astype(np.uint8)
    tpl = ref_g[hy0:hy1, hx0:hx1]
    if tpl.size == 0:
        raise ValueError(f"頭部矩形が空: {head_box}")

    fields = [(img_g, tpl)]
    if use_edges:
        fields.append((cv2.Canny(img_g, 60, 160), cv2.Canny(tpl, 60, 160)))

    best = None
    for scene, template in fields:
        for k in scales:
            th, tw = int(round(template.shape[0] * k)), int(round(template.shape[1] * k))
            if th < 8 or tw < 8 or th > scene.shape[0] or tw > scene.shape[1]:
                continue
            t = cv2.resize(template, (tw, th), interpolation=cv2.INTER_AREA)
            res = cv2.matchTemplate(scene, t, cv2.TM_CCOEFF_NORMED)
            _, score, _, loc = cv2.minMaxLoc(res)
            if best is None or score > best[0]:
                best = (float(score), float(loc[0]), float(loc[1]), float(k), tw, th)
    if best is None:
        raise RuntimeError("頭部テンプレートの照合に失敗した")

    score, lx, ly, k, tw, th = best
    cimg = (lx + tw / 2.0, ly + th / 2.0)          # img 内の頭中心
    cref = ((hx0 + hx1) / 2.0, (hy0 + hy1) / 2.0)  # ref 内の頭中心
    s = 1.0 / k                                     # img を s 倍すると ref に合う
    return (cref[0] - cimg[0], cref[1] - cimg[1], s, cimg, score)


def apply_similarity(img, dx: float, dy: float, s: float, center):
    """``center`` を中心に ``s`` 倍してから ``(dx, dy)`` 平行移動する（キャンバス不変）。"""
    from PIL import Image

    cx, cy = center
    inv = 1.0 / s
    a, c = inv, cx - cx * inv - dx * inv
    e, f = inv, cy - cy * inv - dy * inv
    return img.transform(img.size, Image.AFFINE, (a, 0, c, 0, e, f),
                         resample=Image.BICUBIC)


# ── キャンバス正規化（キャラ間の見かけを揃える）──────────────

def canvas_transform(img) -> tuple[float, float, float]:
    """キャラの見かけの大きさと足元位置を目標値へ揃える ``(dx, dy, s)`` を返す。

    実測でキャラ間の char_h/canvas_h は 96.2%(yume) と 90.4%(priya) で既に6%ずれており、
    ``scale=-1:320`` は**キャンバス高さ**を揃えるので、そのまま画面上のズレになる。
    キャラごとに ``normal`` から1回だけ算出し、そのキャラの全画像へ同じ変換を掛ける。
    """
    w, h = img.size
    x0, y0, x1, y1 = effective_bbox(img)
    ch = y1 - y0
    if ch <= 0:
        raise ValueError("キャラの高さが取れない")
    s = (h * TARGET_CHAR_FRAC) / ch
    cx = (x0 + x1) / 2.0
    # スケール後の足元を目標位置へ、水平中心をキャンバス中心へ
    dx = (w / 2.0) - cx
    dy = (h * (1.0 - TARGET_BOTTOM_FRAC)) - y1
    return dx, dy, s


def apply_canvas_transform(img, tf: tuple[float, float, float]):
    """``canvas_transform`` の結果を適用する（拡縮の中心は char の水平中心・下端）。"""
    dx, dy, s = tf
    x0, y0, x1, y1 = effective_bbox(img)
    return apply_similarity(img, dx, dy, s, ((x0 + x1) / 2.0, float(y1)))


# ── 領域合成（口・目の移植）──────────────────────────────────

def compose_region_only(
    base_png: Path, src_png: Path, dst_png: Path, *,
    boxes=None, boxes_fn=None, pad=(0.22, 0.22, 0.22, 0.22), pad_px: int = 6,
    feather: int | None = None, color_match: bool = True,
    align_region=None, max_shift: int = 12,
) -> tuple[Path, float]:
    """``src`` の指定領域**だけ**を ``base`` へ移植する（口・目の共通実装）。

    領域は ``boxes`` で直接渡すか、``boxes_fn(base_gray, src_gray, alpha) -> [box, ...]`` で
    **位置合わせ後に**決めさせる（口の bbox は差分から取るので、shift の後でないと決まらない）。

    ``pad`` は ``(top, right, bottom, left)`` の膨張率。目では上を小さくして眉に届かせない。

    境界にアーティファクトを出さないための処理（効く順）:

    1. **アルファでマスクをクリップ**する。これが無いと楕円がキャラ輪郭を越えた部分に
       半透明の肌色が乗り、映像に重ねたときに縁のハローとして出る。
    2. 箱ごとに独立した楕円（両目をまたぐ1つの楕円は鼻梁・前髪を横断するので不可）。
    3. 貼る前の位置合わせは ``align_region``（頭部矩形）に限定する。
    4. 羽根幅は下限2px・上限6px（1024²換算）でクランプ。細すぎるとハードエッジ、
       太すぎると眉へ滲む。
    5. マスク外周リングの平均色差を ``src`` に加算して、生成毎の肌トーン差を吸収する。
    6. アルファは ``base`` のものを保持し、生成側の縁ノイズを持ち込まない。
    """
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    base = Image.open(base_png).convert("RGBA")
    src = Image.open(src_png).convert("RGBA")
    if src.size != base.size:
        src = src.resize(base.size, Image.LANCZOS)

    dx, dy = best_shift(flat_gray(base), flat_gray(src),
                        max_shift=max_shift, region=align_region)
    if (dx, dy) != (0, 0):
        src = src.transform(src.size, Image.AFFINE, (1, 0, -dx, 0, 1, -dy),
                            resample=Image.BICUBIC)

    if boxes is None:
        if boxes_fn is None:
            raise ValueError("boxes か boxes_fn のどちらかが要る")
        boxes = boxes_fn(flat_gray(base), flat_gray(src),
                         np.asarray(base.split()[3]))
    if not boxes:
        raise RuntimeError("移植する領域が空")

    k = base.height / REF_CANVAS
    pt, pr, pb, pl = pad
    mask = Image.new("L", base.size, 0)
    for x0, y0, x1, y1 in boxes:
        w, h = x1 - x0, y1 - y0
        box = (max(0, int(x0 - w * pl) - pad_px), max(0, int(y0 - h * pt) - pad_px),
               min(base.width - 1, int(x1 + w * pr) + pad_px),
               min(base.height - 1, int(y1 + h * pb) + pad_px))
        one = Image.new("L", base.size, 0)
        ImageDraw.Draw(one).ellipse(box, fill=255)
        r = feather if feather is not None else int(round(
            min(max(min(box[2] - box[0], box[3] - box[1]) / 8.0, 2.0 * k), 6.0 * k)))
        one = one.filter(ImageFilter.GaussianBlur(max(1, r)))
        mask = Image.fromarray(np.maximum(np.asarray(mask), np.asarray(one)))

    # 1. アルファでクリップ（縁のハロー対策）
    import cv2

    a = np.asarray(base.split()[3])
    solid = cv2.erode((a > ALPHA_ON).astype(np.uint8) * 255,
                      np.ones((3, 3), np.uint8), iterations=1)
    m = np.minimum(np.asarray(mask), solid)

    if color_match:
        src = _match_tone(base, src, m)

    out = base.copy()
    out.paste(src, (0, 0), Image.fromarray(m))
    out.putalpha(base.split()[3])
    dst_png.parent.mkdir(parents=True, exist_ok=True)
    out.save(dst_png)
    return dst_png, float((m > 8).mean())


def _match_tone(base, src, mask_arr, *, ring: int = 4, limit: int = 8):
    """マスク外周リングの平均色差を ``src`` に加算して肌トーン差を吸収する。

    生成AIは全体の色温度を微妙に変えるので、これが無いと移植部だけ色が違って
    「四角い色ムラ」として境界が見える。``cv2.seamlessClone`` は線画を鈍らせるので使わない。
    """
    import cv2
    import numpy as np

    m = (mask_arr > 8).astype(np.uint8)
    if m.sum() == 0:
        return src
    outer = cv2.dilate(m, np.ones((ring * 2 + 1, ring * 2 + 1), np.uint8), iterations=1)
    ring_mask = (outer > 0) & (m == 0)
    if ring_mask.sum() < 32:
        return src
    nb = np.asarray(base.convert("RGB"), dtype=np.int16)
    ns = np.asarray(src.convert("RGB"), dtype=np.int16)
    off = np.clip(nb[ring_mask].mean(axis=0) - ns[ring_mask].mean(axis=0), -limit, limit)
    from PIL import Image

    adj = np.clip(ns + off.astype(np.int16), 0, 255).astype(np.uint8)
    out = Image.fromarray(adj, "RGB").convert("RGBA")
    out.putalpha(src.split()[3])
    return out


# ── regions.json ────────────────────────────────────────────

def regions_path(char_dir_: Path) -> Path:
    return char_dir_ / "regions.json"


def load_regions(char_dir_: Path) -> dict:
    p = regions_path(char_dir_)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_regions(char_dir_: Path, data: dict) -> Path:
    p = regions_path(char_dir_)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


# ── 検査（すべて無課金・純関数）──────────────────────────────

def check_head_anchor(img, ref, head_box) -> tuple[float, float, float]:
    """登録後の頭部残差 ``(dx, dy, ds)``。顔の位置と見かけの大きさが固定できているか。

    目安: ``|dx|, |dy| <= 2px``、``|ds| <= 0.005``（1024²換算）。
    """
    dx, dy, s, _c, _sc = register_to_head(img, ref, head_box)
    return dx, dy, s - 1.0


def check_face_identity(img, ref, head_box, *, exclude=()) -> float:
    """頭部矩形から目・眉を除いた領域の画素差分率。顔を描き直されていないか。"""
    import numpy as np

    a = flat_gray(ref)
    b = flat_gray(img)
    hx0, hy0, hx1, hy1 = (int(v) for v in head_box)
    m = np.zeros(a.shape, dtype=bool)
    m[hy0:hy1, hx0:hx1] = True
    for x0, y0, x1, y1 in exclude:
        m[max(0, int(y0)):int(y1), max(0, int(x0)):int(x1)] = False
    if not m.any():
        return 0.0
    return float((np.abs(a - b) > 24)[m].mean())


def pose_delta(img, ref, head_box) -> float:
    """頭部**より下**のシルエット IoU（``ref`` 比）。身振りが実際に変わったか。

    ユーザーの「差が分からない」という不満をそのまま数値化した gate。1.0 に近い＝
    ポーズが変わっていない（不満の再発）、低すぎ＝別人。目安は 0.75〜0.97。
    """

    a = alpha_mask(ref)
    b = alpha_mask(img)
    y = int(head_box[3])
    a, b = a[y:], b[y:]
    if a.size == 0:
        return 1.0
    inter = float((a & b).sum())
    union = float((a | b).sum())
    return inter / union if union else 1.0


def check_mouth_closed(img, ref, mouth_box) -> float:
    """口箱内の「開き具合」を ``ref``(normal 口閉じ) 比で返す。1.0 付近なら閉じている。

    ``Mouth fully CLOSED`` はプロンプトで守られないことがある（surprised の "o" 字を実測）。
    移植で機構的に閉じさせる前に、モデルが指示を守ったかをログへ残すための指標。
    """

    x0, y0, x1, y1 = (int(v) for v in mouth_box)
    a = flat_gray(ref)[y0:y1, x0:x1]
    b = flat_gray(img)[y0:y1, x0:x1]
    if a.size == 0:
        return 1.0
    da = float((a < 128).sum())
    db = float((b < 128).sum())
    return db / da if da > 0 else 1.0


def check_patch_seam(img, boxes, *, ring: int = 3) -> float:
    """移植した楕円の境界リングでの勾配が、周囲に比べて突出していないかを測る。

    1.0 付近＝継ぎ目が見えない。大きいほど境界が立っている（アーティファクト）。
    """
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw

    g = flat_gray(img).astype(np.float32)
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.hypot(gx, gy)

    m = Image.new("L", (img.width, img.height), 0)
    d = ImageDraw.Draw(m)
    for b in boxes:
        d.ellipse(tuple(int(v) for v in b), fill=255)
    arr = (np.asarray(m) > 8).astype(np.uint8)
    k = np.ones((ring * 2 + 1, ring * 2 + 1), np.uint8)
    band = (cv2.dilate(arr, k) > 0) & (cv2.erode(arr, k) == 0)
    around = (cv2.dilate(arr, np.ones((ring * 6 + 1,) * 2, np.uint8)) > 0) & (~band)
    if band.sum() < 16 or around.sum() < 16:
        return 1.0
    base = float(mag[around].mean()) or 1.0
    return float(mag[band].mean()) / base
