"""[E]/[4] フレーミング — 動き/シーン変化検出とメイン領域 bbox。

- ``motion.detect_stable_regions`` : PySceneDetect で「同一フレーミングで居られる
  安定区間」と「変化区間」を分ける（CPUのみ、GPU不要）。
  AdaptiveDetector の rolling average でマウス移動・広告アニメ程度の局所変化は許容し、
  画面切替・スクロール等の大きな変化でカットする。
- （後続）メイン領域 bbox は OmniParser V2 + ヒューリスティック（[3]）。
- （後続）切替 vs コンテンツ内動画の判別はオプティカルフローの空間的広がりで。
"""

from wwedit.framing.motion import detect_stable_regions, representative_time

__all__ = ["detect_stable_regions", "representative_time"]
