"""Qwen3-TTS の**別venv側**で走る合成ランナー（wwedit を import しない）。

`happy-collapse-maker` の `app.synthesize` 経路をそのまま使う（ゼロショット音声クローン）。
**モデル読み込みが重いので、渡された全ジョブを1プロセスでまとめて合成する。**

    <qwen venv python> _qwen_runner.py <jobs.json> <results.json>

jobs.json = {"hcm_dir": "...", "model": "...", "jobs": [
    {"text": "...", "out": "...", "char": "suzu", "ref": "normal_01",
     "seed": 0, "dur": 8.0}, ...],
    "sim_tries": 3, "sim_threshold": 0.85, "f0_max_octave": 0.35}
results.json = [{"out": "...", "duration_sec": 1.23, "sim": 0.97, "tries": 1}, ...]

**話者が別人になったら引き直す**（ユーザー指摘「参照音声と全くの別人になるときがある」）。
判定は `_speaker_sim.py`（numpyだけ）。**引き直しはこのプロセスの中でやる**
—— モデル読み込みが約280秒なので、外で回すと引き直しのたびにそれを払う。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _load_speaker_sim():
    """``_speaker_sim.py`` を**ファイルパスで**読み込む。

    このランナーは自分のディレクトリを ``sys.path`` から外している（隣の ``qwen_tts.py``
    が本家 ``qwen_tts`` パッケージを覆い隠すため）。普通の import は使えない。
    """
    import importlib.util

    path = Path(__file__).resolve().with_name("_speaker_sim.py")
    spec = importlib.util.spec_from_file_location("_wwedit_speaker_sim", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> None:
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out_path = Path(sys.argv[2])

    # **このスクリプトが居るディレクトリを sys.path から外す。**残っていると
    # 隣の `qwen_tts.py`（wwedit側のラッパ）が本家 `qwen_tts` パッケージを覆い隠す（踏んだ）。
    here = Path(__file__).resolve().parent
    sys.path[:] = [p for p in sys.path if p and Path(p).resolve() != here]

    hcm = Path(spec["hcm_dir"])
    sys.path.insert(0, str(hcm))
    if spec.get("hf_home"):
        os.environ.setdefault("HF_HOME", spec["hf_home"])
    os.environ.setdefault("HF_HUB_OFFLINE", "1")

    import app
    import collapse_fx as fx
    import numpy as np
    import soundfile as sf

    app._REFS = app.load_refs([str(p) for p in sorted((hcm / "refs").glob("*/refs.json"))])

    def ref_sets(char: str) -> list[str]:
        """そのキャラの参照セット名（``normal_01`` 等）を名前順で。"""
        return sorted(k.split("/", 1)[1] for k in app._REFS if k.startswith(f"{char}/"))

    def resolve_ref(char: str, ref: str) -> str:
        """指定の参照セットが無ければ、そのキャラの先頭セットへ落とす（noa は set1… 等）。"""
        if f"{char}/{ref}" in app._REFS:
            return ref
        names = [k.split("/", 1)[1] for k in app._REFS if k.startswith(f"{char}/")]
        if not names:
            raise SystemExit(f"参照セットが無いキャラ: {char}")
        return sorted(names)[0]

    device, dtype = app.pick_device("auto")
    from qwen_tts import Qwen3TTSModel

    app._MODEL = Qwen3TTSModel.from_pretrained(
        app.resolve_model(spec["model"]), device_map=device, dtype=dtype,
        attn_implementation="sdpa",
    )

    sim = _load_speaker_sim()
    tries_max = max(1, int(spec.get("sim_tries", 3)))
    thr = float(spec.get("sim_threshold", sim.SIM_THRESHOLD))
    oct_max = float(spec.get("f0_max_octave", sim.F0_MAX_OCTAVE))
    win_thr = float(spec.get("win_sim_threshold", sim.WIN_SIM_THRESHOLD))
    ref_cache: dict[str, list] = {}

    def refs_of(char: str):
        if char not in ref_cache:
            ref_cache[char] = sim.ref_embeddings(sorted((hcm / "refs" / char).glob("*.wav")))
        return ref_cache[char]

    def _score(sc: dict) -> float:
        """回どうしを比べるための1つの数。**全体と最悪窓の低いほう**。"""
        return min(float(sc.get("sim", 0.0)), float(sc.get("sim_min", 1.0)))

    results = []
    for j in spec["jobs"]:
        char = j.get("char", "suzu")
        base_seed = int(j.get("seed", 0))
        refs = refs_of(char)
        # 引き直しの手は2段。**シードを変える**（同じシードなら同じ別人が出るだけ）→
        # それでも駄目なら**別の参照セット**に替える。参照とニュアンスが違う文
        # （棒読みの参照に「まじか！」のような感情表現）はスコアが落ちやすいので、
        # 参照側を替えると当たることがある。それでも駄目な行は台本の見直しへ回す
        # （呼び出し側が `voice_tts_recheck.tsv` を書く）。
        ref_names = ref_sets(char) or [j.get("ref", "normal_01")]
        plan = [(base_seed + t * 7919, ref_names[min(t // 2, len(ref_names) - 1)])
                for t in range(tries_max)]
        best = None
        for t, (seed, rname) in enumerate(plan):
            p = app.Params(
                mode="wwedit", text=j["text"],
                ref_choice=f"{char}/{resolve_ref(char, rname)}",
                ref_file=None, ref_text="", auto_text="", do_trim=True,
                effect="none", chunk_sec=1.0, ramp_start=0.0, ramp_hard=1.0, warp=0.0,
                normal_head=False, head_lo=0.0, head_hi=0.0,
                dur_sec=float(j.get("dur", 12.0)), seed=seed,
                temperature=float(j.get("temperature", 0.9)),
                sub_temperature=float(j.get("sub_temperature", 0.9)),
            )
            wav, _log, _refs = app.synthesize(p)
            wav = np.asarray(wav, dtype=np.float32)
            sc = (sim.compare_wave(wav, fx.SR, refs, sim_threshold=thr,
                                   f0_max_octave=oct_max, win_sim_threshold=win_thr)
                  if refs else {"sim": 1.0, "ok": True, "octave": 0.0, "skipped": True,
                                "sim_min": 1.0, "sim_min_at": 0.0, "n_win": 0})
            # 良し悪しは**全体と最悪窓の低いほう**で比べる。全体だけで選ぶと、
            # 「平均は高いが出だしが別人」の回を最良として拾ってしまう。
            if best is None or _score(sc) > _score(best[1]):
                best = (wav, sc, t + 1)
            if sc["ok"]:
                break
            if t + 1 < len(plan):
                nxt = plan[t + 1]
                how = "参照を替えて" if nxt[1] != rname else "シードを変えて"
                print(f"  [別人] {Path(j['out']).name} sim={sc['sim']:.3f} "
                      f"最悪窓={sc.get('sim_min', 1.0):.3f}@{sc.get('sim_min_at', 0.0):.1f}s "
                      f"oct={sc['octave']:.2f} → {how}再合成 ({t + 2}/{tries_max})",
                      flush=True)
        wav, sc, used = best
        out = Path(j["out"])
        out.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(out), fx.normalize_peak(wav), fx.SR, subtype="PCM_16")
        # **1本ごとに台帳サイドカーを書く。**呼び出し側が途中で kill されても
        # 合成済みが再合成対象に戻らない（分割実行の途中打ち切り対策）。
        out.with_suffix(".txt").write_text(j["text"], encoding="utf-8")
        results.append({"out": str(out), "duration_sec": round(len(wav) / fx.SR, 3),
                        "sim": sc["sim"], "octave": sc.get("octave", 0.0),
                        "sim_min": sc.get("sim_min", 1.0),
                        "sim_min_at": sc.get("sim_min_at", 0.0),
                        "n_win": sc.get("n_win", 0),
                        "sim_ok": bool(sc["ok"]), "tries": used})
        flag = "" if sc["ok"] else "  <<< 引き直しても別人のまま"
        print(f"  {out.name} {results[-1]['duration_sec']:.2f}s "
              f"sim={sc['sim']:.3f} 最悪窓={sc.get('sim_min', 1.0):.3f}{flag}", flush=True)

    out_path.write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
