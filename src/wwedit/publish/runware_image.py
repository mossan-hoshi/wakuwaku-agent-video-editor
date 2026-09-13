"""GPT Image 2.5 Flare（Runware 経由）で画像を生成する。

Gemini ネイティブ（`thumbnail.generate_image`）と**同じ呼び口**で使えるようにし、
`generate_image(model=GPT_IMAGE_25_FLARE, ...)` がここへ分岐する。

仕様の出どころは novtube の PR #2078（`feature/gpt-image-25-flare`）で、
そこでの **実測（2026-09-13）** をそのまま前提にしている:

- プロバイダは **Runware のみ**。AIR ID は ``openai:gpt-image@2.5-flare``。
- ``negativePrompt`` を**持たない**。送ると 400 で生成ごと落ちる。
- ``quality`` は**完全に無視される**（low〜max と未指定で cost が同一）。
  よって wwedit 側の ``image_size`` は flare では意味を持たない。
- 参照画像は **top-level ``referenceImages``**（nano banana 系の ``inputs.*`` ではない）。
  **最大 16 枚をそのまま渡せる**ので、ここでは渡された枚数ぶんを個別に送る。
  画像入力は $8/1M のトークン課金（トークン数＝32x32 patch 数）で枚数に線形なため、
  各枚は長辺 ``REFERENCE_MAX_PX`` へ落として原価だけ抑える。
- 参照と寸法の**排他は無い**。参照を渡しても width/height は目標どおり出る。

原価（実測・参考）: 枚単価 $0.0037〜0.0065 ＋ 入力 $10.73/1M tok。
**寸法を下げても安くならない**（0.66MP 未満は仕様外）ので、Gemini のような
「0.5K で下見 → 2K で本番」の2段は flare には無い。
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

#: wwedit 側の論理モデルID（CLI の ``--model`` に渡す値）。
GPT_IMAGE_25_FLARE = "gpt-image-2.5-flare"

#: 論理ID → Runware AIR ID。
_AIR_ID = {
    GPT_IMAGE_25_FLARE: "openai:gpt-image@2.5-flare",
    # AIR をそのまま渡されても通す（novtube 側の表記で書く人がいる）。
    "openai:gpt-image@2.5-flare": "openai:gpt-image@2.5-flare",
}

_ENDPOINT = "https://api.runware.ai/v1"

#: キーの出所（Gemini と同じ GCP プロジェクト）。
_SECRET_PROJECT = "cosmic-talent-450413-f9"
_SECRET_NAME = "RUNWARE_API_KEY"

#: positivePrompt の受理範囲（Runware API）。空も超過も 400 になる。
PROMPT_MAX_RUNES = 10000

#: 参照1枚あたりの長辺上限。原価は面積（patch 数）で決まるのでここで頭を打つ。
#: 1024 は「細部が保てる」実測点で、これ以下に落とすと質感の再現が甘くなる。
REFERENCE_MAX_PX = 1024

#: Runware 公式: ``referenceImages`` は最大 16 件。
MAX_REFERENCE_IMAGES = 16

#: アスペクト比 → (width, height)。flare は連続寸法だが
#: **16 の倍数・1:3 以内・0.66MP 以上**の制約があるので、その範囲で置いた本番寸法。
_DIMS = {
    "16:9": (1536, 864),
    "9:16": (864, 1536),
    "1:1": (1152, 1152),
    "4:3": (1280, 960),
    "3:4": (960, 1280),
    "3:2": (1408, 928),
    "2:3": (928, 1408),
    "21:9": (1792, 768),
}


def is_runware_model(model: str | None) -> bool:
    """``model`` が Runware 経由（flare）かどうか。"""
    return (model or "") in _AIR_ID


def dims_for(aspect_ratio: str) -> tuple[int, int]:
    """アスペクト比から flare へ送る (width, height) を返す。

    未知の比は 16:9 に落とす（黙って別の比で焼くより、既定に寄せた方が事故が小さい）。
    """
    return _DIMS.get(aspect_ratio or "16:9", _DIMS["16:9"])


def _api_key() -> str:
    """``.env`` を優先し、無ければ GCP Secret Manager から取り直す。

    `thumbnail._api_key`（GEMINI_API_KEY）と同じ2段構え。
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
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=60,
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    raise RuntimeError(
        f"{_SECRET_NAME} が .env にも Secret Manager にもありません"
        f"（gcloud secrets versions access latest --secret={_SECRET_NAME} "
        f"--project={_SECRET_PROJECT}）")


def _post(tasks: list[dict], *, api_key: str, timeout: int = 300,
          retries: int = 3) -> list[dict]:
    """Runware へタスク配列を投げ、``data`` の配列を返す。"""
    body = json.dumps(tasks).encode()
    delay = 2.0
    last: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(
            _ENDPOINT, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {api_key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode())
            break
        except urllib.error.HTTPError as e:
            # 400 系は投げ直しても同じなので即上げる（課金も発生しない）。
            detail = ""
            try:
                detail = e.read().decode()[:600]
            except Exception:  # noqa: BLE001 - 読めないなら本文なしで上げる
                pass
            if 400 <= e.code < 500:
                raise RuntimeError(f"runware {e.code}: {detail}") from e
            last = e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
        if attempt == retries - 1:
            raise RuntimeError(f"runware: 応答が取得できません（{last}）")
        time.sleep(delay)
        delay = min(delay * 2, 30.0)
    errs = payload.get("errors") or []
    if errs:
        msg = "／".join(str(x.get("message") or x) for x in errs)
        raise RuntimeError(f"runware error: {msg}")
    return payload.get("data") or []


def _upload_reference(data: bytes, *, mime: str, api_key: str,
                      timeout: int = 300) -> str:
    """参照画像を Runware へ上げ、``imageUUID`` を返す。"""
    task = {
        "taskType": "imageUpload",
        "taskUUID": str(uuid.uuid4()),
        "image": f"data:{mime};base64,{base64.standard_b64encode(data).decode()}",
    }
    for item in _post([task], api_key=api_key, timeout=timeout):
        if item.get("imageUUID"):
            return item["imageUUID"]
    raise RuntimeError("runware: imageUpload が imageUUID を返しませんでした")


def _downscale(data: bytes, max_px: int) -> bytes:
    """長辺が ``max_px`` を超える参照だけ縮めて PNG で返す（原価は面積で決まる）。"""
    import io as _io

    from PIL import Image

    im = Image.open(_io.BytesIO(data)).convert("RGB")
    if max(im.size) > max_px:
        s = max_px / max(im.size)
        im = im.resize((max(1, round(im.width * s)), max(1, round(im.height * s))),
                       Image.LANCZOS)
    buf = _io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def generate_image(
    prompt: str,
    *,
    model: str = GPT_IMAGE_25_FLARE,
    aspect_ratio: str = "16:9",
    reference_images: list[tuple[str, bytes]] | None = None,
    api_key: str | None = None,
    timeout: int = 300,
    retries: int = 3,
    width: int | None = None,
    height: int | None = None,
    reference_roles: list[str] | None = None,
) -> bytes:
    """flare で画像を生成し、バイト列（PNG）を返す。

    ``reference_images`` は ``[(mime, bytes), ...]``。**flare は参照を最大16枚そのまま
    受け取る**ので、渡された枚数ぶんを個別に送る（1枚に合成しない）。
    ``reference_roles`` を渡すと「何枚目が何の資料か」をプロンプト冒頭に書く。
    ``width``/``height`` を渡さなければ ``aspect_ratio`` から本番寸法を引く。

    ⚠️ 参照を1枚のシートへ合成する経路は**持たない**。novtube にはあるが、あれは
    1日に何千枚も焼く運用で画像入力のトークン課金（枚数に線形）を抑えるための最適化で、
    wwedit が焼くのは動画1本あたり1〜2枚なので効かない。実際に合成したら縦長の立ち絵が
    潰れて顔が30pxになり、瞳の色もホクロも落ちて別人になった（2026-09-13）。
    """
    air = _AIR_ID.get(model)
    if not air:
        raise ValueError(f"Runware のモデルではありません: {model}")
    text = (prompt or "").strip()
    if not 1 <= len(text) <= PROMPT_MAX_RUNES:
        raise ValueError(
            f"positivePrompt は 1〜{PROMPT_MAX_RUNES} 文字（now {len(text)}）")
    key = api_key or _api_key()

    refs = reference_images or []
    if len(refs) > MAX_REFERENCE_IMAGES:
        raise ValueError(
            f"参照画像は最大 {MAX_REFERENCE_IMAGES} 枚（now {len(refs)}）")
    # 参照はそれぞれ別の画像として送る（flare は最大16枚）。
    # 各枚は長辺 REFERENCE_MAX_PX へ落とす。画像入力は $8/1M tok・トークン数＝32x32 patch 数
    # なので、原価は**面積**で決まる。縮めるのは原価のためで、合成のためではない。
    ref_uuids = [
        _upload_reference(_downscale(data, REFERENCE_MAX_PX), mime="image/png",
                          api_key=key, timeout=timeout)
        for _mime, data in refs
    ]
    if len(refs) > 1:
        # どの参照が何なのかを**順番で**明示する。書かないとモデルは全部を等しく
        # 「描くべき絵」と受け取り、2枚目の部屋の雰囲気にキャラを寄せたりする。
        roles = reference_roles or []
        lines = [f"{i + 1}枚目" + (f"＝{roles[i]}" if i < len(roles) else "")
                 for i in range(len(refs))]
        text = ("[参照画像について] 参照画像を " + str(len(refs)) + " 枚、この順で渡しています: "
                + " / ".join(lines)
                + "。それぞれ別の資料なので、参照どうしを1枚の絵として合成しないこと。\n\n") + text
    if len(text) > PROMPT_MAX_RUNES:
        raise ValueError(
            f"positivePrompt が上限超過（{len(text)} > {PROMPT_MAX_RUNES}）")

    w, h = (width, height) if (width and height) else dims_for(aspect_ratio)
    task = {
        "taskType": "imageInference",
        "taskUUID": str(uuid.uuid4()),
        "model": air,
        "positivePrompt": text,
        "outputType": "URL",
        "outputFormat": "PNG",
        "numberResults": 1,
        "width": w,
        "height": h,
        # 実支払い額を応答に含めさせる（見積りとの乖離を観測するため）。
        "includeCost": True,
    }
    if ref_uuids:
        # flare は top-level。``inputs.referenceImages`` に入れると無視される。
        task["referenceImages"] = ref_uuids
    # ⚠️ negativePrompt は送らない（flare は非対応で 400 になる）。

    items = _post([task], api_key=key, timeout=timeout, retries=retries)
    for item in items:
        if item.get("taskUUID") != task["taskUUID"]:
            continue
        url = (item.get("imageURL") or item.get("imageUrl")
               or (item.get("output") or {}).get("imageURL"))
        if not url:
            continue
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    raise RuntimeError(f"runware: 応答に画像URLがありません（{items}）")


def last_cost_usd(items: list[dict]) -> float | None:
    """応答から実支払い USD を拾う（``includeCost`` を立てたときだけ入る）。"""
    for item in items:
        if item.get("cost") is not None:
            return float(item["cost"])
    return None


def read_reference(path: str | Path) -> tuple[str, bytes]:
    """ファイルを ``(mime, bytes)`` で読む（``generate_image`` の参照に渡す形）。"""
    p = Path(path)
    ext = p.suffix.lower()
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp"}.get(ext, "image/png")
    return mime, p.read_bytes()
