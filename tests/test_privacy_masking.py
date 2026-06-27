"""PII マスキング（privacy/masking.py）の単体テスト。

注意: 実際の秘匿語は使わない。すべてダミー（alice / 山田 等）でテストする。
"""

from __future__ import annotations

import numpy as np

from wwedit.privacy.masking import (
    OcrBox,
    apply_blur,
    find_mask_regions,
    load_mask_terms,
)


def test_load_from_environ(monkeypatch):
    monkeypatch.setenv("WWEDIT_MASK_TERMS", " alice , 山田 ,, bob ")
    assert load_mask_terms() == ["alice", "山田", "bob"]


def test_load_from_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("WWEDIT_MASK_TERMS", raising=False)
    f = tmp_path / ".env"
    f.write_text('WWEDIT_MASK_TERMS="alice,山田"\n# comment\nOTHER=x\n', encoding="utf-8")
    assert load_mask_terms(env_file=f) == ["alice", "山田"]


def test_load_unset_returns_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("WWEDIT_MASK_TERMS", raising=False)
    assert load_mask_terms(env_file=tmp_path / "nope.env") == []


def test_find_regions_substring_and_casefold():
    boxes = [
        OcrBox("作成者: ALICE さん", (10, 10, 100, 30)),  # 大文字→casefoldで一致
        OcrBox("山田 太郎", (10, 40, 100, 60)),  # 全角名・空白挟み
        OcrBox("無関係なテキスト", (10, 70, 100, 90)),  # 非一致
    ]
    hits = find_mask_regions(boxes, ["alice", "山田"])
    assert hits == [(10, 10, 100, 30), (10, 40, 100, 60)]


def test_find_regions_whitespace_in_ocr():
    # OCR が語中に空白を挟んでも、正規化で吸収して一致する
    boxes = [OcrBox("s a c k n", (0, 0, 50, 10))]
    assert find_mask_regions(boxes, ["sackn"]) == [(0, 0, 50, 10)]


def test_find_regions_no_terms():
    boxes = [OcrBox("alice", (0, 0, 10, 10))]
    assert find_mask_regions(boxes, []) == []


def test_apply_blur_obscures_region_only():
    rng = np.random.default_rng(0)
    img = rng.integers(0, 256, size=(80, 120, 3), dtype=np.uint8)
    img_before = img.copy()
    region = (20, 20, 80, 60)
    out = apply_blur(img, [region])

    assert out.shape == img.shape
    # 元画像は破壊されない
    assert np.array_equal(img, img_before)
    # 領域内は変化している（ぼかし適用）
    x0, y0, x1, y1 = region
    assert not np.array_equal(out[y0:y1, x0:x1], img[y0:y1, x0:x1])
    # 領域外は不変
    assert np.array_equal(out[:10, :10], img[:10, :10])
    # 「読めない」＝高周波が落ちる: 領域内の隣接画素差分の平均が大きく減る
    def hf(a):
        a = a.astype(np.int32)
        return np.abs(np.diff(a, axis=1)).mean() + np.abs(np.diff(a, axis=0)).mean()

    assert hf(out[y0:y1, x0:x1]) < hf(img[y0:y1, x0:x1]) * 0.5


def test_apply_blur_empty_regions_noop():
    img = np.zeros((10, 10, 3), dtype=np.uint8)
    out = apply_blur(img, [])
    assert np.array_equal(out, img)


def test_mask_pii_integration(monkeypatch):
    # OCRエンジンはダミー差し替え（重いモデルを呼ばない）
    from wwedit.privacy import masking as m
    from wwedit.privacy.masking import OcrBox

    fake = [OcrBox("作成 alice", (20, 20, 80, 60)), OcrBox("公開資料", (0, 0, 5, 5))]
    monkeypatch.setattr("wwedit.ocr.run_ocr", lambda _img: fake, raising=False)

    rng = np.random.default_rng(1)
    img = rng.integers(0, 256, size=(80, 120, 3), dtype=np.uint8)
    out, n = m.mask_pii(img, terms=["alice"])
    assert n == 1  # alice を含む領域のみ
    assert not np.array_equal(out[20:60, 20:80], img[20:60, 20:80])
    assert np.array_equal(out[:5, 90:], img[:5, 90:])  # 非該当領域は不変


def test_apply_name_replacements_dummy():
    from wwedit.privacy.masking import apply_name_replacements

    mp = {"フー": "Foo", "フーバー": "FooBar"}
    # 長い原表記を優先（フーバー→FooBar、単独フー→Foo）
    assert apply_name_replacements("フーバーとフー", mp) == "FooBarとFoo"
    assert apply_name_replacements("変化なし", {}) == "変化なし"
