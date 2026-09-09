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


# ── 構図: **コードで決めない**（決めると毎回同じ絵になる）────────────────────
# 2026-08-07 ユーザー指摘「正面バストアップ構図、もう飽きた」「リスト明示するな。
# 直近10動画の構図をメモっておいて被らないように自由に選べ」。
def test_no_default_framing_is_baked_in() -> None:
    """framing 未指定なら**構図の指示が一切入らない**（既定の正面バストアップを持たない）。"""
    p = build_prompt("A scene.", "noa").lower()
    assert "bust-up" not in p
    assert "framing:" not in p
    assert "40% of the frame" not in p


def test_caller_supplied_framing_is_free_text() -> None:
    """呼び出し側の自由文がそのまま入る（enum に無い言い回しでも通る）。"""
    f = "knee-up from a low angle near the ground, subject on the right third"
    p = build_prompt("A scene.", "noa", f)
    assert f in p
    assert "Framing: " + f in p


def test_lipsync_safety_floor_is_always_present() -> None:
    """口が見える・顔が切れない・3/4より外を向かない、は構図に関わらず必ず付く。"""
    for f in ("", "extreme close-up, dutch tilt"):
        p = build_prompt("A scene.", "noa", f).lower()
        assert "mouth must be fully visible" in p
        assert "not turned away past a 3/4 view" in p
