"""editor.html の埋め込みJSが構文エラーで丸ごと死んでいないかを検査する。

JSが1文字でも壊れるとタイムラインが**何も表示されなくなる**（描画は全部このスクリプト）。
Pythonのテストでは気づけないので、node があるときだけ構文チェックを回す。
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = Path(__file__).resolve().parents[1] / "src/wwedit/webapp/static/editor.html"


def test_editor_inline_js_parses(tmp_path: Path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node が無い環境ではスキップ")
    src = HTML.read_text(encoding="utf-8")
    m = re.search(r"<script>(.*)</script>", src, re.S)
    assert m, "editor.html に <script> ブロックが無い"
    js = tmp_path / "editor_inline.js"
    js.write_text(m.group(1), encoding="utf-8")
    p = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
    assert p.returncode == 0, f"editor.html の JS が構文エラー:\n{p.stderr}"
