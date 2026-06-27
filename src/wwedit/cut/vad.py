"""silero VAD による発話区間検出（無音カットの本命・高速）。

無音カットに**全文STTは不要**。VADは発話/非発話を音量に頑健に（学習モデル＝固定閾値不要）、
1トラック数十秒で直接返す。話者別トラックごとに発話区間を取り、全話者の和集合＝keep、
その隙間＝無音カット候補とする。STT(WhisperX)はフィラー/チャプター用で別工程。

silero VAD は whisperx 同梱の `torch.hub` 経由モデルを使う（追加導入なし）。16kHz mono 前提。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = ["speech_regions", "speech_regions_multi"]

Interval = tuple[float, float]

SILERO_SR = 16000


@dataclass
class _VadModel:
    model: object
    get_ts: object  # get_speech_timestamps


_CACHE: _VadModel | None = None


def _load_silero() -> _VadModel:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    import torch

    # whisperx と同様に snakers4/silero-vad を hub から取得（torch.load patch 不要）
    model, utils = torch.hub.load(
        repo_or_dir="snakers4/silero-vad",
        model="silero_vad",
        trust_repo=True,
        onnx=False,
    )
    get_speech_timestamps = utils[0]
    _CACHE = _VadModel(model=model, get_ts=get_speech_timestamps)
    return _CACHE


def _load_audio_16k(audio_path: str | Path):
    """音声を 16kHz mono float32 tensor で読む（whisperx.load_audio を流用）。"""
    import torch
    import whisperx

    arr = whisperx.load_audio(str(audio_path))  # 16kHz mono float32 ndarray
    return torch.from_numpy(arr)


def speech_regions(
    audio_path: str | Path,
    *,
    threshold: float = 0.5,
    min_speech_ms: int = 150,
    min_silence_ms: int = 200,
    speech_pad_ms: int = 120,
) -> list[Interval]:
    """1トラックの発話区間を秒で返す。

    ``threshold``: silero の発話確率しきい値（0.5既定。学習モデルなので録音音量に頑健）。
    ``min_silence_ms``: これ未満の無音は区間を割らない（語間の細かい無音で過分割しない）。
    ``speech_pad_ms``: 各発話区間の前後パディング（語頭/語尾を削らない）。
    """
    vad = _load_silero()
    wav = _load_audio_16k(audio_path)
    ts = vad.get_ts(
        wav,
        vad.model,
        sampling_rate=SILERO_SR,
        threshold=threshold,
        min_speech_duration_ms=min_speech_ms,
        min_silence_duration_ms=min_silence_ms,
        speech_pad_ms=speech_pad_ms,
    )
    return [(t["start"] / SILERO_SR, t["end"] / SILERO_SR) for t in ts]


def _union(intervals: list[Interval], *, bridge_s: float = 0.0) -> list[Interval]:
    if not intervals:
        return []
    intervals = sorted(intervals)
    out: list[Interval] = [intervals[0]]
    for s, e in intervals[1:]:
        ps, pe = out[-1]
        if s - pe <= bridge_s:
            out[-1] = (ps, max(pe, e))
        else:
            out.append((s, e))
    return out


def speech_regions_multi(
    audio_paths: list[str | Path],
    *,
    bridge_s: float = 0.4,
    **kwargs,
) -> list[Interval]:
    """複数話者トラックの発話区間の和集合（誰かが喋っていれば keep）。

    ``bridge_s``: 近接 keep区間をつなぐ隙間上限（過剰分割の抑制）。
    その他 kwargs は :func:`speech_regions` に渡す。
    """
    all_iv: list[Interval] = []
    for p in audio_paths:
        all_iv.extend(speech_regions(p, **kwargs))
    return _union(all_iv, bridge_s=bridge_s)
