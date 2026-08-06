"""話者が同一人物かを**numpyだけ**で測る（参照音声 vs 合成結果）。

Qwen3-TTS のゼロショット音声クローンは、推論によって**参照と全くの別人**になることがある
（ユーザー指摘）。合成のたびにここで測り、外れたらシードを変えて引き直す。

**numpy 以外に依存しない**のは、この判定を Qwen3-TTS 側の venv（`_qwen_runner.py`）から
そのまま呼ぶため。別プロセスへ出すとモデル読み込み（約280秒）を毎回払うので、
引き直しは**同じプロセスの中**でやらないと現実的な時間に収まらない。

測るもの:

* **声道の形**（誰の声か）… 対数メルスペクトルの DCT（MFCC相当）の平均。c0（音量）は捨てる。
* **声の高さ**… 自己相関で F0 の中央値。オクターブ差は別人の強い証拠。

どちらも**有声フレームだけ**で測る。無音や息を混ぜると全員似てしまう。

**クリップ全体の平均だけでは足りない**。Qwen3-TTS は1本の中で途中から別人になることがある
（実測: #103 の idx=142「まあまあ、意味わかんないですね。あー、はいはい。…」は
**前半だけが別人**なのに、全体平均では 0.9533 で合格していた）。そこで
``window_sims`` で**窓ごと**に測り、**最悪の窓**でも判定する。
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

__all__ = [
    "SIM_THRESHOLD", "F0_MAX_OCTAVE", "MIN_CHECK_S",
    "WIN_SIM_THRESHOLD", "WIN_S", "WIN_HOP_S",
    "read_wav", "logmel", "embed", "f0_median", "cosine", "compare", "compare_wave",
    "ref_embeddings", "window_sims",
]

#: MFCC 平均のコサイン類似度がこれ未満なら「別人」。
#: #103 の176本の実測: 中央 0.96〜0.97 / 1秒以上の164本のうち 0.85未満は15本。
#: 0.85 で「明らかに違う」だけを拾える（0.9 だと1割が引っかかって引き直しが増えすぎる）。
SIM_THRESHOLD = 0.85
#: F0 中央値の差がこのオクターブ数を超えたら「別人」。実測の最悪は 0.86＝ほぼ1オクターブ下。
F0_MAX_OCTAVE = 0.35
#: これより短いクリップは判定しない（材料が足りず、相槌が延々と引き直しになる）。
MIN_CHECK_S = 0.5

#: 途中で別人になるのを見つけるための窓（秒）とホップ（秒）。
#: 1.5秒あれば MFCC 平均が安定し、「1文だけ崩れた」を1〜2窓で捉えられる。
WIN_S = 1.5
WIN_HOP_S = 0.75
#: **最悪の窓**がこれ未満なら「途中で別人になった」。窓は材料が短いぶん値が散るので
#: クリップ全体の閾値(0.85)より下に置く。
#: #103 の162本の実測: ``sim_min`` は 中央0.894 / 10%点0.768 / 5%点0.729 / 最小0.462。
#: 0.80 で約13%が引っかかる。ユーザーが耳で「別人」と指摘した idx=142 は
#: 全体 0.930（合格）に対し **最悪窓 0.731（0.00秒＝出だし）** で、ここでだけ落ちる。
#: 出だしの窓が落ちるのは無音のせいではない（idx=142 の先頭窓の rms 0.227 はクリップ最大）。
#: **Qwen3-TTS は喋り出しで参照に乗り切らないことがある**、という系統的な癖。
WIN_SIM_THRESHOLD = 0.80

_N_FFT = 1024
_HOP = 256
_N_MELS = 40
_N_MFCC = 20


def read_wav(path: str | Path) -> tuple[np.ndarray, int]:
    """wav を mono float32 で読む（16bit PCM 前提）。"""
    with wave.open(str(path), "rb") as wf:
        sr, ch = wf.getframerate(), wf.getnchannels()
        x = np.frombuffer(wf.readframes(wf.getnframes()), "<i2").astype(np.float32)
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x / 32768.0, sr


def _mel_filters(sr: int, n_fft: int = _N_FFT, n_mels: int = _N_MELS) -> np.ndarray:
    def to_mel(f):
        return 2595.0 * np.log10(1.0 + f / 700.0)

    def to_hz(m):
        return 700.0 * (10.0 ** (m / 2595.0) - 1.0)

    edges = to_hz(np.linspace(to_mel(50.0), to_mel(min(7600.0, sr / 2)), n_mels + 2))
    bins = np.floor((n_fft + 1) * edges / sr).astype(int)
    fb = np.zeros((n_mels, n_fft // 2 + 1), dtype=np.float32)
    for i in range(n_mels):
        lo, mid, hi = bins[i], bins[i + 1], bins[i + 2]
        if mid == lo:
            mid = lo + 1
        if hi <= mid:
            hi = mid + 1
        fb[i, lo:mid] = np.linspace(0, 1, mid - lo, endpoint=False)
        fb[i, mid:hi] = np.linspace(1, 0, hi - mid, endpoint=False)
    return fb


def logmel(x: np.ndarray, sr: int) -> np.ndarray:
    """``(フレーム, メル)`` の対数メルスペクトル。"""
    if len(x) < _N_FFT:
        x = np.pad(x, (0, _N_FFT - len(x)))
    win = np.hanning(_N_FFT).astype(np.float32)
    n = 1 + (len(x) - _N_FFT) // _HOP
    idx = np.arange(_N_FFT)[None, :] + _HOP * np.arange(n)[:, None]
    spec = np.abs(np.fft.rfft(x[idx] * win, axis=1)) ** 2
    return np.log(spec @ _mel_filters(sr).T + 1e-10)


def embed(x: np.ndarray, sr: int) -> np.ndarray | None:
    """有声フレームの MFCC 平均（c0 は捨てる）。有声が足りなければ None。"""
    lm = logmel(x, sr)
    if len(lm) < 4:
        return None
    energy = lm.mean(axis=1)
    keep = energy > np.percentile(energy, 60)          # 上位40%＝鳴っている所
    if keep.sum() < 4:
        return None
    k = np.arange(_N_MELS)
    dct = np.cos(np.pi / _N_MELS * (k[None, :] + 0.5) * np.arange(_N_MFCC)[:, None])
    mf = (lm[keep] @ dct.T)[:, 1:]                     # c0（音量）を捨てる
    v = mf.mean(axis=0)
    n = np.linalg.norm(v)
    return v / n if n > 0 else None


def f0_median(x: np.ndarray, sr: int, *, lo: float = 70.0, hi: float = 500.0) -> float:
    """自己相関で F0 の中央値(Hz)。有声が取れなければ 0。"""
    win = int(sr * 0.04)
    hop = int(sr * 0.02)
    lag_lo, lag_hi = int(sr / hi), int(sr / lo)
    out: list[float] = []
    for i in range(0, max(0, len(x) - win), hop):
        f = x[i:i + win]
        if float(np.sqrt((f ** 2).mean())) < 0.01:
            continue
        f = f - f.mean()
        ac = np.correlate(f, f, "full")[win - 1:]
        seg = ac[lag_lo:lag_hi]
        if len(seg) < 2 or ac[0] <= 0:
            continue
        lag = int(np.argmax(seg)) + lag_lo
        if ac[lag] / ac[0] > 0.3:                      # 周期性が弱い＝無声
            out.append(sr / lag)
    return float(np.median(out)) if out else 0.0


def cosine(a: np.ndarray | None, b: np.ndarray | None) -> float:
    if a is None or b is None:
        return 0.0
    return float(np.dot(a, b))


def ref_embeddings(ref_paths) -> list[tuple[np.ndarray, float]]:
    """参照音声の ``(埋め込み, F0中央値)`` を先に作っておく（合成のたびに読み直さない）。"""
    out = []
    for p in ref_paths:
        y, sr = read_wav(p)
        e = embed(y, sr)
        if e is not None:
            out.append((e, f0_median(y, sr)))
    return out


def _best_sim(x: np.ndarray, sr: int, refs) -> tuple[float, float]:
    """``(最も似ている参照とのコサイン, その参照のF0中央値)``。埋め込めなければ ``(0, 0)``。"""
    ev = embed(x, sr)
    best, best_f0 = 0.0, 0.0
    for e, rf0 in refs:
        s = cosine(ev, e)
        if s > best:
            best, best_f0 = s, rf0
    return best, best_f0


def window_sims(x: np.ndarray, sr: int, refs, *,
                win_s: float = WIN_S, hop_s: float = WIN_HOP_S) -> list[tuple[float, float]]:
    """``(窓の開始秒, その窓の最良コサイン)`` の並び。

    **1本の中で途中から別人になる**のを見つけるための関数。全体平均では、崩れた1文が
    残りの正常な部分に薄められて見えなくなる（実測 0.9533 で合格していた）。

    窓が1つも取れない短いクリップでは空リストを返す（呼び側は全体の値だけで判定する）。
    """
    win, hop = int(sr * win_s), int(sr * hop_s)
    if win <= 0 or hop <= 0 or len(x) < win:
        return []
    out: list[tuple[float, float]] = []
    for i in range(0, len(x) - win + 1, hop):
        s, _ = _best_sim(x[i:i + win], sr, refs)
        if s > 0.0:                                    # 有声が足りない窓は捨てる
            out.append((round(i / sr, 2), round(s, 4)))
    return out


def compare_wave(x: np.ndarray, sr: int, refs, *, sim_threshold: float = SIM_THRESHOLD,
                 f0_max_octave: float = F0_MAX_OCTAVE,
                 min_check_s: float = MIN_CHECK_S,
                 win_sim_threshold: float = WIN_SIM_THRESHOLD) -> dict:
    """合成波形を参照と比べる。``refs`` は ``ref_embeddings`` の結果。

    参照が複数あるときは**最も似ている1本**を採る（キャラに複数の参照セットがあるため）。
    短すぎるクリップは判定せず ``ok=True``（材料が足りず相槌が延々と引き直しになる）。

    判定は**2段**。クリップ全体（``sim``）と、**最悪の窓**（``sim_min``）の両方が
    閾値を超えて初めて合格。後者が無いと「途中の1文だけ別人」を取りこぼす。
    """
    if len(x) / max(1, sr) < min_check_s:
        return {"sim": 1.0, "f0_syn": 0.0, "f0_ref": 0.0, "octave": 0.0,
                "sim_min": 1.0, "sim_min_at": 0.0, "n_win": 0,
                "ok": True, "skipped": True}
    best, best_f0 = _best_sim(x, sr, refs)
    f0 = f0_median(x, sr)
    oct_diff = abs(np.log2(f0 / best_f0)) if f0 > 0 and best_f0 > 0 else 0.0
    wins = window_sims(x, sr, refs)
    at, worst = min(wins, key=lambda w: w[1]) if wins else (0.0, best)
    return {
        "sim": round(best, 4),
        "f0_syn": round(f0, 1),
        "f0_ref": round(best_f0, 1),
        "octave": round(float(oct_diff), 3),
        "sim_min": round(worst, 4),
        "sim_min_at": at,
        "n_win": len(wins),
        "ok": bool(best >= sim_threshold and oct_diff <= f0_max_octave
                   and worst >= win_sim_threshold),
        "skipped": False,
    }


def compare(syn_path: str | Path, ref_paths, **kw) -> dict:
    """``compare_wave`` のファイル版（既存の合成結果を後から監査するとき用）。"""
    x, sr = read_wav(syn_path)
    return compare_wave(x, sr, ref_embeddings(ref_paths), **kw)
