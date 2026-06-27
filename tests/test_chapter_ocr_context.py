"""chapter.ocr_context（画面OCR文脈生成）のテスト。OCR/抽出は注入してI/O分離。"""

from __future__ import annotations

import numpy as np

from wwedit.chapter.ocr_context import (
    ScreenText,
    build_screen_digest,
    crop_bgr,
    dedup_consecutive,
    format_digest,
)
from wwedit.edl.schema import Edl, FramingRegion, SourceMedia


def _edl_with_framing(regions):
    return Edl(
        recording_dir="2026-06-04",
        source=SourceMedia(video_path="dummy.mp4", duration_s=100.0),
        framing=regions,
    )


def test_crop_bgr_clamps_and_crops():
    img = np.zeros((100, 200, 3), dtype="uint8")
    img[20:60, 30:90] = 255
    c = crop_bgr(img, (30, 20, 60, 40))  # x,y,w,h
    assert c.shape == (40, 60, 3)
    assert (c == 255).all()
    # None → 原画像
    assert crop_bgr(img, None).shape == img.shape
    # はみ出しはクランプ
    assert crop_bgr(img, (190, 90, 100, 100)).shape[0] >= 1


def test_dedup_consecutive_collapses_same_text():
    e = [
        ScreenText(0.0, "Transformer"),
        ScreenText(1.0, "Transformer"),
        ScreenText(2.0, "RAG"),
        ScreenText(3.0, "Transformer"),
    ]
    out = dedup_consecutive(e)
    assert [x.text for x in out] == ["Transformer", "RAG", "Transformer"]


def test_format_digest_has_header_and_times():
    block = format_digest([ScreenText(75.0, "FlashVSR")])
    assert "画面テキスト(OCR)" in block
    assert "01:15\tFlashVSR" in block


def test_build_screen_digest_with_injected_fns():
    regions = [
        FramingRegion(start=0.0, end=10.0, kind="static", bbox=(0, 0, 10, 10)),
        FramingRegion(start=10.0, end=20.0, kind="static", bbox=(0, 0, 10, 10)),
        FramingRegion(start=20.0, end=30.0, kind="pending"),  # static以外は無視
    ]
    edl = _edl_with_framing(regions)
    texts = {5.0: "SAM3D", 15.0: "SAM3D"}  # 代表時刻=中点。同一テキスト→畳む

    def fake_extract(_video, t, png):
        # 代表時刻を OCR 結果に紐付けるため、time をファイルに残さず辞書参照で代用
        fake_extract.last_t = float(t)
        return True

    def fake_ocr(_img):
        return [type("B", (), {"text": texts[round(fake_extract.last_t, 3)]})()]

    # cv2.imread をパッチ（ダミー画像を返す）
    import wwedit.chapter.ocr_context as mod

    real_cv2 = __import__("cv2")
    orig = real_cv2.imread
    real_cv2.imread = lambda _p: np.zeros((20, 20, 3), dtype="uint8")
    try:
        digest = build_screen_digest(edl, ocr_fn=fake_ocr, extract_fn=fake_extract)
    finally:
        real_cv2.imread = orig
    assert [d.text for d in digest] == ["SAM3D"]  # 2区間同一→1件
    assert digest[0].time_s == 5.0
    _ = mod  # モジュール参照（lint用）
