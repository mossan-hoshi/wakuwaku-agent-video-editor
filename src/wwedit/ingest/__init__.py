"""[A] 取り込み/正規化 — フォルダ名正規化・トラック判別・magic解決。"""

from wwedit.ingest.normalize import normalize_folder_name
from wwedit.ingest.tracks import RecordingTracks, detect_tracks

__all__ = ["normalize_folder_name", "RecordingTracks", "detect_tracks"]
