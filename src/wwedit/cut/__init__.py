"""[C] 無音/フィラー検出 — カット候補（invalid区間）の算出。

**重要: 固定dB閾値は使わない（収録ごとに録音音量が変動するため）。**
工程順は [B] STT → [C]。まず STT の word単位タイムスタンプで発話/非発話期間を把握し、
狭い期間（隙間ごと/数十秒窓）の局所音量を測って区間ごとに動的に閾値を決める。

- ``silence.detect_silence`` は ffmpeg silencedetect の低レベルヘルパ（局所窓に対して
  局所ノイズフロアを与えて使う想定）。グローバル固定閾値での一括検出には使わない。
- フィラー = 話者別トランスクリプトのフィラー語＋音響特徴。

STT 実装後に動的検出器として完成させ、CLI に登録する（現状は未登録）。
将来は Webアプリの修正ログ＋fcpxml由来データセットで学習したモデルに差し替える。
"""

from wwedit.cut.silence import SilenceInterval, detect_silence, silence_to_segments

__all__ = ["SilenceInterval", "detect_silence", "silence_to_segments"]
