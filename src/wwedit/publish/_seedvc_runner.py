"""Seed-VC の**別venv側**で走る変換ランナー（wwedit を import しない）。

`seed-vc-2025/inference.py` の `main()` をそのまま使い、`load_models` を memoize して
**1プロセスで全ジョブ**を処理する（モデル読み込みは1回だけ）。

    <seed-vc venv python> _seedvc_runner.py <jobs.json> <results.json>

jobs.json = {"seedvc_dir": "...", "diffusion_steps": 30, "fp16": true,
             "jobs": [{"source": "...", "target": "<参照wav>", "out": "..."}, ...]}
results.json = [{"out": "...", "duration_sec": 12.3}, ...]

**1ジョブ完了ごとに results.json を書き直す**ので、タイムアウトや中断でも完了分は残る
（呼び出し側は out の存在で再開判定できる）。CWD は seed-vc リポジトリルートであること
（HF_HUB_CACHE が相対パスのため。wwedit 側 `seedvc.convert_batch` が cwd を渡す）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


def _duration(path: Path) -> float:
    """wav の尺(秒)。Seed-VC の出力は float32 wav なので soundfile で読む。"""
    import soundfile as sf

    info = sf.info(str(path))
    return info.frames / info.samplerate


def main() -> None:
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    res_path = Path(sys.argv[2])

    # このスクリプトのディレクトリを sys.path から外す（wwedit 側モジュールの覆い隠し防止。
    # _qwen_runner.py と同じ罠対策）。
    here = Path(__file__).resolve().parent
    sys.path[:] = [p for p in sys.path if p and Path(p).resolve() != here]

    svc = Path(spec["seedvc_dir"])
    os.chdir(svc)  # inference.py の HF_HUB_CACHE='./checkpoints/hf_cache' が相対
    sys.path.insert(0, str(svc))

    import inference  # noqa: E402  (seed-vc-2025/inference.py)

    # load_models を memoize（inference.main は毎回呼ぶが、ロードは初回だけ）
    _orig_load = inference.load_models
    _cache: dict = {}

    def _cached_load(args):  # noqa: ANN001
        if "m" not in _cache:
            _cache["m"] = _orig_load(args)
        return _cache["m"]

    inference.load_models = _cached_load

    steps = int(spec.get("diffusion_steps", 30))
    fp16 = bool(spec.get("fp16", True))
    results: list[dict] = []

    for j in spec["jobs"]:
        out = Path(j["out"])
        out.parent.mkdir(parents=True, exist_ok=True)
        # inference.main は出力名を自分で決めるので、ジョブごとの空 tmpdir に吐かせて拾う
        tmp_out = Path(tempfile.mkdtemp(prefix="svc_"))
        ns = argparse.Namespace(
            source=j["source"], target=j["target"], output=str(tmp_out),
            diffusion_steps=steps, length_adjust=1.0, inference_cfg_rate=0.7,
            f0_condition=False, auto_f0_adjust=False, semi_tone_shift=0,
            checkpoint=None, config=None, fp16=fp16,
        )
        inference.main(ns)
        produced = sorted(tmp_out.glob("*.wav"))
        if not produced:
            raise SystemExit(f"変換出力が見つからない: {j['source']}")
        shutil.move(str(produced[0]), str(out))
        shutil.rmtree(tmp_out, ignore_errors=True)

        dur = _duration(out)
        results.append({"out": str(out), "duration_sec": round(dur, 3)})
        # 逐次保存: 中断しても完了分が残る
        res_path.write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
        print(f"  {out.name} {dur:.2f}s", flush=True)


if __name__ == "__main__":
    main()
