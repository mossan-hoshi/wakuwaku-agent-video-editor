"""[G] AIVis(=Style-Bert-VITS2) 音声合成クライアント（決定的CLI部品）。

novtube の SBV2 合成サーバ（`tool/dub_local/server.py`・:8123）へ `/synth` を叩くだけの
**決定的処理**。文章の中身・尺調整・声選びは呼び出し側（intro-builder スキル＝Claudeの判断）。

前提: サーバ起動済み（SBV2 venv の python で起動）。起動法・styleは [[external-assets-and-keys]]。
"""

from __future__ import annotations

import base64
import json
import subprocess
import urllib.request
from pathlib import Path

DEFAULT_SYNTH_URL = "http://127.0.0.1:8123"
# AIVIS キャラの中立(normal)スタイル。docs/tts_dub.yaml の aivis: emotion_lut['normal'] に対応。
DEFAULT_STYLE = {"noa": "normal"}


def synth(
    text: str,
    voice: str = "noa",
    *,
    style: str | None = None,
    synth_url: str = DEFAULT_SYNTH_URL,
    engine: str = "AIVIS",
    timeout: int = 300,
) -> tuple[bytes, float]:
    """テキストを合成し (wav_bytes, duration_ms) を返す。サーバ未起動なら明確に失敗。"""
    style = style or DEFAULT_STYLE.get(voice, "normal")
    body = json.dumps({
        "engine": engine, "voice_model_id": voice, "voice_style_id": style, "texts": [text],
    }).encode()
    req = urllib.request.Request(
        f"{synth_url}/synth", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.load(r)
    except OSError as e:
        raise RuntimeError(
            f"AIVis /synth 失敗（SBV2サーバ未起動?）: {e}"
            "  github の SBV2 venv python で tool/dub_local/server.py を起動する"
        ) from e
    chunk = (resp.get("chunks") or [{}])[0]
    if not chunk.get("wav_base64"):
        raise RuntimeError(f"AIVis 応答に音声なし: {resp}")
    return base64.b64decode(chunk["wav_base64"]), float(chunk.get("duration_ms") or 0.0)


def synth_to_file(
    text: str, out_path: str | Path, voice: str = "noa", **kw
) -> float:
    """合成→44100/mono wav 保存。実尺(秒)を返す（ffprobe実測）。"""
    wav, _dur_ms = synth(text, voice, **kw)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".raw.wav")
    tmp.write_bytes(wav)
    try:
        subprocess.run(["ffmpeg", "-y", "-i", str(tmp), "-ar", "44100", "-ac", "1",
                        str(out_path)], check=True, capture_output=True)
    finally:
        tmp.unlink(missing_ok=True)
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
         "default=nw=1:nk=1", str(out_path)], capture_output=True, text=True)
    return float(out.stdout.strip() or 0.0)
