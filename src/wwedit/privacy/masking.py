"""PII マスキング中核：秘匿語ロード・OCRボックス照合・読めないぼかし適用。

秘匿語は **コード/リポジトリに一切埋め込まない**。`.env`(gitignore済) の
``WWEDIT_MASK_TERMS``（カンマ区切り）から実行時に読む。未設定なら空＝何もマスクしない
（安全側デグレード）。cv2/numpy は ``apply_blur`` 内で遅延import。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

__all__ = ["OcrBox", "load_mask_terms", "find_mask_regions", "apply_blur", "mask_pii"]

MASK_ENV = "WWEDIT_MASK_TERMS"
PixelBox = tuple[int, int, int, int]  # (x0, y0, x1, y1) ピクセル


@dataclass(frozen=True)
class OcrBox:
    """OCR で検出した1テキスト領域（元フレームのピクセル座標）。"""

    text: str
    box: PixelBox  # (x0, y0, x1, y1)


def _parse_env_file(path: Path, key: str) -> str | None:
    """依存を増やさず .env から1キーだけ読む簡易パーサ（os.environ 優先で補助的に使う）。"""
    if not path.exists():
        return None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            return v.strip().strip('"').strip("'")
    return None


def load_mask_terms(env_var: str = MASK_ENV, env_file: str | Path = ".env") -> list[str]:
    """マスク対象の秘匿語リストを取得。os.environ を優先し、無ければ .env から読む。

    返すのは正規化（前後空白除去・空要素除外）済みの語リスト。未設定なら空リスト。
    """
    raw = os.environ.get(env_var)
    if raw is None:
        raw = _parse_env_file(Path(env_file), env_var)
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


NAME_MAP_ENV = "WWEDIT_SUBTITLE_NAME_MAP"


def load_name_replacements(
    env_var: str = NAME_MAP_ENV, env_file: str | Path = ".env"
) -> dict[str, str]:
    """字幕の表記置換マップを取得（``原表記=置換,...`` 形式）。os.environ 優先、無ければ .env。

    個人名（漢字→カタカナ等）はコードに直書きせず .env に置く（[[pii-masking-and-ocr-engine]]）。
    未設定なら空 dict。
    """
    raw = os.environ.get(env_var)
    if raw is None:
        raw = _parse_env_file(Path(env_file), env_var)
    if not raw:
        return {}
    out: dict[str, str] = {}
    for pair in raw.split(","):
        k, _, v = pair.partition("=")
        if k.strip() and v.strip():
            out[k.strip()] = v.strip()
    return out


def apply_name_replacements(text: str, mapping: dict[str, str]) -> str:
    """テキストに表記置換を適用（長い原表記から順に置換）。"""
    for src in sorted(mapping, key=len, reverse=True):
        text = text.replace(src, mapping[src])
    return text


def _norm(s: str) -> str:
    """照合用正規化：casefold＋空白除去（OCRが空白を挟むことがあるため）。"""
    return "".join(s.split()).casefold()


def find_mask_regions(ocr_boxes: list[OcrBox], terms: list[str]) -> list[PixelBox]:
    """OCRボックスのうち、いずれかの秘匿語を含むテキストの box を返す。

    照合は正規化後の部分一致（語が OCR テキストの一部に出現すればヒット）。
    半角/全角や大文字小文字差は casefold で吸収。空白はOCR分割対策で無視。
    """
    if not terms:
        return []
    nterms = [_norm(t) for t in terms if t.strip()]
    hits: list[PixelBox] = []
    for ob in ocr_boxes:
        text = _norm(ob.text)
        if any(nt and nt in text for nt in nterms):
            hits.append(ob.box)
    return hits


def apply_blur(
    image,
    regions: list[PixelBox],
    *,
    margin: int = 4,
    block: int = 12,
    sigma: float = 8.0,
):
    """各 region を「読めない」レベルまでぼかした新しい画像(BGR ndarray)を返す。

    ピクセル化（モザイク：ブロック単位に縮小→最近傍拡大）＋ガウシアンの二段で、
    文字の高周波情報を確実に潰す。``margin`` で文字の縁まで覆う。元画像は破壊しない。
    """
    import cv2

    out = image.copy()
    h, w = out.shape[:2]
    for x0, y0, x1, y1 in regions:
        x0 = max(0, int(x0) - margin)
        y0 = max(0, int(y0) - margin)
        x1 = min(w, int(x1) + margin)
        y1 = min(h, int(y1) + margin)
        if x1 <= x0 or y1 <= y0:
            continue
        roi = out[y0:y1, x0:x1]
        rh, rw = roi.shape[:2]
        # モザイク（ブロック化）
        sw = max(1, rw // block)
        sh = max(1, rh // block)
        small = cv2.resize(roi, (sw, sh), interpolation=cv2.INTER_LINEAR)
        mosaic = cv2.resize(small, (rw, rh), interpolation=cv2.INTER_NEAREST)
        # さらにガウシアンで縁も滑らかに潰す
        k = max(3, int(sigma) | 1)  # 奇数カーネル
        out[y0:y1, x0:x1] = cv2.GaussianBlur(mosaic, (k, k), sigma)
    return out


def mask_pii(image, terms: list[str] | None = None) -> tuple:
    """画像(path or BGR ndarray)内のPII秘匿語をOCRで検出し、全該当領域をぼかす。

    各工程(OCR→照合→ぼかし)の統合シーム。compose のフレーム配列にそのまま使える。
    返り値: (マスク後画像, マスクした領域数)。``terms`` 省略時は .env から読む。
    """
    import cv2

    from wwedit.ocr import run_ocr

    img = cv2.imread(str(image)) if isinstance(image, str | Path) else image
    if terms is None:
        terms = load_mask_terms()
    regions = find_mask_regions(run_ocr(image), terms)
    return apply_blur(img, regions), len(regions)
