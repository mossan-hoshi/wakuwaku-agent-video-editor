"""ちびキャラのアセット生成（ベース取り込み・背景抜き・感情×口開閉ペアの画像生成）。

アセットは**全収録で再利用するグローバルキャッシュ**（``assets/chibi/``、untracked・
``WWEDIT_CHIBI_ASSETS`` で差し替え可）。使うキャラ×決定済み感情だけを遅延生成する。

構成:
    assets/chibi/<char>/
      base_raw.webp            # novtube drawable からコピー（tts_chibi_<char>.webp）
      base_rgba.png            # rembg(isnet-anime) で背景抜き済みベース
      regions.json             # 目/口 bbox（第二弾・瞬き用。第一弾は無くてよい）
      <emotion>/
        mouth_closed.png / mouth_open.png   # 生成済み・背景抜き済み RGBA
        mouth_open_gen.png                  # 口領域を合成する前の生成画像（検証用）
        gen_meta.json                       # 生成記録（1枚勝負の台帳）

口パクは**中間フレームを作らない**。閉/開の2枚を離散的に切り替える（ゆっくり系の実際の
作りと同じ）。補間で滑らかに繋ぐと線がボケて「合成っぽさ」が出るため不採用。

画像生成は ``publish.thumbnail.generate_image``（nano banana）を再利用。**課金なので
承認ゲートは CLI 側**（``chibi gen``/``ensure`` が --yes 無しで確認、既存はエラー、
リテイクは --force のみ＝[[paid-image-gen-one-shot-only]]）。
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from wwedit.chibi.emotion import CHIBI_EMOTIONS
from wwedit.common.env import env_value
from wwedit.publish.character import IDENTITY_CONSTRAINT, expression_of

__all__ = [
    "CHIBI_EMOTIONS", "DEFAULT_CHIBI_MODEL", "FALLBACK_CHIBI_MODEL", "N_MOUTH",
    "assets_root", "char_dir", "resolve_chibi_base", "ensure_base", "remove_bg",
    "mouth_pair_paths", "sprite_path", "sprite_paths", "chibi_emotion_prompt",
    "generate_mouth_image", "compose_mouth_only", "missing_assets", "check_pair_alignment",
]

#: 口の状態数（0=閉 / 1=開）。中間フレームは作らない。
N_MOUTH = 2

#: nano banana 2 lite（安価・1K固定）。精度不足なら ``FALLBACK_CHIBI_MODEL`` へ昇格。
DEFAULT_CHIBI_MODEL = "gemini-3.1-flash-lite-image"
FALLBACK_CHIBI_MODEL = "gemini-3-pro-image"

_NOVTUBE_DRAWABLE_DEFAULT = (
    r"C:\Users\sackn\repos2\novtube\android\app\src\main\res\drawable"
)

# 感情ごとの表情プロンプト（ちび絵の記号的表現）。キャラ別上書きは CHAR_EMOTION_OVERRIDE。
# 表情は**目と眉で表す**。口の形は口パク側が決めるので、ここで口に触れると
# 「口を小さく閉じる」指定と綱引きになり、口閉じ画像が笑い口のままになる。
EMOTION_PROMPT = {
    "normal": "neutral calm expression",
    "smile": "happy expression shown by cheerful closed-curve (^_^) eyes and blushing cheeks",
    "surprised": "surprised expression, wide open eyes, raised eyebrows",
    "troubled": "troubled sad expression, downcast eyebrows, sweat drop",
    "angry": "comically angry expression, furrowed brows, puffed cheeks or anger vein",
    "thinking": "thinking expression, eyes looking up to the side, hand near chin if visible",
}

# キャラ個性による上書き（mascot.md 準拠。[[character-personality-mascot-md]]）。
CHAR_EMOTION_OVERRIDE = {
    ("yume", "normal"): "deadpan half-lidded sleepy eyes (jito-me), flat expression, NO smile",
    ("yume", "smile"): "very faint subtle smile while keeping half-lidded sleepy jito-me "
                       "eyes, NOT a big grin",
    ("yume", "thinking"): "half-lidded sleepy jito-me eyes looking sideways, flat expression",
}


def assets_root() -> Path:
    return Path(env_value("WWEDIT_CHIBI_ASSETS") or "assets/chibi")


def char_dir(char: str) -> Path:
    return assets_root() / char


def resolve_chibi_base(char: str) -> Path:
    """novtube の読み上げ画面ちび画像 ``tts_chibi_<char>.webp`` を探す。"""
    drawable = Path(env_value("WWEDIT_NOVTUBE_DRAWABLE") or _NOVTUBE_DRAWABLE_DEFAULT)
    for cand in (drawable / f"tts_chibi_{char}.webp",
                 drawable.parent / "drawable-nodpi" / f"tts_chibi_{char}.webp"):
        if cand.exists():
            return cand
    raise FileNotFoundError(f"ちびベース画像が無い: tts_chibi_{char}.webp（{drawable}）")


_REMBG_SESSION = None


def remove_bg(src: Path, dst: Path) -> Path:
    """rembg(isnet-anime) で背景を抜いた RGBA PNG を書き出す（CPU実行・VRAM消費なし）。"""
    global _REMBG_SESSION
    from PIL import Image
    from rembg import new_session, remove

    if _REMBG_SESSION is None:
        _REMBG_SESSION = new_session("isnet-anime")
    img = Image.open(src).convert("RGBA")
    out = remove(img, session=_REMBG_SESSION)
    dst.parent.mkdir(parents=True, exist_ok=True)
    out.save(dst)
    return dst


def ensure_base(char: str, *, force: bool = False) -> Path:
    """ベースちび画像を取り込み背景抜きする（課金なし・キャッシュ済みなら再利用）。"""
    d = char_dir(char)
    raw = d / "base_raw.webp"
    rgba = d / "base_rgba.png"
    if rgba.exists() and not force:
        return rgba
    d.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(resolve_chibi_base(char), raw)
    return remove_bg(raw, rgba)


def mouth_pair_paths(char: str, emotion: str) -> tuple[Path, Path]:
    d = char_dir(char) / emotion
    return d / "mouth_closed.png", d / "mouth_open.png"


def sprite_path(char: str, emotion: str, mouth: int, eye: int | None = None) -> Path:
    """スプライト解決の一元点。``mouth`` 0=閉 / 1=開。``eye`` は第二弾（瞬き）用の予約。

    第二弾では ``m{mouth}_e{eye}.png`` の行列に拡張する（目パッチ合成・ffmpeg側は不変）。
    """
    closed, open_ = mouth_pair_paths(char, emotion)
    if eye is not None:
        return (char_dir(char) / emotion / f"m{mouth}_e{eye}.png")
    return closed if mouth == 0 else open_


def sprite_paths(char: str, emotion: str) -> list[Path]:
    return [sprite_path(char, emotion, m) for m in range(N_MOUTH)]


def chibi_emotion_prompt(char: str, emotion: str, mouth: str) -> str:
    """ちび感情画像の生成プロンプト（参照画像＝base または同感情の他方の口状態）。"""
    expr = CHAR_EMOTION_OVERRIDE.get((char, emotion)) or EMOTION_PROMPT[emotion]
    if mouth == "closed":
        change = (f"expression changed to: {expr}. Mouth small and fully CLOSED. "
                  "Everything else identical to the reference: same chibi proportions, "
                  "same pose, same costume, same colors, same position in frame.")
    else:
        change = ("ONLY change: the mouth is slightly open. No teeth visible inside the mouth. "
                  "Everything else identical to the reference.")
    return (
        IDENTITY_CONSTRAINT + change +
        " Chibi (super-deformed) style. Keep the EXACT same framing and crop as the "
        "reference (bust-up, character at the same size and position, nothing cut off "
        "that is visible in the reference). Plain solid white background, "
        "NO TEXT, no watermark. "
        f"(character's baseline look: {expression_of(char)})"
    )


def generate_mouth_image(
    char: str, emotion: str, mouth: str, *,
    model: str = DEFAULT_CHIBI_MODEL, force: bool = False, reuse_base: bool = True,
) -> Path:
    """感情×口状態の1枚を生成して背景抜きまで行う（**課金1回**・既存はエラー）。

    参照連鎖: mouth_closed は base_rgba を参照、mouth_open はその感情の mouth_closed を参照。
    ``emotion=="normal"`` の closed は既定でベースをコピーするだけ（課金なし）。ただし
    ベースの口が笑い口などで口パクの閉じ側として不自然なキャラは ``reuse_base=False``
    にして、口を閉じた normal を描かせる（priya がこれに当たる）。
    承認は呼び出し側（CLI の確認ゲート）で済ませてから呼ぶこと。
    """
    closed_p, open_p = mouth_pair_paths(char, emotion)
    target = closed_p if mouth == "closed" else open_p
    if target.exists() and not force:
        raise FileExistsError(f"生成済み: {target}（リテイクは --force。1枚勝負）")
    target.parent.mkdir(parents=True, exist_ok=True)

    base = ensure_base(char)
    if mouth == "closed" and emotion == "normal" and reuse_base:
        shutil.copyfile(base, closed_p)  # ベースは口閉じ想定＝課金なしで流用
        return closed_p

    if mouth == "closed":
        ref = base
    else:
        if not closed_p.exists():
            raise FileNotFoundError(f"先に mouth_closed を作る: {closed_p}")
        ref = closed_p

    from wwedit.publish.thumbnail import generate_image, save_image

    data = generate_image(
        chibi_emotion_prompt(char, emotion, mouth),
        model=model, aspect_ratio="1:1", image_size="1K",
        reference_images=[("image/png" if ref.suffix == ".png" else "image/webp",
                           ref.read_bytes())],
    )
    raw = target.with_name(target.stem + "_raw.png")
    save_image(data, raw)
    remove_bg(raw, target)
    _match_size(target, ref)  # 生成側は 1K 固定なので参照（ベース）寸法へ揃える
    if mouth == "open":
        # 生成AIは口以外も微妙に描き直す。口だけ採って残りは口閉じ画像を使う。
        gen = target.with_name("mouth_open_gen.png")
        shutil.copyfile(target, gen)
        compose_mouth_only(closed_p, gen, target)

    meta_p = target.parent / "gen_meta.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.exists() else {}
    meta[mouth] = {"model": model, "generated_at": datetime.now().isoformat(timespec="seconds"),
                   "ref": str(ref)}
    meta_p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def _flat_gray(img):
    """RGBA を白背景に平坦化したグレースケール配列（位置合わせ・差分用）。"""
    import numpy as np
    from PIL import Image

    rgb = Image.new("RGB", img.size, (255, 255, 255))
    rgb.paste(img, mask=img.split()[3])
    return np.asarray(rgb.convert("L"), dtype=np.int16)


def _best_shift(base, moving, *, max_shift: int = 12) -> tuple[int, int]:
    """``moving`` を ``base`` に重ねる整数平行移動量 (dx, dy) を粗探索する。

    生成画像は微妙に構図がずれるので、口マスク合成の前に全体を合わせておく。
    1/4 縮小で探索してからスケールを戻す（895² で全探索すると重い）。
    """
    import numpy as np

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


def _mouth_bbox(closed_g, open_g, alpha) -> tuple[int, int, int, int]:
    """口が動いた領域の bbox を差分の連結成分から推定する。

    生成AIは目や輪郭も微妙に描き直すので、単純な行列和では眼鏡まで巻き込む。
    差分の連結成分のうち**顔の下半分に重心があり差分量が最大**のものを口とみなす。
    """
    import cv2
    import numpy as np

    ys, xs = np.where(alpha > 16)
    y0, y1, x0, x1 = ys.min(), ys.max(), xs.min(), xs.max()
    ch, cw = y1 - y0, x1 - x0
    mag = np.abs(closed_g - open_g).astype(np.float32)
    binary = (mag > 24).astype(np.uint8)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    n, labels, stats, cent = cv2.connectedComponentsWithStats(binary, connectivity=8)
    ry0, ry1 = y0 + ch * 0.40, y0 + ch * 0.85   # 口がありうる帯（目より下・あご上）
    rx0, rx1 = x0 + cw * 0.25, x0 + cw * 0.75
    best, best_s = None, 0.0
    for i in range(1, n):
        cxi, cyi = cent[i]
        if not (rx0 <= cxi <= rx1 and ry0 <= cyi <= ry1):
            continue
        s = float(mag[labels == i].sum())
        if s > best_s:
            bx, by, bw, bh = stats[i, :4]
            best_s, best = s, (int(bx), int(by), int(bx + bw), int(by + bh))
    if best is None:
        raise RuntimeError("口の差分が検出できない（生成画像が参照と同じ／位置が想定外）")
    return best


def compose_mouth_only(closed: Path, generated: Path, dst: Path) -> tuple[Path, float]:
    """生成画像から**口領域だけ**を口閉じ画像に合成する（口以外のブレを完全に消す）。

    生成AIは口以外も微妙に描き直してしまうので、位置合わせ→口bbox推定→楕円ぼかし
    マスクで口だけ差し替える。返り値は (出力パス, 口マスクが占める面積比)。
    """
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    c = Image.open(closed).convert("RGBA")
    g = Image.open(generated).convert("RGBA")
    if g.size != c.size:
        g = g.resize(c.size, Image.LANCZOS)
    dx, dy = _best_shift(_flat_gray(c), _flat_gray(g))
    if (dx, dy) != (0, 0):
        g = g.transform(g.size, Image.AFFINE, (1, 0, -dx, 0, 1, -dy),
                        resample=Image.BICUBIC)
    alpha = np.asarray(c.split()[3])
    x0, y0, x1, y1 = _mouth_bbox(_flat_gray(c), _flat_gray(g), alpha)
    pw, ph = int((x1 - x0) * 0.22) + 6, int((y1 - y0) * 0.22) + 6
    box = (max(0, x0 - pw), max(0, y0 - ph),
           min(c.width - 1, x1 + pw), min(c.height - 1, y1 + ph))
    mask = Image.new("L", c.size, 0)
    ImageDraw.Draw(mask).ellipse(box, fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(max(3, min(box[2] - box[0],
                                                           box[3] - box[1]) // 10)))
    out = c.copy()
    out.paste(g, (0, 0), mask)
    out.putalpha(c.split()[3])  # 輪郭は口閉じ側を保つ（生成側の縁ノイズを持ち込まない）
    dst.parent.mkdir(parents=True, exist_ok=True)
    out.save(dst)
    return dst, float((np.asarray(mask) > 8).mean())


def _match_size(target: Path, ref: Path) -> Path:
    """生成画像を参照画像と同寸にリサイズする（RIFE補間は同寸必須・重ね位置も揃う）。"""
    from PIL import Image

    with Image.open(ref) as r:
        size = r.size
    img = Image.open(target).convert("RGBA")
    if img.size != size:
        img.resize(size, Image.LANCZOS).save(target)
    return target


def missing_assets(chars: list[str], emotions: list[str]) -> list[tuple[str, str, str]]:
    """不足アセット (char, emotion, what) を列挙する。what ∈ {base, closed, open}。"""
    out: list[tuple[str, str, str]] = []
    for c in chars:
        if not (char_dir(c) / "base_rgba.png").exists():
            out.append((c, "", "base"))
        for e in emotions:
            closed_p, open_p = mouth_pair_paths(c, e)
            if not closed_p.exists():
                out.append((c, e, "closed"))
            if not open_p.exists():
                out.append((c, e, "open"))
    return out


def check_pair_alignment(closed_png: Path, open_png: Path) -> float:
    """口領域（中央下寄り）以外の画素差分率を返す（0=完全一致）。

    口開/口閉ペアの位置ドリフト検知（閾値超は「顔が泳ぐ」原因）。第一弾は警告のみで、
    リテイク判断はユーザー（[[paid-image-gen-one-shot-only]]）。
    """
    import numpy as np
    from PIL import Image

    a = Image.open(closed_png).convert("RGBA")
    b = Image.open(open_png).convert("RGBA")
    if a.size != b.size:
        b = b.resize(a.size)
    na = np.asarray(a, dtype=np.int16)
    nb = np.asarray(b, dtype=np.int16)
    h, w = na.shape[:2]
    mask = np.ones((h, w), dtype=bool)
    # 口が動く領域（中央・下半分寄り）を除外
    mask[int(h * 0.45):int(h * 0.85), int(w * 0.30):int(w * 0.70)] = False
    diff = (np.abs(na - nb).max(axis=2) > 24)
    return float(diff[mask].mean())
