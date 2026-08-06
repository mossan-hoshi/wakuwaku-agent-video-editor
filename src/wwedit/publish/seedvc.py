"""Seed-VC（声質変換）ラッパ。[V] 方式A＝元音声のタイミングを完全維持してキャラ声化する。

推論一式は別リポ `seed-vc-2025`（専用 venv・モデルDL済み）。`qwen_tts.py` と同じ
**別プロセス方式**（jobs.json/results.json、ランナー `_seedvc_runner.py` が seed-vc の
venv 側で走る）。**モデル読み込みが重いので全ジョブを1プロセスでまとめる**
（[[cache-model-forward-not-resweep]]）。ランナーは1ジョブ完了ごとに results を追記保存
するので、タイムアウト/中断しても完了分は失われない。

参照音源は happy-collapse-maker の ``refs/<char>/set*.wav``（Qwen3-TTSと同じ声）を連結して
~24秒に構築する（Seed-VC の実効上限25秒・先頭から単純カットのため先頭無音も除去）。
収録日に依存しない共有物なので ``data/_shared/voice_refs/`` にキャッシュする。

パスは `.env` で差し替え: ``WWEDIT_SEEDVC_DIR`` / ``WWEDIT_SEEDVC_PYTHON``。
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from wwedit.common.env import env_value

__all__ = ["build_char_ref", "convert_batch", "plan_ref_concat", "shared_ref_dir"]

_DEFAULTS = {
    "WWEDIT_SEEDVC_DIR": r"D:\Users\sackn\repos\seed-vc-2025",
    "WWEDIT_SEEDVC_PYTHON": r"D:\Users\sackn\repos\seed-vc-2025\.venv\Scripts\python.exe",
}

#: Seed-VC が実際に食う参照は先頭25秒（inference.py: ``ref_audio[:sr*25]``）。
#: 連結はこの目標秒に達したら打ち切る（少し短い分には問題ない）。
REF_TARGET_SEC = 24.0


def _cfg(key: str) -> str:
    return env_value(key) or _DEFAULTS[key]


def shared_ref_dir() -> Path:
    """参照音源キャッシュの置き場（収録日非依存の共有物）。

    ランナーは別CWD（seed-vcルート）で走るので**絶対パス**で返す。
    """
    return Path("data/_shared/voice_refs").resolve()


def plan_ref_concat(durations: list[float], target: float = REF_TARGET_SEC) -> int:
    """先頭から何ファイル連結すれば ``target`` 秒に達するかを返す（純関数・テスト用）。

    足しても届かなければ全ファイル。1本目で超えても最低1本は使う。
    """
    total = 0.0
    for i, d in enumerate(durations):
        total += d
        if total >= target:
            return i + 1
    return len(durations)


def build_char_ref(char: str, *, cache_dir: Path | None = None, force: bool = False) -> Path:
    """キャラの参照音源 wav（~24秒・44.1k mono）を構築する（キャッシュ済みなら再利用）。

    happy-collapse-maker ``refs/<char>/refs.json`` のセット順に wav を連結し、
    ``silenceremove`` で**先頭の無音だけ**除去する（Seed-VC は参照の先頭25秒しか見ない）。
    """
    cache = cache_dir or shared_ref_dir()
    out = cache / f"{char}_ref.wav"
    if out.exists() and not force:
        return out

    hcm = Path(env_value("WWEDIT_QWEN_TTS_DIR") or r"C:\Users\sackn\repos2\happy-collapse-maker")
    refs_json = hcm / "refs" / char / "refs.json"
    if not refs_json.exists():
        raise FileNotFoundError(f"参照セットが無いキャラ: {char}（{refs_json}）")
    sets = json.loads(refs_json.read_text(encoding="utf-8"))
    wavs = [(refs_json.parent / s["wav"], float(s.get("duration_sec", 0.0))) for s in sets]
    wavs = [(p, d) for p, d in wavs if p.exists()]
    if not wavs:
        raise FileNotFoundError(f"参照 wav が1本も無い: {refs_json.parent}")
    n = plan_ref_concat([d for _, d in wavs])
    picked = [p for p, _ in wavs[:n]]

    cache.mkdir(parents=True, exist_ok=True)
    inputs: list[str] = []
    for p in picked:
        inputs += ["-i", str(p)]
    concat_in = "".join(f"[{i}:a]" for i in range(len(picked)))
    filt = (
        f"{concat_in}concat=n={len(picked)}:v=0:a=1,"
        "silenceremove=start_periods=1:start_threshold=-40dB,"
        "aresample=44100[a]"
    )
    cmd = ["ffmpeg", "-y", *inputs, "-filter_complex", filt,
           "-map", "[a]", "-ac", "1", str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError(f"参照音源の構築失敗({char}):\n{(proc.stderr or '')[-800:]}")
    return out


def convert_batch(
    jobs: list[dict], *, diffusion_steps: int = 30, fp16: bool = True,
) -> list[dict]:
    """``[{"source","target","out"}]`` を1プロセスでまとめて変換する。

    戻り値は完了ジョブの ``[{"out", "duration_sec"}]``。ランナーが逐次追記するので、
    プロセスが途中で死んでも完了分の results は返る（呼び出し側は out の存在で再開判定）。
    """
    if not jobs:
        return []
    svc = Path(_cfg("WWEDIT_SEEDVC_DIR"))
    py = Path(_cfg("WWEDIT_SEEDVC_PYTHON"))
    if not py.exists():
        raise FileNotFoundError(f"Seed-VC の python が無い: {py}")
    if not (svc / "inference.py").exists():
        raise FileNotFoundError(f"Seed-VC の推論一式が無い: {svc}")

    work = Path(tempfile.mkdtemp())
    spec = {
        "seedvc_dir": str(svc),
        "diffusion_steps": diffusion_steps,
        "fp16": fp16,
        "jobs": [{k: str(v) for k, v in j.items()} for j in jobs],
    }
    spec_path = work / "jobs.json"
    res_path = work / "results.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")

    runner = Path(__file__).with_name("_seedvc_runner.py")
    proc = subprocess.run(
        [str(py), "-u", str(runner), str(spec_path), str(res_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(svc),  # HF_HUB_CACHE が './checkpoints/hf_cache' 相対のため必須
    )
    results: list[dict] = []
    if res_path.exists():
        results = json.loads(res_path.read_text(encoding="utf-8"))
    if proc.returncode != 0 and not results:
        tail = "\n".join((proc.stderr or proc.stdout or "").splitlines()[-15:])
        raise RuntimeError(f"Seed-VC 変換失敗:\n{tail}")
    return results
