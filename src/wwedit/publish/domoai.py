"""[G] DomoAI talking-avatar（リップシンク）クライアント。

API: `https://api.domoai.com/v1/`・`Authorization: Bearer <DOMOAI_API_KEY>`（.env のみ）。
- 生成: `POST /v1/video/talking-avatar`（image/video のどちらか＋audio＋seconds 必須）。
- 入力: ≤10MB は `bytes_base64_encoded` 直送、>10MB は `POST /v1/upload/file`→presigned PUT→uri。
- 取得: `GET /v1/tasks/{id}` をポーリング（SUCCESS で `output_videos[].url`、URLは8h失効）。

⚠️ **高コスト**（$0.06/秒＝30秒で約100円超）。検証は必ず `seconds` を小さく（1秒）。
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.request
from pathlib import Path

BASE = "https://api.domoai.com/v1"
# Cloudflare(error 1010)が urllib 既定UAを弾くため、ブラウザ風UAを付ける。
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_B64_MAX = 10 * 1024 * 1024  # これ超は upload 経由
_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".webp": "image/webp", ".mp3": "audio/mpeg", ".wav": "audio/wav",
         ".m4a": "audio/mp4", ".aac": "audio/aac"}


def _key() -> str:
    k = os.environ.get("DOMOAI_API_KEY")
    if not k:
        raise RuntimeError("DOMOAI_API_KEY が .env にありません")
    return k


def _call(req, timeout):
    import urllib.error

    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:1000]
        raise RuntimeError(f"domoai HTTP {e.code} {req.full_url}: {body}") from e


def _post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}", data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {_key()}", "Content-Type": "application/json",
                 "User-Agent": _UA, "Accept": "application/json"},
        method="POST",
    )
    return _call(req, 120)


def _get(path: str) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        headers={"Authorization": f"Bearer {_key()}", "User-Agent": _UA,
                 "Accept": "application/json"},
        method="GET")
    return _call(req, 60)


def upload_file(path: str | Path) -> str:
    """ファイルを presigned 経由でアップロードし domoai_uri を返す（>10MB 用）。"""
    path = Path(path)
    resp = _post("/upload/file", {"filename": path.name})
    data = resp.get("data", resp)
    presigned, headers, uri = data["presigned_url"], data["headers"], data["domoai_uri"]
    put = urllib.request.Request(presigned, data=path.read_bytes(), headers=headers, method="PUT")
    with urllib.request.urlopen(put, timeout=300):
        pass
    return uri


def _file_input(path: str | Path) -> dict:
    """≤10MB は base64 直送、>10MB は upload→domoai_uri。"""
    path = Path(path)
    if path.stat().st_size <= _B64_MAX:
        return {"bytes_base64_encoded": base64.standard_b64encode(path.read_bytes()).decode()}
    return {"domoai_uri": upload_file(path)}


def create_talking_avatar(
    *,
    image: str | Path | None = None,
    audio: str | Path,
    seconds: int,
    aspect_ratio: str = "16:9",
    prompt: str = "",
    model: str = "talking-avatar-v1",
) -> str:
    """talking-avatar タスクを作成し task_id を返す。seconds は 1–60（コスト=秒×$0.06）。"""
    if not 1 <= seconds <= 60:
        raise ValueError("seconds は 1–60")
    if image is None:
        raise ValueError("image が必要（video 入力は未対応）")
    body = {
        "image": _file_input(image),
        "audio": _file_input(audio),
        "seconds": seconds,
        "aspect_ratio": aspect_ratio,
        "model": model,
    }
    if prompt:
        body["prompt"] = prompt[:2000]
    resp = _post("/video/talking-avatar", body)
    if resp.get("code", 0) != 0:
        raise RuntimeError(f"domoai create error: {resp}")
    return resp["data"]["task_id"]


def get_task(task_id: str) -> dict:
    """タスク状態を取得（status / output_videos 等）。"""
    resp = _get(f"/tasks/{task_id}")
    return resp.get("data", resp)


def wait_for_task(task_id: str, *, interval: float = 5.0, timeout: float = 900.0) -> dict:
    """SUCCESS まで polling。失敗/タイムアウトで例外。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = get_task(task_id)
        status = (t.get("status") or "").upper()
        if status == "SUCCESS":
            return t
        if status in ("FAILED", "ERROR", "CANCELLED"):
            raise RuntimeError(f"domoai task {status}: {t}")
        time.sleep(interval)
    raise TimeoutError(f"domoai task timeout: {task_id}")


def download_video(task: dict, out_path: str | Path) -> Path:
    """SUCCESS タスクの output_videos[0].url を保存（URLは8h失効なので即DL）。"""
    vids = task.get("output_videos") or []
    if not vids:
        raise RuntimeError(f"output_videos が空: {task}")
    url = vids[0]["url"]
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    dreq = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(dreq, timeout=300) as r:
        out_path.write_bytes(r.read())
    return out_path


def generate_talking_avatar(
    image: str | Path,
    audio: str | Path,
    out_path: str | Path,
    *,
    seconds: int,
    aspect_ratio: str = "16:9",
    prompt: str = "",
) -> Path:
    """画像＋音声→リップシンク動画を生成して保存（作成→polling→DL）。"""
    task_id = create_talking_avatar(
        image=image, audio=audio, seconds=seconds, aspect_ratio=aspect_ratio, prompt=prompt)
    task = wait_for_task(task_id)
    return download_video(task, out_path)
