"""[V] キャラ声差し替えのキャラ割当（voice-cast）。

話者→のべつべキャラの対応（``Edl.character_cast``）は音声変換・字幕色・ちびキャラ表示の
3系統が共有する SoT。ここで一括で書き込み、``revert_voice`` で全て元に戻せる（非破壊）。
割当はランダム（リロール=再実行）だが、実行前に auto-edit スキル側の G-V ゲートで
ユーザー承認を取る運用。
"""

from __future__ import annotations

import random
from datetime import datetime

from wwedit.edl.schema import ChibiConfig, Edl
from wwedit.subtitle.ass import CHAR_THEME_HEX, ensure_legible

__all__ = ["VOICE_METHODS", "NON_ORIGINAL_CHARS", "mic_speakers", "pick_cast",
           "apply_cast", "revert_voice"]

VOICE_METHODS = ("seedvc", "tts")

#: のべつべオリジナルではないキャラ。声・ちび絵ともに本編では使わない
#: （つくよみちゃんは外部のフリー素材キャラ。ちび素材は novtube 側にあるが対象外）。
NON_ORIGINAL_CHARS = frozenset({"tsukuyomi"})


def mic_speakers(edl: Edl) -> list[str]:
    """割当対象の話者（マイクトラックの話者・ソート順）。desktop音声は対象外。"""
    return sorted({t.speaker for t in edl.source.audio_tracks if not t.is_desktop_audio})


def pick_cast(
    edl: Edl, *, chars: list[str] | None = None, pool: list[str] | None = None,
    rng: random.Random | None = None,
) -> dict[str, str]:
    """話者→キャラの割当を作る（EDLへはまだ書かない）。

    ``chars`` 指名があればソート順の話者へ順に割当。無ければ ``pool`` からランダムに
    重複なしで選ぶ。話者数より pool が少なければエラー。
    のべつべオリジナルでないキャラ（``NON_ORIGINAL_CHARS``）は指名でも弾く。
    """
    speakers = mic_speakers(edl)
    if not speakers:
        raise ValueError("マイクトラックの話者がいない（ingest 済みのEDLが必要）")
    pool = [c for c in (pool if pool is not None else sorted(CHAR_THEME_HEX))
            if c not in NON_ORIGINAL_CHARS]
    if chars:
        excluded = [c for c in chars if c in NON_ORIGINAL_CHARS]
        if excluded:
            raise ValueError(
                f"のべつべオリジナルではないキャラは使えない: {excluded}")
        bad = [c for c in chars if c not in CHAR_THEME_HEX]
        if bad:
            raise ValueError(f"未知のキャラID: {bad}（候補: {', '.join(sorted(CHAR_THEME_HEX))}）")
        if len(chars) < len(speakers):
            raise ValueError(f"キャラ指名が足りない（話者{len(speakers)}人に{len(chars)}体）")
        picked = list(chars[: len(speakers)])
    else:
        if len(pool) < len(speakers):
            raise ValueError(f"参照音声のあるキャラが足りない（{len(pool)} < {len(speakers)}）")
        picked = (rng or random).sample(pool, len(speakers))
    return dict(zip(speakers, picked, strict=True))


def apply_cast(edl: Edl, cast: dict[str, str], *, method: str) -> None:
    """割当を EDL へ一括で書き込む（character_cast / 字幕色 / chibi有効化 / meta.voice）。

    初回のみ元の字幕色設定を ``meta["voice"]["prev_colors"]`` に退避する
    （再キャストしても最初のスナップショットを保持＝revert で完全に戻せる）。
    """
    if method not in VOICE_METHODS:
        raise ValueError(f"method は {'/'.join(VOICE_METHODS)} のいずれか: {method!r}")
    meta_voice = edl.meta.get("voice") or {}
    if "prev_colors" not in meta_voice:
        meta_voice["prev_colors"] = dict(edl.subtitle_speaker_colors or {})
    meta_voice["method"] = method
    meta_voice["confirmed_at"] = datetime.now().isoformat(timespec="seconds")
    edl.meta["voice"] = meta_voice

    edl.character_cast = dict(cast)
    for speaker, char in cast.items():
        edl.subtitle_speaker_colors[speaker] = char
    if edl.chibi is None:
        edl.chibi = ChibiConfig(enabled=True)
    else:
        edl.chibi.enabled = True


def revert_voice(edl: Edl) -> list[str]:
    """voice-cast 以降の変更を全て戻す。戻した項目の説明リストを返す。"""
    done: list[str] = []
    meta_voice = edl.meta.get("voice") or {}
    if "prev_colors" in meta_voice:
        edl.subtitle_speaker_colors = dict(meta_voice["prev_colors"])
        done.append("字幕色を元に戻した")
    if "prev_subtitles" in meta_voice:
        from wwedit.edl.schema import Subtitle

        edl.subtitles = [Subtitle(**s) for s in meta_voice["prev_subtitles"]]
        done.append(f"字幕を元の{len(edl.subtitles)}件に戻した（方式Bの読み上げ字幕を破棄）")
    if edl.character_cast:
        edl.character_cast = {}
        done.append("character_cast を解除")
    if edl.freezes:
        done.append(f"freezes {len(edl.freezes)}件を削除")
        edl.freezes = []
    for t in edl.source.audio_tracks:
        if t.voice_path:
            t.voice_path = None
            done.append(f"{t.speaker} の voice_path を解除")
    if edl.chibi and edl.chibi.enabled:
        edl.chibi.enabled = False
        done.append("chibi 表示を無効化")
    if "voice" in edl.meta:
        edl.meta.pop("voice")
        done.append("meta.voice を削除")
    return done


def describe_cast(cast: dict[str, str]) -> list[tuple[str, str, str]]:
    """表示用: (話者, キャラ, 字幕色hex[明度補正後]) の行を返す。"""
    rows = []
    for speaker, char in sorted(cast.items()):
        hex_c = ensure_legible(CHAR_THEME_HEX[char])
        rows.append((speaker, char, hex_c))
    return rows
