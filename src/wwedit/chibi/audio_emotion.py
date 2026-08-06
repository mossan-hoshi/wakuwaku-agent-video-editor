"""[E] **元の収録マイク音声**から感情を測る（emotion2vec+ を別プロセスで1回だけ回す）。

## なぜ音声を見るのか

これまでは**ターンの先頭テキストだけ**で判定していたので、「なるほど」に surprised が付く、
明らかに驚いている所が normal のまま、といった破綻が出ていた（ユーザー指摘）。
声の高さ・強さ・立ち上がりは**テキストに出ない**ので、波形を見ないと当たらない。

## なぜ「元の」音声なのか

判定に使うのは**収録のマイク音声**であって、Qwen3-TTS の合成結果ではない。
合成音は基本的に棒読みなので、そこから感情は取れない（ユーザー指摘）。

## 推論は1回だけ

モデル読み込みが重いので全区間を1プロセスでまとめて推論し、結果を JSON に残す。
閾値の調整は**後処理だけ**でやること（[[cache-model-forward-not-resweep]]）。

## 環境

``funasr`` は ``torch`` を巻き込むので **wwedit の venv には入れない**。
``.env`` の ``WWEDIT_EMOTION2VEC_PYTHON`` に専用 venv の python を指す
（未設定なら wwedit の python で試み、無ければ分かるエラーを出す）。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from wwedit.common.env import env_value
from wwedit.compose.speedup import merge_spans
from wwedit.edl.schema import ChibiEmotion, Edl, voiced_word_spans

__all__ = [
    "AUDIO_EMOTION_JSON", "EMOTION_FROM_AUDIO", "MIN_SPAN_S", "MIN_SCORE",
    "audio_spans", "decode_for_analysis", "analyze_spans", "to_chibi_emotion",
    "load_audio_emotions",
]

AUDIO_EMOTION_JSON = "chibi_audio_emotion.json"
#: 判定する最短の発話区間（これ未満は材料不足）。
MIN_SPAN_S = 0.6
#: 隣り合う有声区間をこの隙間まで繋ぐ。``voiced_word_spans`` は word を
#: 「文字数×0.22秒」で切り詰めるので、そのままだと 0.4秒級の細切れになって
#: 感情を測る材料が足りない。文節の切れ目でぶつ切りにしない。
MERGE_GAP_S = 0.6
#: これ未満のスコアは「はっきりしていない」として normal 扱い。
MIN_SCORE = 0.5

#: emotion2vec+ の9クラス → ちびキャラの6感情。
#: ``thinking`` は**音に対応物が無い**のでここには現れない（テキスト側=LLMで拾う）。
EMOTION_FROM_AUDIO: dict[str, ChibiEmotion] = {
    "angry": "angry",
    "happy": "smile",
    "surprised": "surprised",
    "sad": "troubled",
    "fearful": "troubled",
    "disgusted": "troubled",
    "neutral": "normal",
    "other": "normal",
    "unknown": "normal",
    # モデルは 9番目のクラスを ``<unk>`` という綴りで返す（``unknown`` ではない）。
    # 表に無いと既定の normal に落ちるので実害は無いが、明示しておく。
    "<unk>": "normal",
}


def audio_spans(edl: Edl, *, min_span: float = MIN_SPAN_S,
                merge_gap: float = MERGE_GAP_S) -> list[dict]:
    """判定対象の発話区間（**素材秒**）を話者ごとに集める。

    区間は ``voiced_word_spans``（word タイミング由来の有声区間）を使い回す。
    utterance まるごとだと相槌をまたぐ数十秒の塊になり、塊の頭で1回しか判定できない
    ＝これが「なるほど」に surprised が付いていた原因。
    """
    wav = {t.speaker: (t.path if not t.voice_path else t.path)
           for t in edl.source.audio_tracks if not t.is_desktop_audio}
    out: list[dict] = []
    for i, u in enumerate(edl.utterances):
        if u.speaker not in wav:
            continue
        # word タイミングが無い発話（別経路で作った EDL）は発話まるごとを1区間にする
        spans = merge_spans(voiced_word_spans(u.words), gap=merge_gap) \
            or [(u.start, u.end)]
        for k, (a, b) in enumerate(spans):
            if b - a < min_span:
                continue
            out.append({"key": f"{i}:{k}", "utt": i, "speaker": u.speaker,
                        "wav": wav[u.speaker], "start": round(a, 3), "end": round(b, 3)})
    return out


#: 判定用にデコードした音声の置き場（素材の隣ではなく EDL の隣に置く）。
DECODED_DIR = "emotion_wav"


def decode_for_analysis(items: list[dict], work: Path) -> list[dict]:
    """区間の参照先を **16kHz mono の wav** に差し替える（元は m4a のことが多い）。

    推論側（別 venv）は依存を増やさないため標準の ``wave`` で読む。**m4a は読めない**
    ので、ここで話者トラックごとに一度だけデコードして使い回す。16kHz mono は
    emotion2vec の入力そのものなので、リサンプルの手間も無くなる。
    """
    from wwedit.common.media import ffmpeg_path

    work.mkdir(parents=True, exist_ok=True)
    cache: dict[str, str] = {}
    for it in items:
        src = str(it["wav"])
        if src not in cache:
            dst = work / f"{Path(src).stem}.16k.wav"
            if not dst.exists():
                proc = subprocess.run(
                    [ffmpeg_path(), "-y", "-v", "error", "-i", src,
                     "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst)],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace")
                if proc.returncode != 0 or not dst.exists():
                    raise RuntimeError(f"判定用の音声デコードに失敗: {src}\n"
                                       f"{(proc.stderr or '')[-500:]}")
            cache[src] = str(dst)
        it["wav"] = cache[src]
    return items


def analyze_spans(items: list[dict], out_json: Path, *, model: str = "",
                  device: str = "cuda", python: str = "") -> Path:
    """全区間をまとめて推論し、``out_json`` に ``[{key,labels,top,score}]`` を書く。"""
    items = decode_for_analysis(items, out_json.parent / DECODED_DIR)
    py = python or env_value("WWEDIT_EMOTION2VEC_PYTHON") or sys.executable
    mdl = model or env_value("WWEDIT_EMOTION2VEC_MODEL") or "iic/emotion2vec_plus_large"
    work = Path(tempfile.mkdtemp())
    spec = work / "spec.json"
    res = work / "res.json"
    spec.write_text(json.dumps({"model": mdl, "device": device, "items": items},
                               ensure_ascii=False), encoding="utf-8")
    runner = Path(__file__).with_name("_emotion2vec_runner.py")
    proc = subprocess.run([py, "-u", str(runner), str(spec), str(res)],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode != 0 or not res.exists():
        tail = "\n".join((proc.stderr or proc.stdout or "").splitlines()[-15:])
        raise RuntimeError(
            "emotion2vec の推論に失敗:\n" + tail +
            "\n（funasr は torch を巻き込むので専用 venv に入れ、"
            ".env の WWEDIT_EMOTION2VEC_PYTHON に指すこと）")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(res.read_text(encoding="utf-8"), encoding="utf-8")
    return out_json


def to_chibi_emotion(row: dict, *, min_score: float = MIN_SCORE) -> ChibiEmotion | None:
    """1区間の推論結果 → ちび感情。はっきりしない/normal なら None。"""
    if not row or float(row.get("score") or 0.0) < min_score:
        return None
    e = EMOTION_FROM_AUDIO.get(str(row.get("top") or ""), "normal")
    return None if e == "normal" else e


def load_audio_emotions(path: Path, *, min_score: float = MIN_SCORE) -> dict[str, dict]:
    """結果JSON → ``{key: row}``。無ければ空（音声判定なしでも動く）。"""
    if not path.exists():
        return {}
    return {str(r["key"]): r for r in json.loads(path.read_text(encoding="utf-8"))}
