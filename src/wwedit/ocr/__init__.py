"""[2] 画面OCR（PP-OCRv5 / PaddleOCR）。

固有名補正（章タイトル）とPIIマスキングのため、フレーム画像からテキスト＋bboxを得る。
重いモデルは遅延ロード・キャッシュ（メモリ: cache-model-forward-not-resweep）。
"""

from wwedit.ocr.engine import run_ocr

__all__ = ["run_ocr"]
