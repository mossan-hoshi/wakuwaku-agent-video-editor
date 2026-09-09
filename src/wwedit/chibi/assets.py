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

from wwedit.chibi import geometry as _G
from wwedit.chibi.emotion import CHIBI_EMOTIONS
from wwedit.common.env import env_value
from wwedit.publish.character import IDENTITY_CONSTRAINT, expression_of
from wwedit.publish.thumbnail import NANO_BANANA_2, NANO_BANANA_2_LITE

__all__ = [
    "CHIBI_EMOTIONS", "DEFAULT_CHIBI_MODEL", "BASE_CHIBI_MODEL", "N_MOUTH",
    "assets_root", "char_dir", "resolve_chibi_base", "ensure_base", "remove_bg",
    "mouth_pair_paths", "sprite_path", "sprite_paths", "scaled_sprite",
    "chibi_emotion_prompt",
    "generate_mouth_image", "compose_mouth_only", "missing_assets", "check_pair_alignment",
    "EMOTION_GESTURE", "CHAR_GESTURE_NOTE", "CHAR_NG",
    "detect_regions", "head_box_of", "register_closed",
    "CHIBI_STYLE_REFS", "chibi_base_prompt", "generate_chibi_base", "base_meta",
    "trim_chibi_base",
    "NO_BLINK_EMOTIONS", "EYES_ALREADY_CLOSED", "BLINKABLE_EMOTIONS",
    "eyes_closed_prompt", "generate_eyes_closed", "build_eye_patches", "blink_emotions",
    "plan_generation", "paid_jobs",
]

#: 口の状態数（0=閉 / 1=開）。中間フレームは作らない。
N_MOUTH = 2

#: 感情×口の量産（1キャラ12枚）は lite。
DEFAULT_CHIBI_MODEL = NANO_BANANA_2_LITE
#: ベースの描き起こしは**キャラあたり1枚**で全12枚の土台になるので nano2 本体を使う。
BASE_CHIBI_MODEL = NANO_BANANA_2

_NOVTUBE_DRAWABLE_DEFAULT = (
    r"C:\Users\sackn\repos2\novtube\android\app\src\main\res\drawable"
)

#: ちび絵の identity の SoT（`mascot.md` §3 が指す character_refs）。**1024²**。
#: `tts_chibi_<char>.webp` は同じ絵を 480² へ縮めた読み上げ画面用アイコンで、
#: これを参照に投げると生成AIが線と塗りを補完し直して identity が流れる
#: （実測: 頬のチークが消え、目が細長くなり、口が一文字になった＝「つんとしすぎ」）。
#: **必ず高解像度の `<char>_00.webp` を優先する。**
_NOVTUBE_CHARACTER_REFS_DEFAULT = (
    r"C:\Users\sackn\repos2\novtube4\tool\achievements\character_refs"
)

# 感情ごとの表情プロンプト（ちび絵の記号的表現）。キャラ別上書きは CHAR_EMOTION_OVERRIDE。
# 表情は**目と眉で表す**。口の形は口パク側が決めるので、ここで口に触れると
# 「口を小さく閉じる」指定と綱引きになり、口閉じ画像が笑い口のままになる。
#: ベースの表情が `normal`（平常）として使えないキャラ。ここに入れると口閉じの normal を
#: **描き直す**（＝課金+1枚）。
#:
#: ⚠️ ベースは「読み上げ画面のアイコン」用に描かれていて、平常表情とは限らない。
#: noa は**片目ウインク**、priya は**笑い口**。ウインクをそのまま normal にすると、
#: 平常時ずっと片目をつぶった絵になるうえ、**瞬きの目パッチが左右非対称に壊れる**
#: （基準の片目が既に閉じているので差分が片目しか出ない）。
#: 新しいキャラを足すときは `chibi ensure-all --dry-run` の前に必ずベースを目視すること。
REDRAW_CLOSED_CHARS = frozenset({"noa", "priya"})

EMOTION_PROMPT = {
    # ⚠️ 「neutral」だけだと生成AIは**つんとした無表情**に寄せてくる（実測: noa の
    #    normal が口一文字・目が細長い険しい顔になった）。ちび絵の平常は「柔らかく穏やか」。
    "normal": "calm relaxed everyday expression with BOTH EYES OPEN and looking forward "
              "(never winking, never one eye closed), eyebrows relaxed and level, "
              "warm and soft and approachable — NOT stern, NOT cold, NOT a sharp glare",
    # ⚠️ 「blushing cheeks」を書いていた（2026-08-06 削除）。表情は**目と眉**で作る。
    #    頬の描き込みはキャラの見た目＝参照画像の担当で、こちらから足すものではない。
    "smile": "happy expression shown by cheerful closed-curve (^_^) eyes",
    "surprised": "surprised expression, wide open eyes, raised eyebrows",
    "troubled": "troubled sad expression, downcast eyebrows, sweat drop",
    "angry": "comically angry expression, furrowed brows, puffed cheeks or anger vein",
    "thinking": "thinking expression, eyes looking up to the side, hand near chin if visible",
}

# 感情ごとの**上半身の身振り**。目と眉だけの差分では 320px に縮めた時に誰も気づかない
# （実測: yume の normal/surprised/thinking はシルエットの IoU が 0.995 以上＝ほぼ同一）。
#
# ⚠️ 動かすのは**肩・腕・上体の傾きだけ**。頭の位置・大きさ・向きは動かさない
# （切り替えたときに顔が泳がないため）。
#
# ⚠️ **両手を体の前へ寄せる指示を書かないこと**。`chibi base-gen` で描き起こしたベースは
# 手に何も持っていないので、「胸元へ寄せる」と両手が重なって**団子状の塊**になる
# （2026-08-06 実測・smile で発生）。手は「体の横」「背中」「握り拳」のように、
# **左右が触れ合わない位置**へ置く。旧版は各キャラが小物を持っていた前提で
# 「whatever the character holds」と書いていたが、新ベースには小物が無いので廃止した。
EMOTION_GESTURE = {
    "normal": "",
    "smile": "shoulders lifted a little and the upper body leaning slightly forward, "
             "both hands taken behind the back so the forearms disappear behind the body",
    # ⚠️ 旧版「両腕を体の横で少し開く」は troubled と同じ罠で pose_iou 0.998 に張り付いた
    #    （souta・2026-08-06）。腕を**上げて**輪郭を変える。顔は隠さない高さに置く。
    "surprised": "both shoulders jerked up toward the ears and the upper body leaning "
                 "slightly back, both arms bent up so that the hands are raised beside the "
                 "head at about ear height, well apart from each other and clear of the face",
    # ⚠️ 旧版「両腕をだらりと下げる」は**シルエットが normal とほぼ同じ**で、
    #    noa 0.993 / souta 0.999 と2キャラ連続で pose_iou が閾値に張り付いた（2026-08-06）。
    #    肘を外へ張り出させて**輪郭そのもの**を変える。thinking（顎に拳）と紛れないよう、
    #    手は後頭部に置く。
    "troubled": "one shoulder dropped and the upper body tilted to one side, one arm bent "
                "up so that the hand rests on the back of the head as if scratching it, "
                "with that elbow sticking clearly out to the side; the other arm hangs "
                "limply down at the side",
    "angry": "shoulders squared and the upper body leaning forward, both hands closed into "
             "small fists held down at the sides, elbows pushed slightly out",
    "thinking": "the upper body tilted to one side, one elbow raised so that the knuckles "
                "of that hand rest lightly against the side of the chin; the other arm "
                "stays down at the side",
}

#: 身振りを付けるときに必ず添える手の描き方の制約。
#: ⚠️ **口を隠させないこと**。口パクで差し替えるのは口だけなので、手が口に掛かると
#: 開閉のたびに手の上に口が現れて破綻する。
GESTURE_HAND_RULE = (
    "Hands stay simple rounded chibi shapes without separated fingers. The two hands must "
    "NEVER touch, overlap or interlock with each other, and nothing may cover the mouth — "
    "the mouth stays fully visible and unobstructed. "
)

# 身振りの振れ幅をキャラの性格で抑える（mascot.md 準拠）。
CHAR_GESTURE_NOTE = {
    "yume": "Keep the motion small and low-energy; she is a reclusive gamer who never "
            "gets visibly excited.",
    "tsukasa": "Keep the motion restrained; he is a cool, reserved teen who would not "
               "get loudly worked up.",
    "suzu": "Keep the motion small and timid, with her slightly hunched shoulders.",
    "reika": "Keep the motion calm and composed; she is an adult holding a sleeping dog.",
}

# キャラ別の禁止事項。identity は参照画像が担保するので、**絶対に避けるものだけ**を書く。
CHAR_NG = {
    "souta": "Never add club-related items (no ball, no instrument, no camera, no game device).",
    "tsukasa": "Avoid smug or mocking expressions and peace-sign gestures.",
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
    """ちびベース画像を探す。**character_refs の 1024² を最優先**。

    探索順:

    1. ``character_refs/<char>_00.webp``（identity の SoT・1024²・全9キャラ揃っている）
    2. ``tts_chibi_<char>.webp``（同じ絵の 480² 版・フォールバック）

    2 を参照に投げると生成AIが低解像度を補完し直して identity が流れるので、
    **1 が存在する限り 1 を使う**。差し替えは ``WWEDIT_NOVTUBE_CHARACTER_REFS`` /
    ``WWEDIT_NOVTUBE_DRAWABLE`` で。
    """
    refs = Path(env_value("WWEDIT_NOVTUBE_CHARACTER_REFS")
                or _NOVTUBE_CHARACTER_REFS_DEFAULT)
    hi = refs / f"{char}_00.webp"
    if hi.exists():
        return hi
    drawable = Path(env_value("WWEDIT_NOVTUBE_DRAWABLE") or _NOVTUBE_DRAWABLE_DEFAULT)
    for cand in (drawable / f"tts_chibi_{char}.webp",
                 drawable.parent / "drawable-nodpi" / f"tts_chibi_{char}.webp"):
        if cand.exists():
            return cand
    raise FileNotFoundError(
        f"ちびベース画像が無い: {char}_00.webp（{refs}） / tts_chibi_{char}.webp（{drawable}）"
    )


_REMBG_SESSION = None


def _keep_existing_alpha(img):
    """既に**手で切り抜かれた**画像ならそのアルファを尊重する（抜き直さない）。

    ユーザーが `base_gen.png` を手修正して透過PNGで置いた場合に、こちらが勝手に
    抜き直して台無しにしないためのガード。アルファが「実際に使われている」＝
    完全透明が1割以上あり、かつ不透明も1割以上あることを条件にする。
    """
    import numpy as np

    if img.mode not in ("RGBA", "LA", "PA"):
        return None
    a = np.asarray(img.convert("RGBA"))[..., 3]
    if (a < 16).mean() < 0.10 or (a > 239).mean() < 0.10:
        return None
    return img.convert("RGBA")


def remove_bg(src: Path, dst: Path, *, size: tuple[int, int] | None = None) -> Path:
    """背景を抜いた RGBA PNG を書き出す。

    優先順位は ① 既にあるアルファ（手で切り抜いた画像）→ ② 白背景マット → ③ rembg。

    生成画像はプロンプトで**純白背景**を要求しているので、
    :func:`geometry.white_bg_matte` で抜く。rembg より境界が綺麗
    （rembg は低解像度マスクを引き伸ばすため輪郭線を削り、**暗背景で白いハロ**が出る）。
    白背景でなければ rembg(isnet-anime) へ落ちる（CPU実行・VRAM消費なし）。

    ``size`` を渡すとそこまで縮小する。生成画像は 1px 硬エッジ（アンチエイリアス無し）
    なので、**大きく引いてから落とす**この縮小が実質のアンチエイリアスになる。
    """
    global _REMBG_SESSION
    from PIL import Image

    img = Image.open(src)
    out = _keep_existing_alpha(img) or _G.white_bg_matte(img)
    if out is None:
        from rembg import new_session, remove

        if _REMBG_SESSION is None:
            _REMBG_SESSION = new_session("isnet-anime")
        out = remove(img.convert("RGBA"), session=_REMBG_SESSION)
    if size is not None and out.size != tuple(size):
        out = out.resize(size, Image.LANCZOS)
    out = _G.drop_specks(out)   # 本体から離れた浮遊ゴミを落とす
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


#: ちびベースを**新規に描き起こす**ときの画風・構図の参照に使う既存ちび絵（キャラID）。
#: identity は当人のフルアートが持ち、これは「線の太さ・頭身・切り取り方」だけを担う。
#: 背景に白い板や丸が焼き込まれている素材（tsukasa/suzu/souta）は避ける。
CHIBI_STYLE_REFS = ("kasumi", "priya")

_BASE_META = "base_meta.json"


def base_meta(char: str) -> dict:
    """ベースの出自（``asset`` = 既存素材そのまま / ``generated`` = 描き起こし）。"""
    p = char_dir(char) / _BASE_META
    if not p.exists():
        return {"source": "asset"}
    return json.loads(p.read_text(encoding="utf-8"))


def chibi_base_prompt(char: str) -> str:
    """ちびベースを描き起こすプロンプト（参照は フルアート → 自分のちび絵 → 他人のちび絵）。

    既存のちび絵は**キメポーズ**（noa=片手を腰＋ウインク）だったり構図が全身だったりして、
    口パクの土台に向かない。ここでは「誰か」をフルアートから、「どう描くか」を既存ちび絵から
    取り、**腰から上・正面・両腕を自然に下ろした中立の立ち姿**へ描き直す。
    """
    return (
        "You are given several reference images. "
        # ⚠️ 1枚目を「full-body art」と紹介すると**構図まで**引っ張られて全身が返る（実測）。
        #    役割を「identity だけ」と明示し、構図は参照するなと先に釘を刺す。
        "REFERENCE 1 is the character's official illustration. Use it ONLY to know WHO this "
        "is: the face, the hair (colour, length, cowlick), the eye colour, and the design and "
        "colours of the clothes. IGNORE its framing, its crop and its camera distance "
        "completely — you are NOT reproducing that picture. "
        "REFERENCE 2 is the SAME character already drawn in this project's chibi style. "
        "THE REMAINING REFERENCES ARE DIFFERENT CHARACTERS drawn in that same chibi style: "
        "use them ONLY for art style, line weight, head-to-body proportion, colouring and "
        "framing. NEVER copy their faces, hair, hair colour or clothes. "
        "TASK: draw a NEW chibi (super-deformed) illustration of the character from "
        "REFERENCE 1, in exactly the chibi art style of the other references. "
        # 構図はユーザー確定仕様（2026-08-06）: priya/yume と揃えて**腰まで・下半身は描かない**。
        # 全身にすると実寸320pxで顔が他キャラの半分になり、並べたときに破綻する。
        # 「waist-up」だけでは効かず太ももまで描かれた。**画像の下辺が腰を横切る**と
        # 物理的に言い切り、外に出すものを一つずつ数え上げる。
        "FRAMING — this is the most important requirement, and it is NOT a full-body "
        "illustration: the BOTTOM EDGE OF THE IMAGE CUTS ACROSS THE CHARACTER'S WAIST. "
        "Draw ONLY the head, the neck, the shoulders, the chest and the arms. "
        "The hips, the belt line, the trousers, the skirt, the shorts, the thighs, the "
        "knees, the legs, the feet and the shoes are ALL OUTSIDE the image and must not "
        "appear anywhere. Crop exactly like the chibi style references, which all end at the "
        "waist. The head is huge in the chibi style: it must take up ABOUT HALF of the "
        "character's drawn height. Character centred and facing the camera straight on, "
        "filling the frame the same way the chibi references do. "
        # 「standing pose」は脚を要求してしまうので使わない。
        "POSE: a relaxed, natural, neutral posture — both shoulders level and both arms "
        "hanging down loosely at the sides (the hands may run off the bottom edge). "
        "NO posing and NO attitude — no hand on the hip, no crossed arms, no peace sign, no "
        "leaning, no hair flip, no props and nothing held in the hands. This is the neutral "
        "resting pose that every other expression is drawn from, so it must be as plain as "
        "possible. "
        "FACE: BOTH EYES OPEN and looking straight at the camera (never winking, never one "
        # ⚠️ ここに書いてよいのは**下流の工程が要求する状態**だけ（両目を開ける＝瞬きパッチ、
        #    口を閉じる＝口パクの土台、眉は水平＝他の感情の基準）。頬の色・肌・髪・服のような
        #    **見た目は参照画像が持っている**ので、こちらから指定しない
        #    （2026-08-06・「頬っぺたピンク」を勝手に足して別人にした）。
        "eye closed), eyebrows relaxed and level, mouth small and fully closed with a soft "
        "natural curve. Do not add anything to the face that is not in the reference. "
        f"(character's baseline look: {expression_of(char)}) "
        "Plain solid white background, NO TEXT, no watermark, no frame, no border, "
        "no background plate or circle behind the character. Square 1:1 image."
    )


def generate_chibi_base(
    char: str, *, model: str = BASE_CHIBI_MODEL, force: bool = False,
    style_refs: tuple[str, ...] = CHIBI_STYLE_REFS,
) -> Path:
    """ちびベースを**描き起こして** ``base_rgba.png`` にする（**課金1回**・既存はエラー）。

    既存素材をそのまま使う :func:`ensure_base` の代わりに使う。描き起こしたベースは
    「両目を開けた中立・口閉じ」なので、``normal`` の口閉じはこれをコピーするだけで済む
    （``base_meta.json`` の ``source=generated`` を見て :func:`generate_mouth_image` が判断）。
    """
    from wwedit.publish.character import DEFAULT_ASSETS, resolve_character_ref
    from wwedit.publish.thumbnail import generate_image, save_image

    d = char_dir(char)
    rgba = d / "base_rgba.png"
    if rgba.exists() and not force:
        raise FileExistsError(f"ベース生成済み: {rgba}（引き直しは --force。1枚勝負）")
    d.mkdir(parents=True, exist_ok=True)

    def _ref(p: Path) -> tuple[str, bytes]:
        mime = "image/png" if p.suffix.lower() == ".png" else "image/webp"
        return mime, p.read_bytes()

    refs = [_ref(resolve_character_ref(char, DEFAULT_ASSETS))]
    try:
        refs.append(_ref(resolve_chibi_base(char)))       # 自分の既存ちび絵（画風＋顔）
    except FileNotFoundError:
        pass
    for other in style_refs:
        if other == char:
            continue
        try:
            refs.append(_ref(resolve_chibi_base(other)))  # 他キャラのちび絵（画風・構図のみ）
        except FileNotFoundError:
            continue

    data = generate_image(chibi_base_prompt(char), model=model,
                          aspect_ratio="1:1", image_size="2K", reference_images=refs)
    raw = d / "base_gen.png"
    save_image(data, raw)
    # 幾何は 1024² 前提（geometry.REF_CANVAS）。2K で引いて**マットを取ってから**落とす
    # ＝縮小がアンチエイリアスになる。抜いた後に落とすと縁が白く滲む。
    remove_bg(raw, rgba, size=(_G.REF_CANVAS, _G.REF_CANVAS))
    (d / _BASE_META).write_text(json.dumps({
        "source": "generated", "model": model, "n_refs": len(refs),
        "at": datetime.now().isoformat(timespec="seconds"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return rgba


def trim_chibi_base(char: str, bottom_frac: float) -> Path:
    """ベースの**下側を切って腰上に詰める**（無課金・何度でもやり直せる）。

    生成AIは「腰で切れ」と書いても脚まで描くことがある。引き直すより切るほうが安いので、
    ``bottom_frac``（キャラ実効高さに対する残す割合）で下を落として、キャンバスを
    :func:`geometry.canvas_transform` で正規化し直す。

    切る前の絵は ``base_untrimmed.png`` に退避し、**常にそこから切り直す**ので、
    フラクションを変えて何度呼んでも劣化しない。
    """
    from PIL import Image

    if not 0.05 < bottom_frac <= 1.0:
        raise ValueError(f"bottom_frac は (0.05, 1.0] : {bottom_frac}")
    d = char_dir(char)
    rgba = d / "base_rgba.png"
    if not rgba.exists():
        raise FileNotFoundError(f"ベースが無い: {rgba}")
    src = d / "base_untrimmed.png"
    if not src.exists():
        shutil.copyfile(rgba, src)

    img = Image.open(src).convert("RGBA")
    x0, y0, x1, y1 = _G.effective_bbox(img)
    cut = y0 + int((y1 - y0) * bottom_frac)
    img = img.crop((0, 0, img.width, max(cut, y0 + 1)))
    canvas = Image.new("RGBA", (_G.REF_CANVAS, _G.REF_CANVAS), (0, 0, 0, 0))
    canvas.alpha_composite(img, (0, 0))
    out = _G.apply_canvas_transform(canvas, _G.canvas_transform(canvas))
    out.save(rgba)

    meta = base_meta(char)
    meta["trim_bottom_frac"] = bottom_frac
    (d / _BASE_META).write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                encoding="utf-8")
    return rgba


def mouth_pair_paths(char: str, emotion: str) -> tuple[Path, Path]:
    d = char_dir(char) / emotion
    return d / "mouth_closed.png", d / "mouth_open.png"


def sprite_path(char: str, emotion: str, mouth: int, eye: int | None = None) -> Path:
    """スプライト解決の一元点。``mouth`` 0=閉 / 1=開、``eye`` 0=開 / 1=閉（瞬き）。

    ``eye`` が None か 0 のときは既存の ``mouth_closed/open.png`` をそのまま指す
    （**e0 の実ファイルは作らない**＝1024² RGBA を倍に増やさないため）。瞬きで目を閉じる
    ``eye>=1`` のときだけ ``m{mouth}_e{eye}.png`` の行列を使う。
    """
    closed, open_ = mouth_pair_paths(char, emotion)
    if eye:
        return (char_dir(char) / emotion / f"m{mouth}_e{eye}.png")
    return closed if mouth == 0 else open_


#: 表示サイズへ縮めたスプライトの置き場（アセット直下・**再利用する**）。
SCALED_DIRNAME = "_scaled"


def scaled_sprite(src: Path, height: int) -> Path:
    """スプライトを**表示する高さへ縮めた実ファイル**を返す（無ければ作る）。

    素のスプライトは 1024x1024 RGBA。画面では 320px 程度でしか使わないのに、
    ffmpeg の ffconcat はフレームごとに元PNGをデコードして ``scale`` するので、
    **合成時間の 29%**（実測: 60秒の合成のうち 19.0秒）がここに消えていた。
    先に縮めておけばデコード量が約1/10になり、``scale`` も要らなくなる。

    元より大きい指定は縮めない（拡大は画質を落とすだけ）。
    **読めない画像は元のまま返す**——高速化のための仕掛けで合成を止めない。
    """
    from PIL import Image

    if height <= 0:
        return src
    dst = src.parent / SCALED_DIRNAME / f"h{height}" / src.name
    try:
        if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
            return dst
        im = Image.open(src).convert("RGBA")
        if im.height <= height:
            return src
        w = max(1, round(im.width * height / im.height))
        dst.parent.mkdir(parents=True, exist_ok=True)
        im.resize((w, height), Image.LANCZOS).save(dst)
        return dst
    except Exception:
        return src


def sprite_paths(char: str, emotion: str) -> list[Path]:
    return [sprite_path(char, emotion, m) for m in range(N_MOUTH)]


def chibi_emotion_prompt(char: str, emotion: str, mouth: str) -> str:
    """ちび感情画像の生成プロンプト（参照画像＝base または同感情の他方の口状態）。

    ``closed`` 側は KEEP（変えない）／CHANGE（変える）／MOUTH（口の再掲）の3ブロック構成。
    旧版は ``same pose, same position in frame`` を KEEP に含めていたため、身振りの指示が
    完全に打ち消されていた（`thinking` の「手を顎に」すら無視されていた）。ポーズを動かす
    以上、KEEP から pose を外し、代わりに**頭の位置・大きさ・向き**を名指しで固定する。

    ``open`` 側は変更しない（同感情の closed を参照する差分生成で、ポーズは closed が持つ）。
    """
    expr = CHAR_EMOTION_OVERRIDE.get((char, emotion)) or EMOTION_PROMPT[emotion]
    if mouth != "closed":
        return (
            IDENTITY_CONSTRAINT +
            "ONLY change: the mouth is slightly open. No teeth visible inside the mouth. "
            "Everything else identical to the reference."
            " Chibi (super-deformed) style. Keep the EXACT same framing and crop as the "
            "reference (bust-up, character at the same size and position, nothing cut off "
            "that is visible in the reference). Plain solid white background, "
            "NO TEXT, no watermark. "
            f"(character's baseline look: {expression_of(char)})"
        )

    gesture = EMOTION_GESTURE.get(emotion, "")
    note = CHAR_GESTURE_NOTE.get(char, "")
    ng = CHAR_NG.get(char, "")
    keep = (
        "KEEP EXACTLY AS IN THE REFERENCE: the framing and crop; the size, position and "
        "facing direction of the head; the art style and line weight; the hair; the "
        "costume; and everything the character wears, holds or carries — do not put "
        "anything down, do not swap or remove what is held. Do NOT zoom out or re-frame "
        "to fit the new pose. "
        # ⚠️ 顔の作りを名指しで固定する。書かないと生成AIは「絵柄を整えて」しまい、
        #    目を細長くし、まつげを増やす（＝別人化・実測済み）。
        # ⚠️ ここは**維持**の指示だけを書く。「頬をピンクに」のような**見た目の追加指定は
        #    絶対に書かない** — 見た目は参照画像が持っている（2026-08-06・ユーザー指摘。
        #    公式ちび絵は9キャラ全員チーク無しなのに、頬紅を足す指示が入っていた）。
        # ⚠️ 維持の指示を**長く書きすぎない**。頬について3文使ったら surprised/troubled の
        #    pose_iou が 0.71→0.98 / 0.94→0.999 に悪化した（＝「何も変えない」へ効いた）。
        #    維持は一息で言い切り、変更点（CHANGE）に文量を残す。
        "KEEP THE FACE ITSELF: the eye shape and eye size (large round chibi eyes — never "
        "narrower, never more slanted), the light simple eyelashes (do not add heavy or "
        "long lashes), the eyebrow thickness, the face outline and the cheeks exactly as "
        "they are drawn in the reference. "
    )
    change = f"CHANGE: the expression, shown with the eyes and eyebrows — {expr}. "
    if gesture:
        change += (
            f"Also change the upper-body posture — {gesture}. Move the arms, shoulders "
            "and upper body by roughly 5-10% of the body height: clearly different at a "
            "glance, but small. The head itself must not move or turn. "
        ) + GESTURE_HAND_RULE
    # ⚠️ 「320pxで違いが明白に」は**身振りのある感情だけ**に付ける。`normal` は他の全感情の
    #    基準（＝登録先・口パクの土台）で、変えるのはベースのウインクを開けることだけ。
    #    ここに「違いを明白に」と書くと平常顔を誇張しにいって別人になる（実測: 険しい顔）。
    readable = (
        "This image is displayed about 320 px tall at the bottom of a video and is swapped "
        "in place with the other emotions, so the difference must be obvious at that size "
        "— a change you can only notice by comparing the images side by side is not enough. "
    ) if gesture else ""
    return (
        IDENTITY_CONSTRAINT + keep + change + (f"{note} " if note else "") +
        (f"Constraint: {ng} " if ng else "") + readable +
        "Chibi (super-deformed) style. Plain solid white background, NO TEXT, no watermark. "
        # 口は「閉じている」だけを要求し、**形は参照から受け継がせる**。ここで
        # 「小さく閉じる」とだけ書くと一文字の険しい口になり、キャラの柔らかさが飛ぶ。
        "MOUTH: Mouth small and fully CLOSED (lips together, no gap, no teeth), keeping "
        "the same gentle mouth shape as in the reference — do not flatten it into a "
        "stern straight line. "
        f"(character's baseline look: {expression_of(char)})"
    )


def generate_mouth_image(
    char: str, emotion: str, mouth: str, *,
    model: str = DEFAULT_CHIBI_MODEL, force: bool = False,
    reuse_base: bool | None = None,
) -> Path:
    """感情×口状態の1枚を生成して背景抜きまで行う（**課金1回**・既存はエラー）。

    参照連鎖: mouth_closed は base_rgba を参照、mouth_open はその感情の mouth_closed を参照。
    ``emotion=="normal"`` の closed は既定でベースをコピーするだけ（課金なし）。ただし
    ベースの口が笑い口などで口パクの閉じ側として不自然なキャラは ``reuse_base=False``
    にして、口を閉じた normal を描かせる（priya がこれに当たる）。
    承認は呼び出し側（CLI の確認ゲート）で済ませてから呼ぶこと。
    """
    if reuse_base is None:
        # ベースが平常表情でないキャラ（noa=ウインク / priya=笑い口）は描き直す。
        # ここを既定にしておかないと、平常時ずっと片目をつぶった絵になる。
        # ただし `chibi base-gen` で**描き起こした**ベースは中立に描かせてあるので流用でよい
        # （＝キャラあたり課金1枚を節約できる）。
        reuse_base = (base_meta(char).get("source") == "generated"
                      or char not in REDRAW_CLOSED_CHARS)
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
    if mouth == "closed":
        # ポーズを動かす以上、キャンバス内でのキャラの大きさ・位置は必ずブレる。
        # 頭部を基準に normal へ登録して揃える（合成は キャンバス高さ を 320 にするため）。
        register_closed(char, emotion, target)
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


# 幾何（実効領域・位置合わせ・領域合成・検査）は chibi.geometry が持つ。ポーズを動かす
# ようになってキャンバス固定率の帯が使えなくなったため、そちらへ集約した。
_flat_gray = _G.flat_gray
_best_shift = _G.best_shift
_mouth_bbox = _G.mouth_bbox


def compose_mouth_only(closed: Path, generated: Path, dst: Path) -> tuple[Path, float]:
    """生成画像から**口領域だけ**を口閉じ画像に合成する（口以外のブレを完全に消す）。

    生成AIは口以外も微妙に描き直してしまうので、位置合わせ→口bbox推定→楕円ぼかし
    マスクで口だけ差し替える。返り値は (出力パス, 口マスクが占める面積比)。

    実測（既存アセット）: 口以外の drift は 0.000000、境界リングの勾配比は 0.04〜0.10
    （1.0=継ぎ目なしの基準を下回る＝境界が立っていない）。8倍に拡大しても継ぎ目は見えない。
    """
    return _G.compose_region_only(
        closed, generated, dst,
        boxes_fn=lambda bg, sg, a: [_G.mouth_bbox(bg, sg, a)],
        color_match=False,   # 口は無補正で drift 0 を達成できている（既存挙動を維持）
    )


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
    # 口が動く領域を除外。regions があればその口 bbox を使う（ポーズが動くとキャンバス
    # 固定率の帯は口を外すため）。無ければ従来どおり中央下寄りの固定帯。
    mb = _G.load_regions(closed_png.parent.parent).get("mouth_box")
    if mb:
        pad = 12
        mask[max(0, mb[1] - pad):mb[3] + pad, max(0, mb[0] - pad):mb[2] + pad] = False
    else:
        mask[int(h * 0.45):int(h * 0.85), int(w * 0.30):int(w * 0.70)] = False
    diff = (np.abs(na - nb).max(axis=2) > 24)
    return float(diff[mask].mean())


# ── 幾何の確定（regions.json）────────────────────────────────

def detect_regions(char: str, *, force: bool = False) -> dict:
    """キャラの幾何を確定して ``regions.json`` に書く（口 → 頭部 → 目 の順）。

    ``normal`` の口ペアから口 bbox を取り、そこから頭部矩形を組む。``eyes_closed.png``
    があれば目 bbox も入れ、頭部矩形を**目基準の精度**へ上げる（眼間距離が使えるため）。
    """
    import numpy as np
    from PIL import Image

    d = char_dir(char)
    cur = _G.load_regions(d)
    if cur.get("head_box") and not force and not (
            (d / "eyes_closed.png").exists() and cur.get("source") != "eyes"):
        return cur

    closed_p, open_p = mouth_pair_paths(char, "normal")
    if not (closed_p.exists() and open_p.exists()):
        raise FileNotFoundError(f"regions には normal の口ペアが要る: {closed_p}")
    c = Image.open(closed_p).convert("RGBA")
    o = Image.open(open_p).convert("RGBA")
    alpha = np.asarray(c.split()[3])
    eff = _G.effective_bbox(c)
    mouth = _G.mouth_bbox(_G.flat_gray(c), _G.flat_gray(o), alpha)
    head = _G.head_box_from_mouth(eff, mouth, c.size)
    out = {"canvas": list(c.size), "effective_bbox": list(eff),
           "mouth_box": list(mouth), "head_box": list(head), "source": "mouth"}

    blink_p = d / "eyes_closed.png"
    if blink_p.exists():
        b = Image.open(blink_p).convert("RGBA")
        if b.size != c.size:
            b = b.resize(c.size, Image.LANCZOS)
        eyes = _G.eye_boxes(_G.flat_gray(c), _G.flat_gray(b), alpha,
                            head_box=head, mouth_box=mouth)
        out["eye_boxes"] = [list(e) for e in eyes]
        out["head_box"] = list(_G.head_box_from_eyes(eyes, c.size))
        out["source"] = "eyes"
    _G.save_regions(d, out)
    return out


def head_box_of(char: str) -> tuple[int, int, int, int] | None:
    hb = _G.load_regions(char_dir(char)).get("head_box")
    return tuple(hb) if hb else None


def register_closed(char: str, emotion: str, target: Path):
    """生成した口閉じ画像を ``normal`` へ頭部基準で登録する（平行移動＋等方スケール）。

    合成は ``scale=-1:{height}`` で**キャンバス高さ**を揃えるので、キャンバス内で
    キャラの大きさ・位置がブレるとそのまま画面上のブレになる。``regions`` がまだ無い
    段階（normal の口ペアを作る前）はスキップし、後から ``chibi rebuild`` で直せる。
    """
    from PIL import Image

    hb = head_box_of(char)
    ref_p = mouth_pair_paths(char, "normal")[0]
    if hb is None or not ref_p.exists() or ref_p.resolve() == target.resolve():
        return None
    img = Image.open(target).convert("RGBA")
    ref = Image.open(ref_p).convert("RGBA")
    dx, dy, s, center, _score = _G.register_to_head(img, ref, hb)
    if abs(dx) < 0.5 and abs(dy) < 0.5 and abs(s - 1.0) < 0.002:
        return (dx, dy, s)
    _G.apply_similarity(img, dx, dy, s, center).save(target)
    return (dx, dy, s)


# ── 瞬き（目領域パッチ）──────────────────────────────────────

#: 驚いた時は目を見開いて瞬きが止まる。演出として自然なうえ、「見開いた目を閉じ目パッチが
#: 覆いきれない」という最も危険な破綻ケースを設計で消せる。
NO_BLINK_EMOTIONS = frozenset({"surprised"})

#: ``^_^`` は既に目が閉じているので瞬きさせても意味が無く、描き方が入れ替わってチラつく。
EYES_ALREADY_CLOSED = frozenset({"smile"})

#: 瞬きを乗せる感情。
BLINKABLE_EMOTIONS = frozenset(CHIBI_EMOTIONS) - NO_BLINK_EMOTIONS - EYES_ALREADY_CLOSED


def eyes_closed_prompt(char: str) -> str:
    """瞬き素材（目を閉じた1枚）のプロンプト。**眉と口を固定させる**のが要点。

    眉を巻き込むと angry/troubled の感情信号が壊れるので、生成側でも明示的に止める。
    """
    return (
        IDENTITY_CONSTRAINT +
        "ONLY change: both eyes are closed, drawn as simple downward-curved eyelid lines. "
        "The eyebrows stay EXACTLY as in the reference (same shape, same angle, same "
        "position). The mouth stays EXACTLY as in the reference. Everything else identical "
        "to the reference. Chibi (super-deformed) style. Plain solid white background, "
        f"NO TEXT, no watermark. (character's baseline look: {expression_of(char)})"
    )


def generate_eyes_closed(
    char: str, *, model: str = DEFAULT_CHIBI_MODEL, force: bool = False,
) -> Path:
    """瞬き用の「目を閉じた」画像を**キャラあたり1枚**生成する（課金1回）。

    眉と口はそのまま残るので、この1枚の目領域を全感情へ移植できる（感情ごとに
    目つむりを生成する必要が無い＝課金がキャラ数ぶんで済む）。
    """
    d = char_dir(char)
    target = d / "eyes_closed.png"
    if target.exists() and not force:
        raise FileExistsError(f"生成済み: {target}（リテイクは --force。1枚勝負）")
    ref = mouth_pair_paths(char, "normal")[0]
    if not ref.exists():
        raise FileNotFoundError(f"先に normal の口閉じを作る: {ref}")

    from wwedit.publish.thumbnail import generate_image, save_image

    data = generate_image(
        eyes_closed_prompt(char), model=model, aspect_ratio="1:1", image_size="1K",
        reference_images=[("image/png", ref.read_bytes())],
    )
    raw = d / "eyes_closed_raw.png"
    save_image(data, raw)
    remove_bg(raw, target)
    _match_size(target, ref)
    register_closed(char, "__eyes__", target)   # normal へ登録（目の位置を合わせる）
    meta_p = d / "gen_meta.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.exists() else {}
    meta["eyes_closed"] = {"model": model, "ref": str(ref),
                           "generated_at": datetime.now().isoformat(timespec="seconds")}
    meta_p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def build_eye_patches(char: str, emotion: str, *, force: bool = False) -> list[Path]:
    """``m{0,1}_e1.png``（目を閉じたスプライト）を作る。**無課金**。

    目つむり画像はキャラに1枚しか無いので、その**目領域だけ**をその感情の口閉じ／口開きの
    両方へ**同じ変換で**貼る。眉と口はその感情のまま残る。同じ変換なので、瞬き中に口が
    動いても目がチラつかない。
    """
    if emotion not in BLINKABLE_EMOTIONS:
        return []
    d = char_dir(char)
    blink = d / "eyes_closed.png"
    if not blink.exists():
        return []
    reg = detect_regions(char)
    eyes = reg.get("eye_boxes")
    if not eyes:
        return []
    head = tuple(reg["head_box"])
    out: list[Path] = []
    for m, base_p in enumerate(mouth_pair_paths(char, emotion)):
        if not base_p.exists():
            continue
        dst = sprite_path(char, emotion, m, eye=1)
        if dst.exists() and not force:
            out.append(dst)
            continue
        _G.compose_region_only(
            base_p, blink, dst,
            boxes=[tuple(e) for e in eyes],
            pad=(0.08, 0.15, 0.15, 0.15),   # 上だけ狭く＝眉に届かせない
            align_region=head, color_match=True,
        )
        out.append(dst)
    return out


def fix_closed_mouth(char: str, emotion: str) -> tuple[Path, float]:
    """口閉じ画像の**口領域だけ**を ``normal`` の口で差し替える（**無課金**）。

    「口は閉じたまま」がモデルに守られなかったときの救済（``check_mouth_closed`` が 1.6 を
    超えたら候補）。口以外は1画素も触らないので、表情（目と眉）とポーズはその感情のまま残る。

    差し替えたら**口開き側も新しい口閉じから作り直す**。口開きは「その感情の口閉じを参照して
    生成したもの」なので、口閉じだけ変えるとペアの整合が崩れるため。

    ⚠️ これは既定の処理ではない。まず生成で正しく描かせ、駄目だったものにだけ明示的に使う。
    """
    closed_p, open_p = mouth_pair_paths(char, emotion)
    ref_p = mouth_pair_paths(char, "normal")[0]
    if emotion == "normal":
        raise ValueError("normal 自身は差し替え元なので対象外")
    if not (closed_p.exists() and ref_p.exists()):
        raise FileNotFoundError(f"口閉じと normal が要る: {closed_p} / {ref_p}")
    reg = detect_regions(char)
    mouth = tuple(reg["mouth_box"])
    head = tuple(reg["head_box"])
    _p, frac = _G.compose_region_only(
        closed_p, ref_p, closed_p, boxes=[mouth], align_region=head, color_match=True)
    gen = open_p.with_name("mouth_open_gen.png")
    if gen.exists():
        compose_mouth_only(closed_p, gen, open_p)
    return closed_p, frac


def blink_emotions(char: str) -> set[str]:
    """``m0_e1`` と ``m1_e1`` が揃っている感情の集合（ffconcat のフォールバック判定用）。"""
    return {e for e in CHIBI_EMOTIONS
            if all(sprite_path(char, e, m, eye=1).exists() for m in range(N_MOUTH))}


# ── 生成計画 ────────────────────────────────────────────────

def plan_generation(
    chars: list[str], emotions: list[str], *,
    blink: bool = True, force: bool = False,
) -> list[tuple[str, str, str]]:
    """生成ジョブ ``(char, emotion, what)`` を列挙する（**課金なし**・存在確認のみ）。

    ``what`` ∈ {base, closed, open, eyes}。``normal`` を先頭に並べる — 口ペアが揃わないと
    ``regions``（頭部矩形）が作れず、以降の感情をアンカー登録できないため。
    """
    order = ["normal"] + [e for e in emotions if e != "normal"]
    jobs: list[tuple[str, str, str]] = []
    for c in chars:
        if force or not (char_dir(c) / "base_rgba.png").exists():
            jobs.append((c, "", "base"))
        for e in order:
            if e not in emotions:
                continue
            closed_p, open_p = mouth_pair_paths(c, e)
            if force or not closed_p.exists():
                jobs.append((c, e, "closed"))
            if force or not open_p.exists():
                jobs.append((c, e, "open"))
        if blink and (force or not (char_dir(c) / "eyes_closed.png").exists()):
            jobs.append((c, "", "eyes"))
    return jobs


def paid_jobs(
    jobs: list[tuple[str, str, str]], *, redraw_closed=REDRAW_CLOSED_CHARS,
) -> list[tuple[str, str, str]]:
    """課金が発生するジョブだけを返す（base とベース流用の normal 口閉じは無課金）。

    ``REDRAW_CLOSED_CHARS`` のキャラは normal の口閉じも描き直すので**課金対象**になる。
    """
    return [j for j in jobs
            if j[2] in ("closed", "open", "eyes")
            and not (j[2] == "closed" and j[1] == "normal" and j[0] not in redraw_closed)]
