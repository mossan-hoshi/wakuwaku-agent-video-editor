"""[I] BGM ファイルの whitelist 化と決定的選曲。

収録の BGM フォルダ（例 ``assets/sounds/bgms/202510``）は **ゴミ混在**:
- 画像/メタ（``AlbumArtSmall.jpg`` / ``Folder.jpg`` / ``desktop.ini``）
- 連番重複（``Cotton Candy Skies (2).mp3`` は ``Cotton Candy Skies.mp3`` の重複）

正規ファイルだけ拾い、重複は素の名前へ統合する。選曲は ``Math.random`` を使わず**キーの
ハッシュで決定的**にする（セクション毎に変える＝キーにセクションindex/タイトルを渡す）。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

__all__ = [
    "AUDIO_EXTS",
    "canonical_stem",
    "is_valid_bgm",
    "list_bgms",
    "pick_bgm",
    "order_bgms",
]

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg"}
_VARIANT_RE = re.compile(r"\s*\(\d+\)\s*$")  # 末尾 " (2)" 等


def canonical_stem(name: str) -> str:
    """ファイル名（拡張子なし）から連番サフィックス ``(N)`` を除いた正規名。"""
    return _VARIANT_RE.sub("", Path(name).stem).strip()


def is_valid_bgm(path: str | Path) -> bool:
    """音声拡張子のみ採用。画像/メタ等のゴミは除外。"""
    return Path(path).suffix.lower() in AUDIO_EXTS


def _content_hash(path: Path) -> str:
    """ファイル内容の md5（真の重複＝バイト一致を判定するため）。"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def list_bgms(directory: str | Path) -> list[Path]:
    """フォルダ内の正規 BGM を返す（ゴミ除外＋**真の重複のみ**統合）。

    統合するのは **正規名(``(N)`` 除去後)が同じ かつ 内容が完全一致(md5)** の時だけ。
    ``long bgm for podcast (1..8)`` のように **``(N)`` が別曲を表すフォルダでも全曲残す**
    （名前だけでの誤統合を避ける＝「1曲ループ」を防ぐ）。真の再DL重複（バイト一致）は
    サフィックス無しの素名を優先して1つに統合。返りは**ファイル名順**。
    """
    directory = Path(directory)
    if not directory.is_dir():
        return []
    by_key: dict[tuple[str, str], Path] = {}
    for p in sorted(directory.iterdir(), key=lambda x: x.name):
        if not p.is_file() or not is_valid_bgm(p):
            continue
        canon = canonical_stem(p.name)
        if not canon:
            continue
        key = (canon, _content_hash(p))
        prev = by_key.get(key)
        if prev is None:
            by_key[key] = p
        elif _VARIANT_RE.search(prev.stem) and not _VARIANT_RE.search(p.stem):
            by_key[key] = p  # 同一内容の重複はサフィックス無し（素の名前）を優先
    return sorted(by_key.values(), key=lambda x: x.name)


def pick_bgm(bgms: list[Path], key: str) -> Path | None:
    """``key`` のハッシュで決定的に1曲選ぶ（同じkeyは同じ曲・乱数非依存）。空なら None。"""
    if not bgms:
        return None
    h = int(hashlib.sha1(key.encode("utf-8")).hexdigest(), 16)
    return bgms[h % len(bgms)]


def order_bgms(bgms: list[Path], key: str) -> list[Path]:
    """``key`` で**全曲を決定的にシャッフル**した並びを返す（プレイリスト用）。

    1曲ループではなく**同ジャンル複数曲をランダム順に**敷くための順序付け。各曲を
    ``sha1(key|曲名)`` でソートキー化して並べる＝**カテゴリは固定のままランダムな並び**。
    ``Math.random`` を使わず ``key``（収録dir）seed なので、収録ごとに毎回違う順だが、
    同じ収録の再レンダリングでは常に同じ並び（再現可能）。``bgms`` が空なら空リスト。
    """
    if not bgms:
        return []

    def sort_key(p: Path) -> str:
        return hashlib.sha1(f"{key}|{p.name}".encode()).hexdigest()

    return sorted(bgms, key=sort_key)
