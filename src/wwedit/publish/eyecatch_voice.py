"""[H] アイキャッチの音＝のべつべ！キャラの「一言」ボイス（決定的CLI部品）。

**方針（ユーザー確定・2026-07-26）: アイキャッチの音楽ジングルは廃止**。代わりに
のべつべ！オリジナルキャラ（SBV2 日本語モデル）が**どの章でも成立する短い一言**を喋る。
話すキャラは章ごとにランダム（seed 決定的＝再レンダリングで同じ結果）。画面にはイントロと
同じ**右上のロゴ＋キャラ名バッジ**を出して「誰が喋ったか」を見せる。

合成は `publish.aivis`（SBV2 `/synth`）に委譲＝ここは「誰が・何を言うか」を決めるだけ。
**読み用テキストはかな書き**（SBV2 は漢字/英字を誤読する＝
[[external-assets-and-keys]]・STATUS §11.5）。
"""

from __future__ import annotations

import random
from pathlib import Path

__all__ = [
    "NOBETUBE_VOICES",
    "VOICE_LINES",
    "pick_voice",
    "pick_line",
    "synth_eyecatch_voice",
]

# SBV2 日本語モデル(`model_assets/<id>`)と立ち姿素材(`<id>_a*.webp`)が**両方ある**キャラ。
# ここに無いIDを足すときは両方の存在を確認すること（名前は `character.FULL_NAME`）。
NOBETUBE_VOICES = [
    "noa", "yume", "kasumi", "priya", "reika", "ritsu", "souta", "suzu", "tsukasa",
]

# (字幕/ログ用の正表記, SBV2へ渡す**かな**読み)。どの章の頭でも成立する短い一言だけを置く。
VOICE_LINES: list[tuple[str, str]] = [
    ("つ～ぎ！", "つぎぃ！"),
    ("つぎ！", "つぎ！"),
    ("えーっと", "えーっと、"),
    ("さてと", "さてと。"),
    ("よし！", "よし！"),
    ("いってみよー！", "いってみよー！"),
    ("お次はこちら！", "おつぎはこちら！"),
    ("はいっ", "はいっ。"),
    ("それでは", "それでは、"),
    ("つづきまして", "つづきまして、"),
    ("ここからは", "ここからは、"),
    ("お楽しみに！", "おたのしみに！"),
]


def pick_voice(seed: int, *, voices: list[str] | None = None) -> str:
    """章 seed からキャラをランダム選択（決定的＝再レンダリングで同じ）。"""
    pool = voices or NOBETUBE_VOICES
    return random.Random(seed * 7919 + 13).choice(pool)


def pick_line(seed: int, *, lines: list[tuple[str, str]] | None = None) -> tuple[str, str]:
    """章 seed から一言を選ぶ。返り値 (表示用の正表記, SBV2へ渡すかな読み)。"""
    pool = lines or VOICE_LINES
    return random.Random(seed * 104729 + 7).choice(pool)


def synth_eyecatch_voice(
    out_wav: str | Path,
    *,
    seed: int = 0,
    char: str | None = None,
    line: tuple[str, str] | None = None,
    synth_fn=None,
    **synth_kw,
) -> tuple[Path, str, str, float]:
    """章 seed でキャラ・一言を決めて合成し ``(wav, char, 表示テキスト, 実尺秒)`` を返す。

    ``synth_fn(text, out, voice, **kw)->float`` を注入すればSBV2無しでテストできる。
    """
    if synth_fn is None:
        from wwedit.publish.aivis import synth_to_file

        synth_fn = synth_to_file

    char = char or pick_voice(seed)
    disp, reading = line or pick_line(seed)
    out_wav = Path(out_wav)
    dur = synth_fn(reading, out_wav, char, **synth_kw)
    return out_wav, char, disp, float(dur)
