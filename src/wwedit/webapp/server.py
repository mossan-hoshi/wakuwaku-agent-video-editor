"""フレーミングbboxアノテータのFastAPIサーバ（CVAT風・ローカル）。

dataset.json（`framing dataset`が生成）を読み、各フレームにbboxを重ねて表示。
ユーザーがドラッグで補正→保存すると dataset.json に書き戻す（corrected=true）。
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

__all__ = ["create_app", "BBoxUpdate"]


class BBoxUpdate(BaseModel):
    bbox: list[float]
    corrected: bool = True
    no_crop: bool = False  # クロップ無し（元フレーム全体＝寄せ不要）
    rejected: bool = False  # 不採用（学習データから除外。遷移/無関係/不鮮明フレーム等）


def create_app(dataset_dir: str | Path):
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles

    dataset_dir = Path(dataset_dir)
    ds_path = dataset_dir / "dataset.json"
    static_dir = Path(__file__).parent / "static"

    app = FastAPI(title="wwedit framing annotator")

    def _load() -> list[dict]:
        return json.loads(ds_path.read_text(encoding="utf-8")) if ds_path.exists() else []

    def _save(items: list[dict]) -> None:
        ds_path.write_text(
            json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @app.get("/api/dataset")
    def get_dataset() -> list[dict]:
        return _load()

    @app.post("/api/item/{item_id}")
    def update_item(item_id: str, payload: BBoxUpdate) -> dict:
        items = _load()
        for it in items:
            if it["id"] == item_id:
                it["bbox"] = [max(0.0, min(1.0, v)) for v in payload.bbox]
                it["corrected"] = payload.corrected
                it["no_crop"] = payload.no_crop
                it["rejected"] = payload.rejected
                _save(items)
                return it
        raise HTTPException(404, f"item {item_id} not found")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (static_dir / "index.html").read_text(encoding="utf-8")

    app.mount("/frames", StaticFiles(directory=dataset_dir / "frames"), name="frames")
    return app
