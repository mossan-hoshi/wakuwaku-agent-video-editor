"""[L] サムネイル生成（nano banana 2 一発生成）。

**方針（確定）**: サムネは **nano banana 2 で一発生成**する＝キャラ・背景・**日本語タイトル文字まで
モデルが一括で描く**。キャラ/絵柄は**立ち姿 `<id>_a*.webp` を参照画像**に渡して固定する
（[[thumbnail-oneshot-nano-banana]]）。nano banana 2 は日本語タイポも崩れにくいので、旧方針の
「背景だけ生成＋PILで文字を後合成（``compose_banners``/``compose_title_logo``）」は使わない
（後者は legacy 残置。`parse_emphasis` 等のみ流用可）。APIキーは `.env: GEMINI_API_KEY` のみ。

参考移植元: novtube `gemini_image.go`（`:generateContent`・`X-Goog-Api-Key`・
`responseModalities:["IMAGE"]`・`imageConfig.aspectRatio/imageSize`・base64応答）。
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

# 画像モデルの**単一の定義場所**。`models.list` の displayName で確認済み（2026-08-06）。
#
# ⚠️ 名前の対応を取り違えないこと。`gemini-3-pro-image` は **Nano Banana Pro** であって
#    nano banana 2 ではない。リポジトリ中でこれを「nano banana 2」と書いていたため、
#    「nano2 で作って」の指示を pro で実行する事故が起きた（2026-08-06）。
# ⚠️ 使ってよいのは**この2つだけ**（ユーザー指示・2026-08-06）。
#    - Nano Banana Pro (`gemini-3-pro-image`) は高すぎるので使わない
#    - 旧 Nano Banana (`gemini-2.5-flash-image`) も使わない
#    使わないモデルは定数も置かない（置くと既定に紛れ込む）。
NANO_BANANA_2 = "gemini-3.1-flash-image"             # Nano Banana 2
NANO_BANANA_2_LITE = "gemini-3.1-flash-lite-image"   # Nano Banana 2 Lite

# GPT Image 2.5 Flare（**Runware 経由**・novtube PR #2078 と同じ実測前提）。
# Gemini ではないので `generate_image` がプロバイダごと分岐する。日本語の焼き込みが
# lite より明確に良く、価格は lite 並み。`--model` にこの値を渡すと flare で焼く。
GPT_IMAGE_25_FLARE = "gpt-image-2.5-flare"

DEFAULT_MODEL = NANO_BANANA_2
_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


#: キーの出所（novtube の生成ハーネスと同じ GCP プロジェクト）。
_SECRET_PROJECT = "cosmic-talent-450413-f9"
_SECRET_NAME = "GEMINI_API_KEY"


def _api_key() -> str:
    """``.env`` を優先し、無ければ GCP Secret Manager から取り直す。

    novtube 側の ``fetch_api_key()`` と同じ2段構え（env 優先 → ``gcloud secrets versions
    access latest``）。``.env`` が未設定・期限切れでも、gcloud にログインしていれば通る。
    """
    import shutil
    import subprocess

    from wwedit.common.env import env_value

    key = env_value(_SECRET_NAME)
    if key:
        return key
    gcloud = shutil.which("gcloud") or shutil.which("gcloud.cmd")
    if gcloud:
        try:
            r = subprocess.run(
                [gcloud, "secrets", "versions", "access", "latest",
                 f"--secret={_SECRET_NAME}", f"--project={_SECRET_PROJECT}"],
                capture_output=True, text=True, timeout=60,
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    raise RuntimeError(
        f"{_SECRET_NAME} が .env にも Secret Manager にもありません"
        f"（gcloud secrets versions access latest --secret={_SECRET_NAME} "
        f"--project={_SECRET_PROJECT}）")


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
    """Gemini ネイティブ画像生成で画像バイト列(PNG)を返す。

    reference_images: [(mime, bytes), ...] をプロンプト前に参照として渡す（画風/ロゴ一貫性）。
    temperature: 未指定はモデル既定。

    ``model`` が Runware 系（GPT Image 2.5 Flare）ならそちらへ丸ごと委譲する。
    ここで分岐しておくと、サムネ/キャラ画/図解/ちび の**全経路**が同じ1行で flare を選べる。
    """
    from wwedit.publish import runware_image

    if runware_image.is_runware_model(model):
        # flare は寸法が連続で `image_size` を持たない（quality は実測で無視される）。
        # `temperature` も受け付けないので、渡されていても黙って捨てる。
        return runware_image.generate_image(
            prompt, model=model, aspect_ratio=aspect_ratio,
            reference_images=reference_images, api_key=api_key,
            timeout=max(timeout, 300), retries=retries,
            reference_roles=reference_roles,
        )
    key = api_key or _api_key()
    parts: list[dict] = []
    for mime, data in reference_images or []:
        parts.append({"inlineData": {"mimeType": mime,
                                     "data": base64.standard_b64encode(data).decode()}})
    parts.append({"text": prompt})
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {"aspectRatio": aspect_ratio, "imageSize": image_size},
        },
    }
    if temperature is not None:
        body["generationConfig"]["temperature"] = temperature
    req = urllib.request.Request(
        _ENDPOINT.format(model=model),
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Goog-Api-Key": key},
        method="POST",
    )
    # 一時的な失敗は指数バックオフで数回粘る（novtube 側と同じ流儀）。
    # 課金は成功した生成にだけ発生するので、接続エラーでバッチ全体を落とす方が損。
    payload = None
    delay = 2.0
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode())
            break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if attempt == retries - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
    if payload is None:
        raise RuntimeError("gemini image: 応答が取得できません")
    if payload.get("error"):
        raise RuntimeError(f"gemini image error: {payload['error'].get('message')}")
    said: list[str] = []
    for cand in payload.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            inline = part.get("inlineData")
            if inline and inline.get("data"):
                return base64.standard_b64decode(inline["data"])
            if part.get("text"):
                said.append(str(part["text"]).strip())
    # 画像が無いときは**理由を捨てない**。安全フィルタで落ちたのか、モデルが
    # 「描けない」と文章で返したのか、単なる打ち切りかで打ち手が全く違う。
    why = [f"finishReason={c.get('finishReason')}"
           for c in payload.get("candidates", []) if c.get("finishReason")]
    fb = payload.get("promptFeedback") or {}
    if fb.get("blockReason"):
        why.append(f"blockReason={fb['blockReason']}")
    for r in (fb.get("safetyRatings") or []):
        if r.get("blocked") or r.get("probability") not in (None, "NEGLIGIBLE", "LOW"):
            why.append(f"{r.get('category')}={r.get('probability')}")
    detail = "／".join(why) or "理由の記載なし"
    text = ("／モデルの返答: " + " ".join(said)[:300]) if said else ""
    raise RuntimeError(f"gemini image: 応答に画像がありません（{detail}{text}）")


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
    model: str = NANO_BANANA_2,
    assets_dir: str | Path | None = None,
    aspect_ratio: str = "16:9",
    image_size: str = "2K",
    ref_images: list[str | Path] | None = None,
) -> Path:
    """サムネを **nano banana 2 で一発生成**して保存する（文字・キャラ・背景を一括描画）。

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
