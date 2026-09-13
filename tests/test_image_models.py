"""画像生成モデルを縛る（課金事故とユーザー指示違反の再発防止）。

🚨 **画像はすべて GPT Image 2.5 Flare**（2026-09-13 ユーザー指示「今後二度とnano banana2で作るな。
flareに完全に切り替えろ」「全部の画像だ」）。nano banana 2 / lite を含む Gemini の画像モデルは
**既定にも置かないし、明示されても焼かない**。

経緯:
- 2026-08-06、`gemini-3-pro-image`（Nano Banana **Pro**）を「nano banana 2」と誤ラベルしていて、
  「nano2 で作って」を Pro で実行した。以後 Pro と旧 Nano Banana を禁止した。
- 2026-09-13、サムネとイントロだけ flare に切り替えたが、図解が既定の nano banana 2 のまま焼かれた。
  ユーザー指示で**全部の画像**を flare にし、Gemini の画像モデルは入口で拒否することにした。

ここでは (1) 関数・CLIオプション・定数の**既定値**を全部走査して flare 以外が無いこと、
(2) `thumbnail.generate_image` が flare 以外のモデルIDを**例外にする**ことを確認する。
"""

from __future__ import annotations

import importlib
import inspect

import pytest

#: 使ってよいモデルはこれだけ。
ALLOWED = {
    "gpt-image-2.5-flare",           # GPT Image 2.5 Flare（Runware 経由・novtube PR #2078）
}
#: 既定に現れたら失敗させる／明示されても拒否するモデル。
FORBIDDEN = {
    "gemini-3.1-flash-image",        # Nano Banana 2（2026-09-13 から禁止）
    "gemini-3.1-flash-lite-image",   # Nano Banana 2 Lite（同上）
    "gemini-3-pro-image",            # Nano Banana Pro（高い）
    "gemini-2.5-flash-image",        # 旧 Nano Banana
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
    if isinstance(v, str) and (v.startswith("gemini-") or v.startswith("imagen-")
                               or v.startswith("gpt-image-")
                               or v.startswith("openai:gpt-image")):
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


def test_no_gemini_image_constants_left():
    """Gemini の画像モデル定数を**置かない**（置くと既定や import に紛れ込む）。"""
    from wwedit.publish import thumbnail

    assert not hasattr(thumbnail, "NANO_BANANA_2")
    assert not hasattr(thumbnail, "NANO_BANANA_2_LITE")
    assert thumbnail.DEFAULT_MODEL == "gpt-image-2.5-flare"


@pytest.mark.parametrize("bad", sorted(FORBIDDEN) + ["", "gemini-4-image", "imagen-4"])
def test_generate_image_refuses_non_flare(bad: str, monkeypatch):
    """明示的に渡されても flare 以外は**焼かない**（API を叩く前に例外）。"""
    from wwedit.publish import runware_image, thumbnail

    called: list[str] = []
    monkeypatch.setattr(runware_image, "generate_image",
                        lambda *a, **k: called.append("runware") or b"PNG")
    with pytest.raises(ValueError):
        thumbnail.generate_image("p", model=bad)
    assert not called


def test_generate_image_routes_flare_to_runware(monkeypatch):
    from wwedit.publish import runware_image, thumbnail

    seen: dict = {}

    def fake(prompt, **kw):
        seen.update(kw)
        return b"PNG"

    monkeypatch.setattr(runware_image, "generate_image", fake)
    assert thumbnail.generate_image("p", aspect_ratio="21:9", image_size="2K") == b"PNG"
    assert seen["model"] == "gpt-image-2.5-flare"
    assert seen["aspect_ratio"] == "21:9"
    assert "image_size" not in seen            # flare には渡さない
