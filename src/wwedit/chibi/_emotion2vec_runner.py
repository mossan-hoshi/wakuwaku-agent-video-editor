"""emotion2vec+ を**別プロセス**で回して発話区間ごとの感情スコアを出す。

    <python> _emotion2vec_runner.py <spec.json> <out.json>

spec.json = {"model": "iic/emotion2vec_plus_large", "device": "cuda",
             "items": [{"key": "...", "wav": "...", "start": 1.2, "end": 3.4}, ...]}
out.json  = [{"key": "...", "labels": {"angry": 0.1, ...}, "top": "happy", "score": 0.7}]

**モデル読み込みが重いので全区間を1プロセスでまとめて推論する**
（[[cache-model-forward-not-resweep]]）。結果は呼び出し側が JSON に残し、
閾値の調整は**後処理だけ**でやる。閾値を変えるたびに推論し直してはいけない。

wwedit を import しない（FunASR 側の venv でも動くようにするため）。
"""

from __future__ import annotations

import json
import sys
import wave
from pathlib import Path

#: emotion2vec+ の9クラス（ラベルは "<中国語>/<英語>" 形式で返るので英語側を採る）。
CLASSES = ("angry", "disgusted", "fearful", "happy", "neutral", "other",
           "sad", "surprised", "unknown")

#: FunASR の既定は ModelScope（阿里雲）から落とすが、**国際回線が細く実測 0.4MB/s**
#: しか出ない（1.2GB のモデルに27分）。同じ重みが Hugging Face にあり、そちらは
#: 実測 45MB/s＝**27秒**で済むので、既知のモデルは HF へ寄せる。
HF_MIRROR = {
    "iic/emotion2vec_plus_large": "emotion2vec/emotion2vec_plus_large",
    "iic/emotion2vec_plus_base": "emotion2vec/emotion2vec_plus_base",
    "iic/emotion2vec_plus_seed": "emotion2vec/emotion2vec_plus_seed",
    "iic/emotion2vec_base": "emotion2vec/emotion2vec_base",
}


def resolve_model(name: str) -> str:
    """モデル名をローカルのディレクトリへ解決する（落ちていなければ HF から取る）。

    ローカルパスならそのまま。HF に対応が無い名前は FunASR（＝ModelScope）に任せる。
    """
    if Path(name).exists():
        return str(Path(name))
    repo = HF_MIRROR.get(name)
    if not repo:
        return name
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        return name
    try:
        return snapshot_download(
            repo, allow_patterns=["*.pt", "*.yaml", "*.json", "tokens.txt"])
    except Exception as e:                       # 落とせなければ ModelScope へ戻す
        print(f"  HF から取れなかったので ModelScope に任せる: {e}", flush=True)
        return name


def _read_span(path: str, start: float, end: float):
    import numpy as np

    with wave.open(path, "rb") as wf:
        sr, ch, n = wf.getframerate(), wf.getnchannels(), wf.getnframes()
        a = max(0, int(start * sr))
        b = min(n, int(end * sr))
        if b <= a:
            return np.zeros(1, dtype="float32"), sr
        wf.setpos(a)
        raw = wf.readframes(b - a)
    x = np.frombuffer(raw, "<i2").astype("float32") / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, sr


def main() -> None:
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out_path = Path(sys.argv[2])
    import numpy as np
    from funasr import AutoModel

    mdl = resolve_model(spec.get("model", "iic/emotion2vec_plus_large"))
    print(f"  モデル: {mdl}", flush=True)
    model = AutoModel(model=mdl, device=spec.get("device", "cuda"),
                      disable_update=True)
    results = []
    for i, it in enumerate(spec["items"]):
        x, sr = _read_span(it["wav"], float(it["start"]), float(it["end"]))
        if sr != 16000:                       # emotion2vec は 16kHz 前提
            idx = (np.arange(int(len(x) * 16000 / sr)) * sr / 16000).astype(int)
            x = x[np.clip(idx, 0, len(x) - 1)]
        if len(x) < 1600:                     # 0.1秒未満は判定しない
            results.append({"key": it["key"], "labels": {}, "top": "", "score": 0.0})
            continue
        r = model.generate(x, granularity="utterance", extract_embedding=False)[0]
        labels = {str(k).split("/")[-1]: float(v)
                  for k, v in zip(r["labels"], r["scores"], strict=False)}
        top = max(labels, key=labels.get) if labels else ""
        results.append({"key": it["key"], "labels": labels, "top": top,
                        "score": round(labels.get(top, 0.0), 4)})
        if (i + 1) % 50 == 0:
            print(f"  {i + 1}/{len(spec['items'])}", flush=True)
    out_path.write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
