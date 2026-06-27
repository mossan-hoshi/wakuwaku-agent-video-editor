"""画面OCRエンジン（RapidOCR-ONNX）。フレーム画像 → テキスト＋軸並行bbox（ピクセル）。

プラン[2]は PP-OCRv5(PaddleOCR) 本命だが、当環境では paddlepaddle が torch と Windows
DLL 衝突（shm.dll WinError127）で import 不可。プラン明記の代替 **RapidOCR(同系ONNX)** を
採用＝onnxruntime駆動で torch 非依存・衝突なし、日本語(漢字/かな)も実測で高信頼に読めた。
出力は ``privacy.masking.OcrBox`` のリスト（PIIマスキングへそのまま渡せる）。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from wwedit.privacy.masking import OcrBox

__all__ = ["run_ocr"]


@lru_cache(maxsize=1)
def _engine():
    from rapidocr_onnxruntime import RapidOCR

    return RapidOCR()


def _aabb(quad) -> tuple[int, int, int, int]:
    """4点ポリゴン [[x,y],...] を軸並行bbox (x0,y0,x1,y1) へ。"""
    xs = [float(p[0]) for p in quad]
    ys = [float(p[1]) for p in quad]
    return (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))


def run_ocr(image, *, min_conf: float = 0.5) -> list[OcrBox]:
    """画像をOCRし OcrBox のリストを返す。``min_conf`` 未満の領域は捨てる。

    ``image`` はパス(str|Path)または BGR ndarray（compose のフレーム配列を直接渡せる）。
    lang 引数は無し（RapidOCRの既定モデルが日本語含む多言語を扱う）。
    """
    src = str(image) if isinstance(image, str | Path) else image
    result, _elapse = _engine()(src)
    out: list[OcrBox] = []
    for quad, text, score in result or []:
        if float(score) >= min_conf:
            out.append(OcrBox(text=text, box=_aabb(quad)))
    return out
