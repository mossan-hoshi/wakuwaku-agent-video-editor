"""動き/シーン変化検出（PySceneDetect, CPU）。

画面録画を「同一フレーミングで居られる安定区間」に分割する。
AdaptiveDetector（rolling average 閾値）を主に使い、マウス移動や広告アニメ程度の
局所的・短時間の変化は無視しつつ、画面切替・スクロール等の大きな変化で区切る。

注: チャプター跨ぎ禁止（[D] のチャプター境界で region を分割）は STT/[D] 実装後に適用する。
切替 vs コンテンツ内動画の判別（オプティカルフローの空間範囲）は後続で追加する。
"""

from __future__ import annotations

from pathlib import Path

from wwedit.edl.schema import FramingRegion

__all__ = ["detect_stable_regions", "representative_time"]


def detect_stable_regions(
    video_path: str | Path,
    *,
    adaptive_threshold: float = 3.0,
    min_scene_len_s: float = 1.0,
    use_content_fallback: bool = True,
    content_threshold: float = 27.0,
    downscale: int | None = None,
) -> list[FramingRegion]:
    """安定フレーミング区間を ``FramingRegion`` 列で返す（kind="static", bbox=None）。

    ``adaptive_threshold``: AdaptiveDetector の rolling-average 閾値（小さいほど敏感）。
    ``min_scene_len_s``: これより短い区間は作らない（チラつき抑制）。
    ``downscale``: フレーム縮小率（速度向上。None で auto）。
    """
    # scenedetect は cv extra。codec 法が既定のため遅延 import にする。
    from scenedetect import AdaptiveDetector, ContentDetector, SceneManager, open_video

    video = open_video(str(video_path))
    fps = video.frame_rate or 25.0
    min_scene_len = max(1, int(min_scene_len_s * fps))

    sm = SceneManager()
    sm.add_detector(
        AdaptiveDetector(adaptive_threshold=adaptive_threshold, min_scene_len=min_scene_len)
    )
    if use_content_fallback:
        # ハードカット（画面切替）の取りこぼし対策
        sm.add_detector(
            ContentDetector(threshold=content_threshold, min_scene_len=min_scene_len)
        )

    if downscale is not None:
        sm.downscale = downscale
    else:
        sm.auto_downscale = True

    sm.detect_scenes(video, show_progress=False)
    scenes = sm.get_scene_list()

    regions: list[FramingRegion] = []
    for start_tc, end_tc in scenes:
        s = start_tc.get_seconds()
        e = end_tc.get_seconds()
        # 2検出器併用で生じる退化区間（1フレーム未満）は除去
        if e - s < 1.0 / fps:
            continue
        regions.append(FramingRegion(start=s, end=e, kind="static", bbox=None))
    return regions


def representative_time(region: FramingRegion) -> float:
    """region の代表フレーム時刻（中点）。bbox 推定のサンプリング点に使う。"""
    return (region.start + region.end) / 2.0
