"""[G] イントロ キャラ画像生成（決定的CLI部品）。

のべつべオリジナルキャラの**フルアート `<id>_a*.webp` を参照画像に渡し、絵柄・キャラ同一性を
維持する制約**を付けて GPT Image 2.5 Flare で生成する。格好/シチュ等の創作（季節・服装の非重複）は
呼び出し側（intro-builder スキル＝Claudeの判断・[[intro-generation-log]] 参照）が prompt で渡す。
chibi/マスコット(`_chibi`)は参照に使わない。
"""

from __future__ import annotations

import glob
from pathlib import Path

from wwedit.common.env import env_value
from wwedit.publish.thumbnail import GPT_IMAGE_25_FLARE, generate_image, save_image

# novtube の web/assets（キャラ素材の在処）。`WWEDIT_NOVTUBE_ASSETS` で差し替え可。
# 他のキー同様 os.environ → .env の順で解決する（生の os.environ だと .env 設定が効かない）。
DEFAULT_ASSETS = (env_value("WWEDIT_NOVTUBE_ASSETS")
                  or r"C:\Users\sackn\github\novtube\web\assets")

# 🚨 **絵柄参照の正はこちら**（2026-09-13 ユーザー指示）。
# novtube の character_expansion が出す `style-<char>-v###.png` が**最新のキャラプロフィール**で、
# 立ち絵の解像度・線・塗りがそろっている。旧 `web/assets/<id>_a*.webp` は LP 用の縮小版
# （565x1024・顔が縦120px程度）で、**これを参照にすると絵柄が再現されない**
# （2026-09-13: 霞の開始フレームが別画風になった）。`WWEDIT_CHAR_STYLE_REFS` で差し替え可。
DEFAULT_STYLE_REFS = (env_value("WWEDIT_CHAR_STYLE_REFS")
                      or r"C:\Users\sackn\repos2\novtube3\output\character_expansion"
                         r"\20260911\images")


def resolve_style_ref(char: str,
                      style_dir: str | Path = DEFAULT_STYLE_REFS) -> Path | None:
    """`style-<char>-v###.png` の**最新版**を返す。無ければ None。

    版は `v001` < `v002` … の辞書順で最後を採る（実際に v003 まである）。
    """
    hits = sorted(Path(style_dir).glob(f"style-{char}-v*.png"))
    return hits[-1] if hits else None

# キャラID→本名フルネーム（novtube `web/docs/mascot.md` の「本名」より）。イントロのキャラ名表示用。
# 全9名とも mascot.md §2 に本名の記載がある（2026-09-13 に priya/kasumi を実物に合わせた）。
FULL_NAME = {
    "noa": "文月 乃亜",
    "tsukasa": "御影 司",
    "ritsu": "柊 律",
    "yume": "沢渡 ゆめ",
    "reika": "御影 怜香",
    "suzu": "御影 すず",
    "souta": "月島 颯太",
    # mascot.md に本名が載った2名（以前は「記載なし」としてカタカナの表示名で暫定していた）。
    "priya": "プリヤ・シャルマ",
    "kasumi": "久遠 霞",
    # 2026-09 追加の新キャラ（novtube3 `web/docs/mascot.md` の Nono 節）。
    "nono": "遠野 のの",
}


def full_name(char: str) -> str:
    """キャラの本名フルネーム（未登録は先頭大文字のIDで代替）。"""
    return FULL_NAME.get(char, char.capitalize())


# 🚨 **声を借りているキャラは概要欄にクレジットを出す**（2026-08-08 ユーザー指摘）。
# 参照音声が CC-BY 等のライセンス素材なら、表示は**義務**であって任意ではない。
# ``(ラベル, URL)`` で持ち、概要欄の links ブロック（ラベル→URL）へそのまま流す。
# 自前データセット由来のキャラ（noa 等）はここに載せない＝出さないのが正しい。
VOICE_CREDIT: dict[str, tuple[str, str]] = {
    "souta": (
        "月島颯太 (松風音声読み上げデータ / 松風 / CC-BY-4.0)",
        "https://twitter.com/mochi_jin_voice",
    ),
}


def voice_credits(chars) -> list[tuple[str, str]]:
    """その動画に**声が入っている**キャラのクレジットを、重複なく登場順で返す。

    未登録キャラは黙って飛ばす（自前素材＝表示不要）。新しく外部素材から声を作ったら
    `VOICE_CREDIT` に足す——足し忘れるとライセンス違反になる。
    """
    out: list[tuple[str, str]] = []
    for c in chars or ():
        credit = VOICE_CREDIT.get((c or "").strip())
        if credit and credit not in out:
            out.append(credit)
    return out


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
    # mascot.md: 明るいが騒がしくない。歯を見せて大口で笑わせない。
    "nono": "bright natural open-eyed smile, gently cheerful and calm, teeth not showing",
}
_DEFAULT_EXPRESSION = "natural neutral expression"


def expression_of(char: str) -> str:
    """キャラの素の表情指示（mascot.md 準拠）。未登録は中立（勝手に笑顔にしない）。"""
    return EXPRESSION.get(char, _DEFAULT_EXPRESSION)

# 開始フレームの解像度。**当たりは 0.5K で取り、通ったものだけ 2K で焼く**
# （2026-09-09 ユーザー指示「0.5kであたりをつけてから2kで作れや」）。
#
# 🚨 **既定を FINAL_SIZE にしない。** 以前は `image_size="2K"` がベタ書きで、CLI から
# 下げる口も無かったため、構図の当たりを取る1枚目まで最高解像度で焼いていた。
# 開始フレームの行き先は DomoAI リップシンク＝**1280x720** なので、
# 2K(2752x1536) は最終版でも過剰ぎみ。下見に至っては完全な無駄。
#
# 🚨 **公式ドキュメントの表記とエンドポイントの受理値が食い違う。**
# ドキュメント（https://ai.google.dev/gemini-api/docs/image-generation）は
# 「0.5K / 1K / 2K / 4K」と書いているが、実際に投げると 400 で拒否され、
# エラー本文が正解を返す（2026-09-09 実測）:
#
#     Unsupported image_size '0.5K'.
#     Supported values are: 1K, 2K, 4K, 512, 512P, 512PX.
#
# よって下見は **`512`**。ドキュメントを信じて `0.5K` に戻さないこと。
# `gemini-3.1-flash-lite-image` は 1K のみ対応（こちらはドキュメントどおり）。
# ⚠️ 2026-09-13 以降、画像はすべて GPT Image 2.5 Flare。flare は `image_size` を受け取って捨てる
#    （1回で本番寸法 1536x864）ので、下見→2K の2段は無い。値は互換のため残している。
DRAFT_SIZE = "512"
FINAL_SIZE = "2K"

# 参照画像に必ず付ける同一性維持の制約（先頭固定）。
IDENTITY_CONSTRAINT = (
    "The reference image is the original character. STRICTLY maintain the EXACT same "
    "art style and character identity as the reference (same hair, same eyes, same face "
    "and proportions, same illustration style). Do NOT redesign the character. "
    "Generate a NEW portrait of the SAME character, changing ONLY the following: "
)
# 🚨 **構図はコードで決めない。** 呼び出し側が毎回**自由文**で渡す。
#
# 2026-08-07 ユーザー指摘＝「正面バストアップ構図、もう飽きた。構図・シチュエーション共に
# 変化持たせろ」「リスト明示するな。直近10動画の構図をメモっておいて被らないように自由に
# 選べ」。以前はここに「上半身バストアップ・正面寄り3/4・顔40%以上」を**直書き**していた
# ので、situation に何を書いても打ち消されて毎回同じ絵になっていた。選択肢を enum で並べても
# 同じこと（軸が固定される）なので**持たない**。
#
# 過去に使った構図は `intro-generation-log` に1行ずつ記録し、**直近10本と被らないものを
# 自分で組み立てる**（ショットサイズ・カメラ高さ・体の向き・姿勢・画面内の位置・前景など、
# どの軸を動かしてもよい）。
#
# ここが持つのは**リップシンクが破綻しない下限**だけ。
LIPSYNC_SAFETY = (
    " The mouth must be fully visible and unobstructed, lips closed, "
    "face not cropped and not turned away past a 3/4 view, {expression}. "
    "16:9 aspect. NO TEXT, no watermark."
)


def resolve_character_ref(char: str, assets_dir: str | Path = DEFAULT_ASSETS,
                          style_dir: str | Path = DEFAULT_STYLE_REFS) -> Path:
    """キャラの**絵柄参照**を返す。

    **`style-<char>-v###.png`（character_expansion の最新プロフィール）を最優先**にする。
    無いキャラだけ旧 `web/assets/<char>_a*.webp` に落ちる。

    ⚠️ 旧 `<char>_a*` は **LP 用に縮小された立ち姿**（実測 565x1024 / 30〜40KB）で、
    顔は縦120px程度しかない。**これを参照にすると絵柄が再現されない**
    （2026-09-09 司が「安っぽい水彩」／2026-09-13 霞が別画風）。
    `--ref-image` / `ref_images=` で明示的に渡せば上書きできる
    （候補は `available_character_refs` で一覧）。
    """
    if (style := resolve_style_ref(char, style_dir)) is not None:
        return style
    assets = Path(assets_dir)
    hits = [Path(p) for p in glob.glob(str(assets / f"{char}_a*"))
            if "chibi" not in Path(p).name.lower()]
    if not hits:
        raise FileNotFoundError(
            f"{char} の絵柄参照が無い: {style_dir} の style-{char}-v*.png も "
            f"{assets} の {char}_a*.webp も見つからない")
    return sorted(hits)[0]


def available_character_refs(char: str,
                             assets_dir: str | Path = DEFAULT_ASSETS) -> list[Path]:
    """そのキャラの参照候補を**サイズの大きい順**に返す（chibi は除外）。

    `style-<char>-v###.png`（最新プロフィール）と、旧 `<char>_a*`（立ち姿の縮小版）・
    `<char>_c*`（高解像度のバストアップ等）をまとめて拾う。
    どれを使うかは呼び出し側の判断 —— **既定の解決は `resolve_character_ref` が持つ**。
    """
    assets = Path(assets_dir)
    hits = [Path(p) for p in glob.glob(str(assets / f"{char}_*"))
            if "chibi" not in Path(p).name.lower() and Path(p).is_file()]
    hits += list(Path(DEFAULT_STYLE_REFS).glob(f"style-{char}-v*.png"))
    return sorted(hits, key=lambda p: (-p.stat().st_size, p.name))


def build_prompt(situation: str, char: str = "", framing: str = "",
                 expression: str = "") -> str:
    """同一性制約＋シチュ＋構図（どちらも呼び出し側の創作）＋破綻回避の下限。

    表情は ``char`` の設定（mascot.md 準拠の `EXPRESSION`）を使う＝**勝手に笑顔にしない**。
    ``expression`` を渡すとその回だけ上書きする（``EXPRESSION`` は書き換えない）。
    キャラの素の表情は残したまま、**その絵だけ笑わせたくない**ときに使う
    （2026-09-09「笑顔止めろ」＝司の既定 `faint smile` を消したいがキャラ設定は変えない）。

    ``framing`` は**自由文**（例: "knee-up, camera low near the ground looking up,
    body nearly in profile with the face turned back, subject on the right third"）。
    空なら構図の指定なし＝モデルに委ねる。**既定の構図は持たない**（持つと毎回同じになる）。
    """
    parts = [IDENTITY_CONSTRAINT + situation.strip()]
    if framing.strip():
        parts.append(" Framing: " + framing.strip().rstrip(".") + ".")
    parts.append(LIPSYNC_SAFETY.format(
        expression=expression.strip() or expression_of(char)))
    return "".join(parts)


def generate_character_image(
    char: str,
    situation: str,
    out_path: str | Path,
    *,
    model: str = GPT_IMAGE_25_FLARE,
    assets_dir: str | Path = DEFAULT_ASSETS,
    framing: str = "",
    ref_images: list[str | Path] | None = None,
    image_size: str = DRAFT_SIZE,
    expression: str = "",
) -> Path:
    """キャラ参照＋同一性制約＋シチュで開始フレームを生成して保存する。

    ``ref_images`` を渡すと**その画像だけ**を参照にする（複数可・渡した順で送る）。
    省略時は従来どおり `resolve_character_ref()`＝`<char>_a*` の1枚。
    高解像度の `<char>_c*` を使いたいときはここで明示する（`resolve_character_ref` の注意参照）。

    ``image_size`` の既定は **`DRAFT_SIZE`＝下見用の 0.5K**（`FINAL_SIZE` が本番の 2K）。
    🚨 **いきなり 2K で焼かない。** 構図・服装・背景の当たりは 0.5K で取り、
    ユーザーが良しとしたものだけ同じプロンプトで 2K を焼く（2026-09-09 ユーザー指示）。
    """
    if ref_images:
        refs = [Path(x) for x in ref_images]
        for r in refs:
            if not r.is_file():
                raise FileNotFoundError(f"参照画像が無い: {r}")
    else:
        refs = [resolve_character_ref(char, assets_dir)]
    # 参照が2枚以上のとき、**1枚目＝キャラ／2枚目以降＝場面の資料**だと明示する。
    # 言わないとモデルは全部を等しく「描くべき絵」と受け取り、背景資料の人物や画風に
    # 引きずられる（2026-09-13: 部屋の資料を足したら瞳の色とホクロが落ちた）。
    roles = (["キャラクターの見本。**この人物・この絵柄をそのまま保つ**"]
             + ["場面（部屋・背景）の資料。家具と内装だけを写す"] * (len(refs) - 1)
             ) if len(refs) > 1 else None
    data = generate_image(
        build_prompt(situation, char, framing, expression), model=model,
        aspect_ratio="16:9", image_size=image_size,
        reference_images=[(_mime_of(r), r.read_bytes()) for r in refs],
        reference_roles=roles,
    )
    return save_image(data, out_path)


def _mime_of(p: Path) -> str:
    """拡張子から画像 MIME を決める（参照は webp とは限らない）。"""
    ext = p.suffix.lower()
    return {".webp": "image/webp", ".png": "image/png",
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}.get(ext, "image/webp")
