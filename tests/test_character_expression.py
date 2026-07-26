"""キャラ別の表情（mascot.md 準拠）のテスト。

全キャラ一律で「笑顔」にすると**キャラ崩れ**になる（2026-07-26 実際に踏んだ:
ジト目設定の yume を満面の笑みで生成してしまった）。
"""

from __future__ import annotations

from wwedit.publish.character import (
    EXPRESSION,
    FULL_NAME,
    build_prompt,
    expression_of,
)


def test_yume_is_deadpan_not_smiling() -> None:
    """mascot.md: ゆめ＝「ボソボソ声でジト目」「眠そうなピンクの目」＝笑わせない。"""
    e = expression_of("yume").lower()
    assert "no smile" in e
    assert "deadpan" in e or "half-lidded" in e


def test_expression_never_defaults_to_smile() -> None:
    # 未登録キャラは中立。勝手に笑顔にしない
    assert "smile" not in expression_of("unknown_char").lower()


def test_every_known_character_has_an_expression() -> None:
    assert set(FULL_NAME) <= set(EXPRESSION)


def test_build_prompt_injects_character_expression() -> None:
    p = build_prompt("She stands under a summer sky.", "yume")
    assert "no smile" in p.lower()
    assert "gentle friendly smile" not in p  # 旧・一律の笑顔が混ざらない
    assert "She stands under a summer sky." in p


def test_build_prompt_without_char_is_neutral() -> None:
    p = build_prompt("Anything.").lower()
    assert "smile" not in p
