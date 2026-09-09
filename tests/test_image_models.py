"""画像生成モデルの**既定値**を縛る（課金事故の再発防止）。

2026-08-06、リポジトリ中で `gemini-3-pro-image` を「nano banana 2」と誤ラベルしていたため、
「nano2 で作って」という指示を **Nano Banana Pro** で実行してしまった。ユーザー指示により

- Nano Banana Pro (`gemini-3-pro-image`) — 高すぎる
- 旧 Nano Banana (`gemini-2.5-flash-image`)

は**一切使わない**。使ってよいのは nano banana 2 と同 lite だけ。ここでは関数・CLIオプションの
**既定値**を全部走査して、許可外のモデルIDが紛れ込んでいないことを確認する
（``--model`` で明示的に他を渡す余地は残す。事故になるのは「既定」なので）。
"""

from __future__ import annotations

import importlib
import inspect

import pytest

#: 使ってよいモデル（`models.list` の displayName で確認済み）。
ALLOWED = {
    "gemini-3.1-flash-image",        # Nano Banana 2
    "gemini-3.1-flash-lite-image",   # Nano Banana 2 Lite
}
#: 既定に現れたら失敗させるモデル。
FORBIDDEN = {
    "gemini-3-pro-image",       # Nano Banana Pro（高い）
    "gemini-2.5-flash-image",   # 旧 Nano Banana
}

MODULES = [
    "wwedit.publish.thumbnail",
    "wwedit.publish.character",
    "wwedit.publish.infographic",
    "wwedit.publish.cli",
    "wwedit.chibi.assets",
    "wwedit.chibi.cli",
]


def _model_like(v: object) -> str | None:
    """typer の Option/Argument も剥がして、モデルIDらしい文字列だけ返す。"""
    v = getattr(v, "default", v)  # typer.models.OptionInfo → その default
    if isinstance(v, str) and (v.startswith("gemini-") or v.startswith("imagen-")):
        return v
    return None


def _defaults(mod_name: str) -> list[tuple[str, str]]:
    """モジュール直下の定数と、全関数の引数既定値から、モデルIDらしきものを集める。"""
    mod = importlib.import_module(mod_name)
    found: list[tuple[str, str]] = []
    for name, obj in vars(mod).items():
        if (m := _model_like(obj)) is not None:
            found.append((f"{mod_name}.{name}", m))
        if not inspect.isfunction(obj) or obj.__module__ != mod_name:
            continue
        for pname, p in inspect.signature(obj).parameters.items():
            if p.default is inspect.Parameter.empty:
                continue
            if (m := _model_like(p.default)) is not None:
                found.append((f"{mod_name}.{name}({pname}=)", m))
    return found


@pytest.mark.parametrize("mod_name", MODULES)
def test_no_forbidden_model_in_defaults(mod_name: str):
    bad = [(w, m) for w, m in _defaults(mod_name) if m in FORBIDDEN]
    assert not bad, f"禁止モデルが既定になっている: {bad}"


@pytest.mark.parametrize("mod_name", MODULES)
def test_defaults_are_allowed_models(mod_name: str):
    bad = [(w, m) for w, m in _defaults(mod_name) if m not in ALLOWED]
    assert not bad, f"許可外のモデルが既定になっている: {bad}"


def test_scan_actually_finds_something():
    """走査が空振りしていたら上の2件は無意味に通る。最低限ヒットすることを確認する。"""
    hits = [x for m in MODULES for x in _defaults(m)]
    assert len(hits) >= 6, hits
