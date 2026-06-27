"""[B] 文字起こし — 話者別 m4a を word単位+フィラー保持で文字起こしし、話者統合する。

faster-whisper (large-v3, int8_float16/float16) が土台（Step0）。`audio2srt` のパターンを流用。
フィラー保持のため initial_prompt にフィラー例文を入れ、VAD を緩める/切る。
出力は EDL.utterances（話者ラベル付き発話列）。後段はフィラーカットと [D] チャプター判定に使う。
"""

from wwedit.transcribe.merge import merge_speakers
from wwedit.transcribe.stt import FILLER_PROMPT, Word, load_model, transcribe_track

__all__ = ["FILLER_PROMPT", "Word", "load_model", "transcribe_track", "merge_speakers"]
