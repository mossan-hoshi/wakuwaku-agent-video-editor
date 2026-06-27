"""[E] 動き区間(pending)の種別判定：画面切替 vs コンテンツ内動画。

SDD [E]: 「切替 vs コンテンツ内動画はオプティカルフローの空間的広がり(全画面変化 vs
局所変化)で判別」。区間内で密なオプティカルフローを取り、動いたセルの割合(spread, 0..1)を測る:
- spread 大（全画面が動く） → **画面切替** → kind="loading"（ローディング画面に置換）。
- spread 小（局所だけ動く） → **コンテンツ内動画** → kind は "pending" のまま warning を付け、
  作業は止めずユーザー確認に回す（同一フレーミングの動画再生か要判断）。

純粋コア ``spread_from_flow`` は合成配列でテスト可。動画I/Oは ``region_motion_spread`` に隔離。
オプティカルフロー(cv2)は関数内で遅延import（CLIロードを壊さない）。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from wwedit.edl.schema import FramingRegion

__all__ = [
    "SWITCH_SPREAD_THR",
    "spread_from_flow",
    "region_motion_spread",
    "classify_pending_region",
]

# spread がこの値以上なら「全画面が動いた=画面切替」とみなす。
SWITCH_SPREAD_THR = 0.6


def spread_from_flow(mag, *, grid: int = 16, mag_thr: float = 2.0) -> float:
    """フロー magnitude 配列(H,W) → 動いたグリッドセルの割合(0..1)。

    フレームを grid×grid に分割し、各セルの平均移動量が mag_thr 超なら「動いたセル」。
    全画面が動く(切替)→1に近い、局所動画→小さい。
    """
    import numpy as np

    m = np.asarray(mag, dtype="float32")
    if m.ndim != 2 or m.size == 0:
        return 0.0
    h, w = m.shape
    gh, gw = max(1, h // grid), max(1, w // grid)
    moved = total = 0
    for gy in range(0, h - gh + 1, gh):
        for gx in range(0, w - gw + 1, gw):
            cell = m[gy : gy + gh, gx : gx + gw]
            total += 1
            if float(cell.mean()) > mag_thr:
                moved += 1
    return moved / total if total else 0.0


def region_motion_spread(
    video_path: str | Path,
    start: float,
    end: float,
    *,
    samples: int = 6,
    grid: int = 16,
    mag_thr: float = 2.0,
    downscale_w: int = 640,
) -> float:
    """区間 [start,end) を samples 枚サンプリングし、連続フレーム間フローの spread の中央値。"""
    import cv2
    import numpy as np

    from wwedit.framing.dataset import _extract_frame

    if end <= start or samples < 2:
        return 0.0
    tmp = Path(tempfile.mkdtemp())
    frames = []
    for i in range(samples):
        t = start + (end - start) * (i + 0.5) / samples
        png = tmp / f"f{i}.png"
        if not _extract_frame(str(video_path), t, png):
            continue
        g = cv2.imread(str(png), cv2.IMREAD_GRAYSCALE)
        if g is None:
            continue
        if g.shape[1] > downscale_w:
            scale = downscale_w / g.shape[1]
            g = cv2.resize(g, (downscale_w, max(1, int(g.shape[0] * scale))))
        frames.append(g)
    if len(frames) < 2:
        return 0.0
    spreads = []
    for a, b in zip(frames, frames[1:], strict=False):
        flow = cv2.calcOpticalFlowFarneback(a, b, None, 0.5, 3, 15, 3, 5, 1.2, 0)
        mag = np.sqrt(flow[..., 0] ** 2 + flow[..., 1] ** 2)
        spreads.append(spread_from_flow(mag, grid=grid, mag_thr=mag_thr))
    spreads.sort()
    return spreads[len(spreads) // 2] if spreads else 0.0


def classify_pending_region(
    video_path: str | Path,
    region: FramingRegion,
    *,
    thr: float = SWITCH_SPREAD_THR,
    **kw,
) -> FramingRegion:
    """pending 区間を spread で分類して kind/warning を更新（同じ region を返す）。

    spread≥thr → "loading"（画面切替）。未満 → "pending" のまま warning（コンテンツ内動画疑い）。
    pending 以外の区間は変更しない。
    """
    if region.kind != "pending":
        return region
    spread = region_motion_spread(video_path, region.start, region.end, **kw)
    if spread >= thr:
        region.kind = "loading"
        region.warning = ""
    else:
        region.warning = (
            f"局所的な動き(spread={spread:.2f})＝コンテンツ内動画の可能性。要確認"
        )
    return region
