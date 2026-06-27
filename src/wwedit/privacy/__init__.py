"""個人情報(PII)マスキング。

画面内OCRで検出したテキストのうち、秘匿語（本名・ハンドル等＝`.env` の
``WWEDIT_MASK_TERMS`` に列挙、リポジトリには含めない）を含む領域を、
読めないレベルのぼかしで隠す。フレーミングのメイン領域OCR結果に対して適用する。
"""

from wwedit.privacy.masking import (
    OcrBox,
    apply_blur,
    find_mask_regions,
    load_mask_terms,
    mask_pii,
)

__all__ = ["OcrBox", "apply_blur", "find_mask_regions", "load_mask_terms", "mask_pii"]
