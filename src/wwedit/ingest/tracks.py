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


def _parse_speaker(stem: str, video_id: str) -> str:
    """``audio<speaker><idx><id>`` -> ``<speaker>``。"""
    core = stem
    if core.lower().startswith("audio"):
        core = core[len("audio") :]
    if video_id and core.endswith(video_id):
        core = core[: -len(video_id)]
    # 末尾のトラック連番（1桁）を除去
    return core.rstrip("0123456789") or core


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
    speaker_tracks: list[SpeakerTrack] = []
    rec_dir = folder / "Audio Record"
    if rec_dir.is_dir():
        for f in sorted(rec_dir.glob("audio*.m4a")):
            speaker = _parse_speaker(f.stem, video_id)
            is_desktop = any(h in speaker.lower() for h in _DESKTOP_HINTS)
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
