"""収録フォルダのトラック判別。

典型構成:
    video<id>.mp4            … Zoom合成画面録画（メイン映像。音声は無音基準に使う）
    audio<id>.m4a            … Zoom合成音声（参考。基本は使わない）
    Audio Record/audio<speaker><idx><id>.m4a … 話者別マイク録音（基本2本）
    recording.conf           … {"items":[...],"magic_number":"..."}

注意: recording.conf の magic_number は実ファイル名の連番と1桁ずれることがある
（例 conf="992213112" / ファイル="1992213112"）。そのため magic_number は信頼せず、
``video<id>.mp4`` の id を共通サフィックスとして話者別ファイル名から剥がして話者名を得る。
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from wwedit.edl.schema import SpeakerTrack

__all__ = ["RecordingTracks", "detect_tracks"]

# デスクトップ音声録音らしき話者名（文字起こし対象外）。必要に応じ拡張。
_DESKTOP_HINTS = ("desktop", "system", "システム", "pc音声", "デスクトップ")

_VIDEO_RE = re.compile(r"^video(\d+)\.mp4$", re.IGNORECASE)


class RecordingTracks(BaseModel):
    recording_dir: str
    video_path: str
    video_id: str
    combined_audio_path: str | None = None
    speaker_tracks: list[SpeakerTrack] = Field(default_factory=list)


def _parse_speaker_index(stem: str, video_id: str) -> tuple[str, int]:
    """``audio<speaker><idx><id>`` -> ``(<speaker>, <idx>)``。

    ``idx`` は Zoom の入室順の連番。**同じ人が2枠で入ると連番だけが違う2本**になる
    （マイク＝発話 ／ もう1枠＝PC音声）。どちらが先かは連番で決まるので、
    ファイル名の並び（OSで大文字小文字の扱いが違う）ではなくこの値で判定する。
    """
    core = stem
    if core.lower().startswith("audio"):
        core = core[len("audio") :]
    if video_id and core.endswith(video_id):
        core = core[: -len(video_id)]
    name = core.rstrip("0123456789") or core
    digits = core[len(name) :]
    return name, int(digits) if digits.isdigit() else 0


def _parse_speaker(stem: str, video_id: str) -> str:
    """``audio<speaker><idx><id>`` -> ``<speaker>``。"""
    return _parse_speaker_index(stem, video_id)[0]


def detect_tracks(folder: str | Path) -> RecordingTracks:
    """収録フォルダから映像・話者別音声を解決する。"""
    folder = Path(folder)
    if not folder.is_dir():
        raise FileNotFoundError(f"フォルダがない: {folder}")

    # メイン映像
    videos = sorted(folder.glob("video*.mp4"))
    if not videos:
        raise FileNotFoundError(f"video*.mp4 が見つからない: {folder}")
    video_path = videos[0]
    m = _VIDEO_RE.match(video_path.name)
    video_id = m.group(1) if m else ""

    # 合成音声（参考）
    combined = sorted(folder.glob("audio*.m4a"))
    combined_audio = str(combined[0]) if combined else None

    # 話者別音声
    #
    # **1話者につき最大2本**＝マイク（発話）と PC 音声（画面共有で流した音楽など）。
    # 参加者は音を流すために別枠でもう1つ入るので、同じ表示名で連番違いの2本になる
    # （例: Taniguchi2=発話 / Taniguchi3=PC音声）。**両者のPCそれぞれから入り得る**ので
    # 話者ごとに独立して判定する。連番の小さい方＝先に入った枠＝発話。
    # PC音声は**合成には混ぜるが文字起こしはしない**（音楽をSTTにかけると幻聴の語が入り、
    # かつ本当の発話トラックを取り違えて丸ごと落とす。2026-08-03 で実際に踏んだ）。
    speaker_tracks: list[SpeakerTrack] = []
    rec_dir = folder / "Audio Record"
    if rec_dir.is_dir():
        files = sorted(rec_dir.glob("audio*.m4a"))
        parsed = [(f, *_parse_speaker_index(f.stem, video_id)) for f in files]
        voice_idx: dict[str, int] = {}
        for _f, speaker, idx in parsed:
            if speaker not in voice_idx or idx < voice_idx[speaker]:
                voice_idx[speaker] = idx
        for f, speaker, idx in parsed:
            is_desktop = (
                any(h in speaker.lower() for h in _DESKTOP_HINTS)
                or idx != voice_idx[speaker]
            )
            speaker_tracks.append(
                SpeakerTrack(speaker=speaker, path=str(f), is_desktop_audio=is_desktop)
            )

    return RecordingTracks(
        recording_dir=str(folder),
        video_path=str(video_path),
        video_id=video_id,
        combined_audio_path=combined_audio,
        speaker_tracks=speaker_tracks,
    )
