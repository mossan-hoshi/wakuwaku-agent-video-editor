"""音量エンベロープ（RMS）ベースの切れ目スナップ（ぶつ切り回避）。

STT/VAD が出す切れ目は時刻ベースで音の途中に落ちることがある。実音声の短窓RMSを取り、
各カット境界を近傍の**低エネルギー点（音量の谷）にスナップ**して、自然な切れ目にする。
さらにフィラーは「両端が谷＝発話に溶けていない端のフィラー」のみ採用し、流暢な発話の
途中を切らない（ユーザー指摘: フィラーは発話前後に出るもの）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = ["Envelope", "load_envelope", "snap_time", "refine_segments"]

SR = 16000


@dataclass
class Envelope:
    rms: object  # np.ndarray (frame毎RMS)
    hop_s: float
    floor: float  # 無音床（低パーセンタイル）

    def at(self, t: float) -> float:
        i = int(round(t / self.hop_s))
        if i < 0:
            i = 0
        elif i >= len(self.rms):
            i = len(self.rms) - 1
        return float(self.rms[i])


def load_envelope(audio_path: str | Path, *, hop_ms: int = 10, win_ms: int = 25) -> Envelope:
    """音声を16kHz monoで読み、短窓RMSのエンベロープを作る。"""
    import numpy as np
    import whisperx

    a = np.asarray(whisperx.load_audio(str(audio_path)), dtype=np.float32)
    hop = max(1, int(SR * hop_ms / 1000))
    win = max(hop, int(SR * win_ms / 1000))
    if len(a) < win:
        a = np.pad(a, (0, win - len(a)))
    n = 1 + (len(a) - win) // hop
    # フレーム毎RMSをベクトル化（sliding window view）
    idx = np.arange(n)[:, None] * hop + np.arange(win)[None, :]
    frames = a[idx]
    rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)
    floor = float(np.percentile(rms, 20))  # 無音床の目安
    return Envelope(rms=rms, hop_s=hop_ms / 1000.0, floor=floor)


def snap_time(t: float, env: Envelope, *, search_s: float = 0.12) -> float:
    """時刻 t を近傍 ±search_s の最小RMS点（音量の谷）へスナップする。"""
    import numpy as np

    hop = env.hop_s
    i = int(round(t / hop))
    w = max(1, int(search_s / hop))
    lo = max(0, i - w)
    hi = min(len(env.rms), i + w + 1)
    if hi <= lo:
        return t
    j = lo + int(np.argmin(env.rms[lo:hi]))
    return j * hop


def refine_segments(
    segments: list,
    env: Envelope,
    *,
    search_s: float = 0.12,
    filler_clean_factor: float = 3.0,
):
    """セグメント境界を音量の谷へスナップし、発話に溶け込んだフィラーのみ不採用にする。

    - 連続セグメントの内部境界を ``snap_time`` で谷へ寄せる（無音・フィラー共通＝切れ目を自然化）。
    - フィラーは発話の前後（間に隣接）に出るもの。スナップ後の**片端でも谷（RMS≦無音床×factor）**
      なら採用＝そこに自然な切れ目がある。両端とも高エネルギー＝連続発話に完全に埋もれている
      場合のみ keep に戻す（発話途中を切らない＝ぶつ切り回避）。

    返り値は新しい Segment 列（``Segment`` は ``segments`` の要素型を流用）。
    """
    from wwedit.edl.schema import Segment

    if not segments:
        return segments
    # 内部境界をスナップ（端点[0]と末尾は固定）
    bounds = [segments[0].start] + [s.end for s in segments]
    for k in range(1, len(bounds) - 1):
        bounds[k] = snap_time(bounds[k], env, search_s=search_s)
    # 単調性を保証
    for k in range(1, len(bounds)):
        if bounds[k] < bounds[k - 1]:
            bounds[k] = bounds[k - 1]

    clean_thr = env.floor * filler_clean_factor
    out: list = []
    idx = 0
    for s, lo, hi in zip(segments, bounds[:-1], bounds[1:], strict=False):
        if hi - lo <= 1e-3:
            continue  # 潰れた区間は捨てる
        invalid = s.invalid
        reason = s.reason
        if invalid and reason == "filler":
            # 片端でも谷なら自然な切れ目あり＝採用。両端とも高エネルギー＝発話に埋没→keepに戻す
            if env.at(lo) > clean_thr and env.at(hi) > clean_thr:
                invalid = False
                reason = None
        out.append(
            Segment(id=f"r{idx:04d}", start=lo, end=hi, invalid=invalid, reason=reason)
        )
        idx += 1
    return _merge_adjacent_keeps(out)


def _merge_adjacent_keeps(segments: list):
    """フィラー差し戻しで隣接した keep 同士を結合して区間数を抑える。"""
    from wwedit.edl.schema import Segment

    merged: list = []
    for s in segments:
        if merged and not s.invalid and not merged[-1].invalid:
            prev = merged[-1]
            merged[-1] = Segment(id=prev.id, start=prev.start, end=s.end, invalid=False)
        else:
            merged.append(s)
    return merged
