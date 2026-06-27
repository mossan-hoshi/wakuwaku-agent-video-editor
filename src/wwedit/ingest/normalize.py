"""収録フォルダ名の正規化。

直近の命名規則 = 素の ``YYYY-MM-DD``。
先頭の日付だけ残し、それ以降（時刻 ``HH.MM.SS``・``わくわく勉強会`` 等の postfix・
絵文字・``[要録画…]``・サフィックス）は全部削除する。

例:
    ``2026-02-06 18.01.05 わくわく勉強会`` -> ``2026-02-06``
    ``2024-08-29 08.05.57 [要録画🔴] わく枠べんきょ会`` -> ``2024-08-29``
    ``20240808`` -> ``2024-08-08``
    ``2025-07-26_saburo`` -> ``2025-07-26``
"""

from __future__ import annotations

import re

__all__ = ["normalize_folder_name"]

# 先頭の日付: YYYY[-]MM[-]DD または YYYYMMDD（区切りは無し/-/./_ を許容）
_DATE_RE = re.compile(r"^\s*(\d{4})[-._/]?(\d{2})[-._/]?(\d{2})")


def normalize_folder_name(name: str) -> str:
    """フォルダ名から先頭日付を抽出し ``YYYY-MM-DD`` を返す。

    日付として解釈できない場合は ``ValueError``。
    """
    m = _DATE_RE.match(name)
    if not m:
        raise ValueError(f"先頭に日付が見つからない: {name!r}")
    year, month, day = (int(g) for g in m.groups())
    if not (1 <= month <= 12 and 1 <= day <= 31):
        raise ValueError(f"日付として不正: {name!r} -> {year}-{month}-{day}")
    return f"{year:04d}-{month:02d}-{day:02d}"
