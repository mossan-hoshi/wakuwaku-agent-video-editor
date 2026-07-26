"""[G] イントロ キャラ画像生成（決定的CLI部品）。

のべつべオリジナルキャラの**フルアート `<id>_a*.webp` を参照画像に渡し、絵柄・キャラ同一性を
維持する制約**を付けて nano banana2 で生成する。格好/シチュ等の創作（季節・服装の非重複）は
呼び出し側（intro-builder スキル＝Claudeの判断・[[intro-generation-log]] 参照）が prompt で渡す。
chibi/マスコット(`_chibi`)は参照に使わない。
"""

from __future__ import annotations

import glob
from pathlib import Path

from wwedit.common.env import env_value
from wwedit.publish.thumbnail import generate_image, save_image

# novtube の web/assets（キャラ素材の在処）。`WWEDIT_NOVTUBE_ASSETS` で差し替え可。
# 他のキー同様 os.environ → .env の順で解決する（生の os.environ だと .env 設定が効かない）。
DEFAULT_ASSETS = (env_value("WWEDIT_NOVTUBE_ASSETS")
                  or r"C:\Users\sackn\github\novtube\web\assets")

# キャラID→本名フルネーム（novtube `web/docs/mascot.md` の「本名」より）。イントロのキャラ名表示用。
# priya/kasumi は mascot.md に本名記載が無いため表示名(カタカナ)で暫定。
FULL_NAME = {
    "noa": "文月 乃亜",
    "tsukasa": "御影 司",
    "ritsu": "柊 律",
    "yume": "沢渡 ゆめ",
    "reika": "御影 怜香",
    "suzu": "御影 すず",
    "souta": "月島 颯太",
    "priya": "プリヤ",
    "kasumi": "カスミ",
}


def full_name(char: str) -> str:
    """キャラの本名フルネーム（未登録は先頭大文字のIDで代替）。"""
    return FULL_NAME.get(char, char.capitalize())


# キャラ別の**素の表情**（novtube `web/docs/mascot.md` の設定が正）。
# ⚠️ 全キャラ一律で「笑顔」にしない＝**キャラ崩れ**になる（2026-07-26 ユーザー指摘）。
# 例: ゆめは「ボソボソ声でジト目」「眠そうなピンクの目」「人見知り」＝満面の笑みは設定違反。
EXPRESSION = {
    "noa": "gentle friendly smile, relaxed and warm",
    "yume": "deadpan half-lidded sleepy eyes (jito-me), NO smile, flat unimpressed "
            "expression, slightly aloof and shy",
    "tsukasa": "calm confident expression, faint smile",
    "ritsu": "composed dignified expression, lips slightly parted as if announcing",
    "reika": "soft mature smile, calm and collected",
    "suzu": "curious bright expression, small smile",
    "souta": "easygoing neutral expression, faint friendly smile",
    "priya": "bright open smile, energetic",
    "kasumi": "gentle calm expression, soft smile",
}
_DEFAULT_EXPRESSION = "natural neutral expression"


def expression_of(char: str) -> str:
    """キャラの素の表情指示（mascot.md 準拠）。未登録は中立（勝手に笑顔にしない）。"""
    return EXPRESSION.get(char, _DEFAULT_EXPRESSION)

# 参照画像に必ず付ける同一性維持の制約（先頭固定）。
IDENTITY_CONSTRAINT = (
    "The reference image is the original character. STRICTLY maintain the EXACT same "
    "art style and character identity as the reference (same hair, same eyes, same face "
    "and proportions, same illustration style). Do NOT redesign the character. "
    "Generate a NEW portrait of the SAME character, changing ONLY the following: "
)
# リップシンク向けの構図（末尾固定）。**表情はキャラ別**（`EXPRESSION`）に差し込む。
LIPSYNC_FRAMING = (
    " Framing for lip-sync: upper body bust-up, facing camera nearly front (slight 3/4), "
    "{expression}, mouth closed, face occupies at least 40% of the frame, "
    "relatively clean background. 16:9 aspect. NO TEXT, no watermark."
)


def resolve_character_ref(char: str, assets_dir: str | Path = DEFAULT_ASSETS) -> Path:
    """キャラの **フルアート参照** `<char>_a*.webp` を返す（chibi/マスコットは除外）。"""
    assets = Path(assets_dir)
    hits = [Path(p) for p in glob.glob(str(assets / f"{char}_a*"))
            if "chibi" not in Path(p).name.lower()]
    if not hits:
        raise FileNotFoundError(f"{char} のフルアート参照(<id>_a*.webp)が無い: {assets}")
    return sorted(hits)[0]


def build_prompt(situation: str, char: str = "") -> str:
    """同一性制約＋シチュ（呼び出し側の創作）＋リップシンク構図 を結合した最終プロンプト。

    表情は ``char`` の設定（mascot.md 準拠の `EXPRESSION`）を使う＝**勝手に笑顔にしない**。
    """
    return (IDENTITY_CONSTRAINT + situation.strip()
            + LIPSYNC_FRAMING.format(expression=expression_of(char)))


def generate_character_image(
    char: str,
    situation: str,
    out_path: str | Path,
    *,
    model: str = "gemini-3-pro-image",
    assets_dir: str | Path = DEFAULT_ASSETS,
) -> Path:
    """キャラ参照＋同一性制約＋シチュで開始フレームを生成して保存する。"""
    ref = resolve_character_ref(char, assets_dir)
    data = generate_image(
        build_prompt(situation, char), model=model, aspect_ratio="16:9", image_size="2K",
        reference_images=[("image/webp", ref.read_bytes())],
    )
    return save_image(data, out_path)
