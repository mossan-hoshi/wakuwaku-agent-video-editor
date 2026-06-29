"""環境変数の取得（os.environ 優先・無ければ .env から読む）。

API キー等は `.env`(gitignore済) に置く運用だが、CLI は .env を自動で環境変数へ
読み込まない。そこで os.environ→.env の順で1値を解決する小ヘルパを共通化する
（秘匿値はコード/リポジトリに残さない＝[[pii-masking-and-ocr-engine]] と同方針）。
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["env_value"]


def _parse_env_file(path: Path, key: str) -> str | None:
    """依存を増やさず .env から1キーだけ読む簡易パーサ。"""
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


def env_value(key: str, *, env_file: str | Path = ".env") -> str | None:
    """環境変数 key を取得。os.environ を優先し、無ければ .env から読む。未設定なら None。"""
    v = os.environ.get(key)
    if v:
        return v
    return _parse_env_file(Path(env_file), key)
