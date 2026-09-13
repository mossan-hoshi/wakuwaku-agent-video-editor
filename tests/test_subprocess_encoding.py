"""子プロセスの出力を文字列で受けるときは **encoding を必ず明示**する。

Windows で ``text=True`` だけだと出力を cp932 で読む。ffmpeg の stderr は UTF-8 で
日本語パス（ジングル名など）を含むので、読み取りスレッドが UnicodeDecodeError で落ち、
失敗時のエラー文も取れなくなる（2026-09-13 `publish intro-compose` で実際に出た）。
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "wwedit"
_FUNCS = {"run", "Popen", "check_output", "check_call", "call"}


def _is_true(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def test_text_mode_subprocess_calls_set_encoding() -> None:
    bad: list[str] = []
    for py in sorted(SRC.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name not in _FUNCS:
                continue
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            text_mode = any(_is_true(kw[k]) for k in ("text", "universal_newlines") if k in kw)
            if text_mode and "encoding" not in kw:
                bad.append(f"{py.relative_to(SRC)}:{node.lineno}")
    assert not bad, "text=True なのに encoding が無い: " + ", ".join(bad)
