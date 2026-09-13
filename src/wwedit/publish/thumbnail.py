"""[L] サムネイル生成（**GPT Image 2.5 Flare** 一発生成）。

🚨 **画像生成はすべて GPT Image 2.5 Flare（Runware 経由）**。nano banana 2 / lite を含む
Gemini の画像モデルは**二度と使わない**（2026-09-13 ユーザー指示「今後二度とnano banana2で
作るな。flareに完全に切り替えろ」「全部の画像だ」）。`generate_image` は Gemini 系のモデルIDを
渡されたら例外にするので、サムネ/キャラ画/図解/ちび のどの経路からも焼けない。

キャラ/絵柄は参照画像（`style-<char>-v###.png`）で固定する。文字はモデルに描かせず、
ユーザーが後から手で載せる。旧方針の「背景だけ生成＋PILで文字を後合成
（``compose_banners``/``compose_title_logo``）」は legacy 残置（`parse_emphasis` 等のみ流用可）。
APIキーは `.env: RUNWARE_API_KEY`（`runware_image`）。
"""

from __future__ import annotations

from pathlib import Path

# 画像モデルの**単一の定義場所**。
#
# 🚨 **使ってよいのは GPT Image 2.5 Flare だけ**（2026-09-13 ユーザー指示「今後二度と
#    nano banana2で作るな。flareに完全に切り替えろ」「全部の画像だ」）。
#    以前の既定は nano banana 2 (`gemini-3.1-flash-image`) と同 lite だった。
#    Gemini の画像モデルは**定数も置かない**（置くと既定に紛れる）。既定値と拒否は
#    `tests/test_image_models.py` が縛っている。
# GPT Image 2.5 Flare は **Runware 経由**（novtube PR #2078 と同じ実測前提）。
GPT_IMAGE_25_FLARE = "gpt-image-2.5-flare"

DEFAULT_MODEL = GPT_IMAGE_25_FLARE


def generate_image(
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    aspect_ratio: str = "16:9",
    image_size: str = "2K",
    reference_images: list[tuple[str, bytes]] | None = None,
    api_key: str | None = None,
    timeout: int = 180,
    temperature: float | None = None,
    retries: int = 3,
    reference_roles: list[str] | None = None,
) -> bytes:
    """画像バイト列(PNG)を返す。**GPT Image 2.5 Flare（Runware）専用**。

    reference_images: [(mime, bytes), ...] を参照として渡す（絵柄/キャラの一貫性）。
    ``image_size`` と ``temperature`` は flare に意味が無いので受け取って捨てる
    （寸法は ``aspect_ratio`` から本番寸法を引く。quality も実測で無視される）。

    🚨 **flare 以外のモデルIDは例外にする**（nano banana 2 / lite を含む Gemini 系は使用禁止）。
    サムネ/キャラ画/図解/ちび の全経路がここを通るので、ここで止めれば漏れない。
    """
    from wwedit.publish import runware_image

    if not runware_image.is_runware_model(model):
        raise ValueError(
            f"画像モデル {model!r} は使えない。画像はすべて {GPT_IMAGE_25_FLARE} で焼く"
            "（2026-09-13 ユーザー指示: nano banana 2 など Gemini の画像モデルは二度と使わない）")
    return runware_image.generate_image(
        prompt, model=model, aspect_ratio=aspect_ratio,
        reference_images=reference_images, api_key=api_key,
        timeout=max(timeout, 300), retries=retries,
        reference_roles=reference_roles,
    )


def save_image(data: bytes, out_path: str | Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    return out_path


def generate_thumbnail(
    prompt: str,
    out_path: str | Path,
    *,
    char: str | None = "noa",
    model: str = DEFAULT_MODEL,
    assets_dir: str | Path | None = None,
    aspect_ratio: str = "16:9",
    image_size: str = "2K",
    ref_images: list[str | Path] | None = None,
) -> Path:
    """サムネを **GPT Image 2.5 Flare で一発生成**して保存する（キャラ・背景。文字は描かせない）。

    ``char`` を指定すると立ち姿 ``<id>_a*.webp`` を参照画像に渡し、絵柄・キャラ同一性を固定する
    （先頭に同一性維持の制約を付与）。``prompt`` には描画したい日本語タイトル・配色・文字サイズ
    階層・構図・表情まで含めて記述する（モデルが文字も描く）。空文字キャラなら参照なし。

    ``ref_images`` を渡すと **その画像を参照にする**（``char`` の自動解決を上書き・複数可）。
    既定の ``<id>_a*`` は LP 用に縮小した立ち姿（実測 565x1024 / 30〜40KB）で、
    寄りの構図では絵柄を再現しきれない。高解像度の ``<id>_c*`` を使いたいときはここで渡す
    （2026-09-09 に司の開始フレームが「安っぽい水彩」になった件と同じ理由）。

    ⚠️ **キャラがブレたら参照ではなくプロンプトを疑う**（2026-08-07 ユーザー指摘）。
    「悪役のような」「劇的な陰影」のような**絵柄そのものを動かす形容を盛る**と、参照を渡して
    いても寄せ切れなくなる。**変えたいのは表情と構図だけ**なので、そこだけ短く書く。
    """
    refs = None
    full = prompt
    if char or ref_images:
        from wwedit.publish.character import (
            DEFAULT_ASSETS,
            IDENTITY_CONSTRAINT,
            _mime_of,
            resolve_character_ref,
        )

        if ref_images:
            paths = [Path(x) for x in ref_images]
            for r in paths:
                if not r.is_file():
                    raise FileNotFoundError(f"参照画像が無い: {r}")
        else:
            paths = [resolve_character_ref(char, assets_dir or DEFAULT_ASSETS)]
        refs = [(_mime_of(r), r.read_bytes()) for r in paths]
        full = IDENTITY_CONSTRAINT + prompt.strip()
    data = generate_image(full, model=model, aspect_ratio=aspect_ratio,
                          image_size=image_size, reference_images=refs)
    return save_image(data, out_path)


_MEIRYO_BOLD = r"C:\Windows\Fonts\meiryob.ttc"
# (文字色, 縁色) 既定＝黄/白/水色の3行。視認性のため太い縁取り。
_DEFAULT_FILLS = [(255, 240, 80), (255, 255, 255), (120, 230, 255)]

# チャンネル傾向の既定背景プロンプト（ゆる×AI・色鮮やか・上下にテキスト帯余白・萌え娘なし）。
DEFAULT_ART_PROMPT = (
    "YouTube thumbnail illustration, 16:9, for a Japanese AI/tech study channel. "
    "Flat colorful playful cartoon/doodle style (NOT photo, NOT realistic anime girl). "
    "Scene: cutting-edge AI research — a couple of cute simple round mascot robots "
    "excitedly presenting, surrounded by floating motifs: research paper pages, a 3D "
    "wireframe object, a video frame being upscaled, neural network nodes, holographic UI. "
    "Bright high-contrast pop colors, thick clean outlines, sticker-like. Keep a CLEAR "
    "mostly-empty horizontal BAND at the TOP and BOTTOM for big text. NO TEXT, no watermark."
)
# 強調色（上帯=黄/下帯=赤）。`[語]` を強調色、その他は白で描く。
EMPH_TOP = (255, 230, 60)
EMPH_BOTTOM = (255, 80, 80)


def parse_emphasis(text: str, emph_color, base_color=(255, 255, 255)) -> list[tuple]:
    """`[語]` を強調色、その他を base_color にしたセグメント列へ。"""
    import re

    segs: list[tuple] = []
    for part in re.split(r"(\[[^\]]*\])", text):
        if not part:
            continue
        if part.startswith("[") and part.endswith("]"):
            segs.append((part[1:-1], emph_color))
        else:
            segs.append((part, base_color))
    return segs


def compose_banners(
    base_image: str | Path | bytes,
    top: str,
    bottom: str,
    out_path: str | Path,
    *,
    logo_path: str | Path | None = None,
    size: tuple[int, int] = (1280, 720),
    font_path: str = _MEIRYO_BOLD,
) -> Path:
    """チャンネル傾向の合成: 上下に半透明帯＋極太縁取りの太字（`[語]`=強調色）＋右下ロゴ。"""
    import io

    from PIL import Image, ImageDraw, ImageFont

    W, H = size
    src = io.BytesIO(base_image) if isinstance(base_image, bytes) else base_image
    img = Image.open(src).convert("RGB").resize((W, H), Image.LANCZOS)

    def band(y0, h, alpha):
        strip = Image.new("RGBA", (W, h), (0, 0, 0, alpha))
        merged = Image.alpha_composite(img.crop((0, y0, W, y0 + h)).convert("RGBA"), strip)
        img.paste(merged.convert("RGB"), (0, y0))

    def seg_w(draw, segs, font):
        return sum(draw.textlength(t, font=font) for t, _ in segs)

    def fit(draw, segs, px, max_w):
        while px > 28:
            f = ImageFont.truetype(font_path, px)
            if seg_w(draw, segs, f) <= max_w:
                return f
            px -= 4
        return ImageFont.truetype(font_path, px)

    def draw_segs(draw, segs, font, cx, y, ow):
        x = cx - seg_w(draw, segs, font) / 2
        for text, fill in segs:
            for dx in range(-ow, ow + 1):
                for dy in range(-ow, ow + 1):
                    if dx * dx + dy * dy <= ow * ow:
                        draw.text((x + dx, y + dy), text, font=font, fill=(20, 20, 30))
            draw.text((x, y), text, font=font, fill=fill)
            x += draw.textlength(text, font=font)

    draw = ImageDraw.Draw(img)
    if top:
        band(0, 150, 130)
        segs = parse_emphasis(top, EMPH_TOP)
        draw_segs(draw, segs, fit(draw, segs, 96, W - 60), W // 2, 22, ow=9)
    if bottom:
        band(H - 170, 170, 120)
        segs = parse_emphasis(bottom, EMPH_BOTTOM)
        draw_segs(draw, segs, fit(draw, segs, 88, W - 60), W // 2, H - 150, ow=9)

    if logo_path and Path(logo_path).exists():
        logo = Image.open(logo_path).convert("RGBA").resize((120, 120), Image.LANCZOS)
        img.paste(logo, (W - 120 - 24, H - 120 - 18), logo)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return out_path


def _draw_outlined(draw, xy, text, font, fill, outline, ow):
    x, y = xy
    for dx in range(-ow, ow + 1):
        for dy in range(-ow, ow + 1):
            if dx * dx + dy * dy <= ow * ow:
                draw.text((x + dx, y + dy), text, font=font, fill=outline)
    draw.text((x, y), text, font=font, fill=fill)


def compose_title_logo(
    base_image: str | Path | bytes,
    title_lines: list[str],
    out_path: str | Path,
    *,
    logo_path: str | Path | None = None,
    size: tuple[int, int] = (1280, 720),
    font_path: str = _MEIRYO_BOLD,
    base_font_px: int = 120,
    fills: list[tuple[int, int, int]] | None = None,
    outline: tuple[int, int, int] = (20, 20, 40),
    margin: tuple[int, int] = (50, 70),
) -> Path:
    """背景アートに日本語タイトル(縁取り)＋ロゴを合成して保存する（PIL・モデルの文字崩れ回避）。

    title_lines: 上から各行。行ごとに fills の色（足りなければ最後の色を流用）。
    """
    import io

    from PIL import Image, ImageDraw, ImageFont

    fills = fills or _DEFAULT_FILLS
    W, H = size
    src = io.BytesIO(base_image) if isinstance(base_image, bytes) else base_image
    img = Image.open(src).convert("RGB").resize((W, H), Image.LANCZOS)
    draw = ImageDraw.Draw(img)

    x0, y0 = margin
    y = y0
    for i, line in enumerate(title_lines):
        px = base_font_px if i < 2 else int(base_font_px * 0.5)
        font = ImageFont.truetype(font_path, px)
        fill = fills[i] if i < len(fills) else fills[-1]
        ow = max(4, px // 15)
        _draw_outlined(draw, (x0, y), line, font, fill, outline, ow)
        y += int(px * 1.18)

    if logo_path and Path(logo_path).exists():
        logo = Image.open(logo_path).convert("RGBA")
        ls = int(H * 0.21)
        logo = logo.resize((ls, ls), Image.LANCZOS)
        img.paste(logo, (40, H - ls - 30), logo)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return out_path
