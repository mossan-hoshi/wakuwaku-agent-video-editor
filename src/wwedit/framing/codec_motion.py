"""コーデック由来の動き検出（超軽量・ピクセル非デコード）。

動画コーデックのインター予測（P/Bフレーム）には既に動き情報が入っている。
``ffprobe -show_packets``（純デマックス、ピクセル復元なし）でフレームごとの
**符号化バイト数**とキーフレームフラグを読むだけで「静止 vs 動き」が分かる:

- 画面が完全静止 → Pフレームは数十バイト（残差ほぼゼロ）
- マウス移動・広告アニメ → 局所的な小さい残差（数百バイト）
- 画面切替・スクロール・動画再生 → 残差が急増（数KB〜）

26分1080pでも実測 0.6 秒（PySceneDetect 全フレーム解析の約270倍速）。
キーフレーム(I)は動きと無関係に intra コストで大きくなるため、近傍の非キーフレームから
補間して動き信号から除外する。閾値は収録ごとのバイト数分布から適応的に決める
（固定閾値にしない＝録音/録画品質の変動に追従）。
"""

from __future__ import annotations

import statistics
import subprocess
from dataclasses import dataclass
from pathlib import Path

from wwedit.common.media import ffprobe_path
from wwedit.edl.schema import FramingRegion

__all__ = ["FramePacket", "read_frame_packets", "detect_stable_regions_codec"]


@dataclass
class FramePacket:
    time: float
    size: int
    is_key: bool


def read_frame_packets(video_path: str | Path) -> list[FramePacket]:
    """``ffprobe -show_packets`` で各フレームの (時刻, 符号化サイズ, キーフレーム) を返す。"""
    cmd = [
        ffprobe_path(),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_packets",
        "-show_entries",
        "packet=pts_time,size,flags",
        "-of",
        "csv",
        str(video_path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe 失敗: {video_path}\n{out.stderr}")

    packets: list[FramePacket] = []
    for line in out.stdout.splitlines():
        parts = line.split(",")
        # 形式: packet,<pts_time>,<size>,<flags>
        if len(parts) < 4 or parts[0] != "packet":
            continue
        try:
            t = float(parts[1])
            size = int(parts[2])
        except ValueError:
            continue
        is_key = "K" in parts[3]
        packets.append(FramePacket(time=t, size=size, is_key=is_key))
    packets.sort(key=lambda p: p.time)
    return packets


def _motion_signal(packets: list[FramePacket]) -> list[float]:
    """キーフレームを近傍の非キーフレーム値で補間した動き信号（バイト）。"""
    sizes = [float(p.size) for p in packets]
    n = len(packets)
    for i, p in enumerate(packets):
        if not p.is_key:
            continue
        # 近傍の非キーフレームを左右に探して補間
        left = next((sizes[j] for j in range(i - 1, -1, -1) if not packets[j].is_key), None)
        right = next((sizes[j] for j in range(i + 1, n) if not packets[j].is_key), None)
        if left is not None and right is not None:
            sizes[i] = (left + right) / 2.0
        elif left is not None:
            sizes[i] = left
        elif right is not None:
            sizes[i] = right
        else:
            sizes[i] = 0.0
    return sizes


def _adaptive_threshold(signal: list[float], k: float, floor_bytes: float) -> float:
    """中央値＋k×MAD による適応閾値（外れ値=動きに頑健）。"""
    nonzero = [s for s in signal if s > 0]
    if not nonzero:
        return floor_bytes
    med = statistics.median(nonzero)
    mad = statistics.median([abs(s - med) for s in nonzero]) or 1.0
    return max(floor_bytes, med + k * mad)


def detect_stable_regions_codec(
    video_path: str | Path,
    *,
    k: float = 6.0,
    floor_bytes: float = 800.0,
    smooth: int = 3,
    min_region_s: float = 1.0,
    bridge_gap_s: float = 0.4,
) -> list[FramingRegion]:
    """符号化サイズから安定フレーミング区間を検出する。

    ``k`` / ``floor_bytes``: 適応閾値（中央値+k*MAD と floor の大きい方）。動きフレーム判定。
    ``smooth``: 移動平均窓（フレーム）。単発スパイク（瞬間ノイズ）を抑える。
    ``min_region_s``: これより短い区間は隣接へ吸収。
    ``bridge_gap_s``: これ以下の短い動きは無視して静止区間を繋ぐ（マウス一瞬の移動等）。

    返すのは静止区間=kind="static" と 動き区間=kind="pending"（後で切替/動画再生を判別）。
    """
    packets = read_frame_packets(video_path)
    if not packets:
        return []
    sig = _motion_signal(packets)

    # 移動平均で平滑化
    if smooth > 1:
        half = smooth // 2
        smoothed = []
        for i in range(len(sig)):
            lo = max(0, i - half)
            hi = min(len(sig), i + half + 1)
            smoothed.append(sum(sig[lo:hi]) / (hi - lo))
        sig = smoothed

    thr = _adaptive_threshold(sig, k=k, floor_bytes=floor_bytes)
    active = [s > thr for s in sig]  # True = 動き

    times = [p.time for p in packets]
    end_time = times[-1] + (times[-1] - times[-2] if len(times) > 1 else 0.04)

    # 連続する同状態フレームを区間化
    raw: list[tuple[float, float, bool]] = []
    cur_state = active[0]
    cur_start = times[0]
    for i in range(1, len(active)):
        if active[i] != cur_state:
            raw.append((cur_start, times[i], cur_state))
            cur_state = active[i]
            cur_start = times[i]
    raw.append((cur_start, end_time, cur_state))

    # 短い動き区間は橋渡し（静止に吸収）
    bridged: list[tuple[float, float, bool]] = []
    for s, e, st in raw:
        if st and (e - s) <= bridge_gap_s and bridged and not bridged[-1][2]:
            # 直前が静止 → 短い動きを静止へ繋ぐ
            ps, pe, _ = bridged.pop()
            bridged.append((ps, e, False))
        elif bridged and bridged[-1][2] == st:
            ps, pe, _ = bridged.pop()
            bridged.append((ps, e, st))
        else:
            bridged.append((s, e, st))

    # 短すぎる区間を隣接へ吸収しつつ FramingRegion 化
    regions: list[FramingRegion] = []
    for s, e, st in bridged:
        if e - s < min_region_s and regions:
            regions[-1].end = e  # 直前区間へ延長吸収
            continue
        regions.append(
            FramingRegion(start=s, end=e, kind="static" if not st else "pending", bbox=None)
        )
    return regions
