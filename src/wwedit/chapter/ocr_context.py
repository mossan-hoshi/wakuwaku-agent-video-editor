"""[D]改善: 画面OCRで章タイトルの固有名を補正するための文脈生成。

STT は技術固有名（モデル名/論文名/ツール名）を誤りやすい。対策として、[E]フレーミングで
確定した **メイン領域 bbox に切り出した代表フレームを OCR** し、その画面テキストを章検出
（および概要欄/サムネ生成）の文脈に注入する。

依存: EDL.framing（static 区間に bbox 割当済み = `framing assign` 後）＋ OCRエンジン。
重いOCR/フレーム抽出は注入可能にしてテスト分離（[[cache-model-forward-not-resweep]]）。
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from wwedit.edl.schema import Edl, FramingRegion

__all__ = [
    "ScreenText",
    "crop_bgr",
    "dedup_consecutive",
    "format_digest",
    "build_screen_digest",
]


@dataclass
class ScreenText:
    time_s: float  # ソース時刻（代表フレーム）
    text: str  # その画面のOCRテキスト（1行連結）


def crop_bgr(img, bbox):
    """BGR ndarray を bbox (x,y,w,h) ピクセルで切り出す（範囲はクランプ）。bbox None は原画像。"""
    if bbox is None:
        return img
    h, w = img.shape[:2]
    x, y, bw, bh = bbox
    x0 = max(0, min(int(x), w - 1))
    y0 = max(0, min(int(y), h - 1))
    x1 = max(x0 + 1, min(int(x + bw), w))
    y1 = max(y0 + 1, min(int(y + bh), h))
    return img[y0:y1, x0:x1]


def dedup_consecutive(entries: list[ScreenText]) -> list[ScreenText]:
    """時刻順で、直前と同一テキストの連続（同じスライドが続く区間）を1つに畳む。"""
    out: list[ScreenText] = []
    for e in entries:
        if out and out[-1].text == e.text:
            continue
        out.append(e)
    return out


def format_digest(entries: list[ScreenText]) -> str:
    """章検出LLMへ注入する画面テキスト文脈ブロックを作る（時刻付き）。"""
    lines = ["# 画面テキスト(OCR) — 固有名の表記はこちらを優先（STTの聞き取り誤りを補正）"]
    for e in entries:
        m, s = divmod(int(e.time_s), 60)
        lines.append(f"{m:02d}:{s:02d}\t{e.text}")
    return "\n".join(lines)


def _ocr_text(ocr_boxes) -> str:
    """OcrBox 群（または (text,...) 群）を1行テキストへ連結。空白正規化。"""
    parts = []
    for b in ocr_boxes:
        t = getattr(b, "text", None)
        if t is None and isinstance(b, (list, tuple)):
            t = b[0]
        if t:
            parts.append(str(t).strip())
    return " ".join(p for p in parts if p)


def build_screen_digest(
    edl: Edl,
    video_path: str | Path | None = None,
    *,
    ocr_fn=None,
    extract_fn=None,
    min_chars: int = 2,
    kinds: tuple[str, ...] = ("static",),
) -> list[ScreenText]:
    """static フレーミング区間の代表フレームをメイン領域に切り出してOCRし、画面テキスト列を返す。

    ``ocr_fn(bgr_ndarray)->boxes`` / ``extract_fn(video,t,png)->bool`` を注入すればGPU/IO無しで
    テスト可。省略時は RapidOCR と ffmpeg 抽出を使う。連続同一テキストは畳む。
    """
    import cv2

    from wwedit.framing.motion import representative_time

    if ocr_fn is None:
        from wwedit.ocr.engine import run_ocr

        ocr_fn = run_ocr
    if extract_fn is None:
        from wwedit.framing.dataset import _extract_frame

        extract_fn = _extract_frame
    video = video_path or edl.source.video_path

    regions: list[FramingRegion] = [r for r in edl.framing if r.kind in kinds]
    tmp = Path(tempfile.mkdtemp())
    entries: list[ScreenText] = []
    for r in regions:
        t = representative_time(r)
        png = tmp / "rep.png"
        if not extract_fn(str(video), t, png):
            continue
        img = cv2.imread(str(png))
        if img is None:
            continue
        text = _ocr_text(ocr_fn(crop_bgr(img, r.bbox)))
        if len(text) >= min_chars:
            entries.append(ScreenText(time_s=t, text=text))
    entries.sort(key=lambda e: e.time_s)
    return dedup_consecutive(entries)
