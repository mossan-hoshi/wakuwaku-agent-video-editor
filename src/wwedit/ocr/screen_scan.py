"""画面OCRを**1回だけ**走らせて共有するキャッシュ層。

OCR（推論）は重いので、用途ごとに走らせ直さない（[[cache-model-forward-not-resweep]]）。
1本の収録につき **framing 区間の代表フレームをフル画面でOCRした結果を `screen_ocr.json` に保存**し、
以降の用途はこのキャッシュを読むだけにする。

現在の利用者:
- **章検出の固有名補正**（`chapter screen-text`）＝ 各フレームの、メイン領域 bbox 内のテキスト。
- **秘匿語/NGワードの自動モザイク**（`privacy ng-mosaic`）＝ 各フレームの、語に当たった box の座標。

フル画面でOCRしておけば、後から crop を広げても（G2の手修正）隠し漏れが出ない。bbox 内だけが
欲しい用途は ``boxes_within`` で絞れる＝**推論は1回・後処理だけ用途別**。
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from wwedit.edl.schema import Edl
from wwedit.ocr.engine import OcrBox

__all__ = [
    "CACHE_NAME",
    "DEFAULT_MAX_SPAN",
    "FrameOcr",
    "sample_times",
    "boxes_within",
    "scan_screen_ocr",
    "save_cache",
    "load_cache",
    "ensure_screen_ocr",
]

CACHE_NAME = "screen_ocr.json"
DEFAULT_MAX_SPAN = 30.0  # 長い区間は途中も拾う（画面が差し替わっていることがある）


@dataclass
class FrameOcr:
    """1フレームのOCR結果（ソース時刻＋フル画面の box 群）。"""

    time_s: float
    boxes: list[OcrBox]


def sample_times(
    edl: Edl,
    *,
    kinds: tuple[str, ...] = ("static", "pending"),
    max_span: float = DEFAULT_MAX_SPAN,
) -> list[float]:
    """OCRするソース時刻を決める（framing 区間の代表時刻＋長い区間の追加サンプル）。

    区間内は同一フレーミング＝画面が概ね安定しているので、**区間あたり1枚が基本**。
    ``max_span`` より長い区間だけ、その間隔で追加サンプルを取る。
    """
    from wwedit.framing.motion import representative_time

    times: list[float] = []
    for r in edl.framing:
        if r.kind not in kinds:
            continue
        times.append(representative_time(r))
        if max_span > 0:
            t = r.start + max_span
            while t < r.end:
                times.append(t)
                t += max_span
    return sorted(set(round(t, 3) for t in times))


def boxes_within(boxes: list[OcrBox], bbox: tuple[int, int, int, int] | None) -> list[OcrBox]:
    """bbox (x, y, w, h) の中に中心がある box だけを返す（bbox None は全部）。"""
    if bbox is None:
        return list(boxes)
    x, y, w, h = bbox
    out = []
    for b in boxes:
        x0, y0, x1, y1 = b.box
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        if x <= cx <= x + w and y <= cy <= y + h:
            out.append(b)
    return out


def scan_screen_ocr(
    edl: Edl,
    video_path: str | Path | None = None,
    *,
    times: list[float] | None = None,
    max_span: float = DEFAULT_MAX_SPAN,
    ocr_fn=None,
    extract_fn=None,
    progress_fn=None,
) -> list[FrameOcr]:
    """代表フレームをフル画面OCRして返す（**重い推論はここ1箇所だけ**）。

    ``ocr_fn(png)->boxes`` / ``extract_fn(video, t, png)->bool`` を注入すればGPU/IO無しでテスト可。
    """
    if ocr_fn is None:
        from wwedit.ocr.engine import run_ocr

        ocr_fn = run_ocr
    if extract_fn is None:
        from wwedit.framing.dataset import _extract_frame

        extract_fn = _extract_frame

    video = str(video_path or edl.source.video_path)
    ts = times if times is not None else sample_times(edl, max_span=max_span)
    tmp = Path(tempfile.mkdtemp())
    png = tmp / "scan.png"
    out: list[FrameOcr] = []
    for i, t in enumerate(ts):
        if progress_fn is not None:
            progress_fn(i, len(ts))
        if not extract_fn(video, t, png):
            continue
        out.append(FrameOcr(time_s=t, boxes=list(ocr_fn(png))))
    return out


def save_cache(path: str | Path, frames: list[FrameOcr], *, video: str = "") -> None:
    """OCR結果を JSON キャッシュへ保存する。"""
    data = {
        "video": video,
        "frames": [
            {"t": f.time_s, "boxes": [{"text": b.text, "box": list(b.box)} for b in f.boxes]}
            for f in frames
        ],
    }
    Path(path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def load_cache(path: str | Path) -> list[FrameOcr]:
    """JSON キャッシュを読む。無ければ空リスト。"""
    p = Path(path)
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    out: list[FrameOcr] = []
    for f in data.get("frames", []):
        boxes = [
            OcrBox(text=b["text"], box=tuple(b["box"]))  # type: ignore[arg-type]
            for b in f.get("boxes", [])
        ]
        out.append(FrameOcr(time_s=float(f["t"]), boxes=boxes))
    return out


def ensure_screen_ocr(
    edl: Edl,
    cache_path: str | Path,
    *,
    refresh: bool = False,
    video_path: str | Path | None = None,
    **scan_kwargs,
) -> list[FrameOcr]:
    """キャッシュがあれば読み、無ければOCRして保存する（**推論は一度きり**）。"""
    if not refresh:
        cached = load_cache(cache_path)
        if cached:
            return cached
    frames = scan_screen_ocr(edl, video_path, **scan_kwargs)
    save_cache(cache_path, frames, video=str(video_path or edl.source.video_path))
    return frames
