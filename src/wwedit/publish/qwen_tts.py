"""Qwen3-TTS（ゼロショット音声クローン）で合成する。**TTSはこれが本命**（SBV2は使わない）。

推論一式は別リポ `happy-collapse-maker`（`app.synthesize` 経路・`refs/<char>/refs.json` に
参照音声と書き起こしが同梱）。**専用の venv とモデルを持つので別プロセスで叩く**
（`_qwen_runner.py` がその venv 側で走る）。

**モデル読み込みが重いので、必要な台詞は ``synth_batch`` で一度にまとめて合成する**
（章ごとに1プロセス起こすと読み込みを回数ぶん払う＝[[cache-model-forward-not-resweep]]）。

パスは `.env` で差し替えられる: ``WWEDIT_QWEN_TTS_DIR`` / ``WWEDIT_QWEN_TTS_PYTHON`` /
``WWEDIT_QWEN_TTS_MODEL`` / ``WWEDIT_QWEN_HF_HOME``。
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from wwedit.common.env import env_value
from wwedit.publish._speaker_sim import F0_MAX_OCTAVE, SIM_THRESHOLD, WIN_SIM_THRESHOLD

__all__ = ["QWEN_VOICES", "available_voices", "synth_batch", "synth_to_file",
           "SIM_THRESHOLD", "F0_MAX_OCTAVE"]

_DEFAULTS = {
    "WWEDIT_QWEN_TTS_DIR": r"C:\Users\sackn\repos2\happy-collapse-maker",
    "WWEDIT_QWEN_TTS_PYTHON": r"D:\novtube_tts\qwen3tts_poc\.venv\Scripts\python.exe",
    "WWEDIT_QWEN_TTS_MODEL": (
        r"D:\novtube_tts\qwen3tts_poc\hf\hub"
        r"\models--Qwen--Qwen3-TTS-12Hz-1.7B-Base\snapshots"
        r"\fd4b254389122332181a7c3db7f27e918eec64e3"
    ),
    "WWEDIT_QWEN_HF_HOME": r"D:\novtube_tts\qwen3tts_poc\hf",
}

#: のべつべ！キャラのうち参照音声があるもの（`mossan_hoshi` は実在の人なので除く）。
QWEN_VOICES = ["noa", "yume", "kasumi", "priya", "reika", "ritsu", "suzu", "tsukasa"]


def _cfg(key: str) -> str:
    return env_value(key) or _DEFAULTS[key]


def available_voices() -> list[str]:
    """参照音声が実在するキャラIDを返す（`QWEN_VOICES` と実ディスクの積）。"""
    refs = Path(_cfg("WWEDIT_QWEN_TTS_DIR")) / "refs"
    if not refs.is_dir():
        return []
    have = {p.parent.name for p in refs.glob("*/refs.json")}
    return [v for v in QWEN_VOICES if v in have]


def synth_batch(jobs: list[dict], *, sim_tries: int = 3,
                sim_threshold: float | None = None,
                f0_max_octave: float | None = None,
                win_sim_threshold: float | None = None,
                report: list[dict] | None = None) -> list[float]:
    """``[{"text","out","char",...}]`` を**1プロセスでまとめて合成**し、実尺(秒)を返す。

    ``char`` は `refs/<char>/` のID。``ref`` 未指定/不在ならそのキャラの先頭セットに落ちる。

    **参照音声と別人になったらシードを変えて引き直す**（``sim_tries`` 回まで・最良を採用）。
    引き直しは別プロセス側の合成ループの中でやる（モデル読み込み約280秒を再度払わないため）。
    ``report`` を渡すと1本ごとの ``{"out","sim","octave","sim_ok","tries"}`` が入る。
    """
    if not jobs:
        return []
    hcm = Path(_cfg("WWEDIT_QWEN_TTS_DIR"))
    py = Path(_cfg("WWEDIT_QWEN_TTS_PYTHON"))
    if not py.exists():
        raise FileNotFoundError(f"Qwen3-TTS の python が無い: {py}")
    if not (hcm / "app.py").exists():
        raise FileNotFoundError(f"Qwen3-TTS の推論一式が無い: {hcm}")

    work = Path(tempfile.mkdtemp())
    spec = {
        "hcm_dir": str(hcm),
        "model": _cfg("WWEDIT_QWEN_TTS_MODEL"),
        "hf_home": _cfg("WWEDIT_QWEN_HF_HOME"),
        "jobs": [{**j, "out": str(j["out"])} for j in jobs],
        "sim_tries": int(sim_tries),
        "sim_threshold": (SIM_THRESHOLD if sim_threshold is None else float(sim_threshold)),
        "f0_max_octave": (F0_MAX_OCTAVE if f0_max_octave is None else float(f0_max_octave)),
        "win_sim_threshold": (WIN_SIM_THRESHOLD if win_sim_threshold is None
                              else float(win_sim_threshold)),
    }
    spec_path = work / "jobs.json"
    res_path = work / "results.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")

    runner = Path(__file__).with_name("_qwen_runner.py")
    proc = subprocess.run(
        [str(py), "-u", str(runner), str(spec_path), str(res_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0 or not res_path.exists():
        tail = "\n".join((proc.stderr or proc.stdout or "").splitlines()[-15:])
        raise RuntimeError(f"Qwen3-TTS 合成失敗:\n{tail}")
    results = json.loads(res_path.read_text(encoding="utf-8"))
    if report is not None:
        report.extend(results)
    return [float(r["duration_sec"]) for r in results]


def synth_to_file(text: str, out_wav: str | Path, voice: str, **kw) -> float:
    """1本だけ合成する（`publish.aivis.synth_to_file` と同じ形）。実尺(秒)を返す。

    複数本要るときは ``synth_batch`` を使うこと（モデル読み込みを1回で済ませる）。
    """
    dur = synth_batch([{"text": text, "out": str(out_wav), "char": voice, **kw}])
    return dur[0] if dur else 0.0
