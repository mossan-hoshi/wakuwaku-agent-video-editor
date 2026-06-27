"""word単位文字起こし（フィラー保持志向）。

2つのバックエンド:
- ``faster-whisper``: Whisper本体の word_timestamps（軽い。境界はやや粗い）。
- ``whisperx``: 上記でテキスト化→**日本語wav2vec2で強制アライメント**し word境界を
  文字〜文節レベルに精緻化（短無音の検出に効く）。SDD確定の本命。

どちらも返り値は ``list[Word]`` で揃える（後段 merge/cut は不変）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "Word",
    "FILLER_PROMPT",
    "WAV2VEC2_JA",
    "load_model",
    "transcribe_track",
    "load_whisperx",
    "transcribe_track_whisperx",
]

# フィラーを落とさせないための initial_prompt（OpenAI公式もprompt活用を案内）
FILLER_PROMPT = "えーと、あのー、んー、まあ、そのー、なんか、うーん、ええ。"

# 日本語の強制アライナ（SDD確定: 1.27GB, model_name明示）
WAV2VEC2_JA = "jonatasgrosman/wav2vec2-large-xlsr-53-japanese"


@dataclass
class Word:
    text: str
    start: float
    end: float


# ── faster-whisper バックエンド ─────────────────────────────────────
def load_model(
    model_size: str = "large-v3",
    device: str = "cuda",
    compute_type: str = "float16",
):
    """WhisperModel をロード。float16 が載らなければ int8_float16 にフォールバック。"""
    from faster_whisper import WhisperModel

    try:
        return WhisperModel(model_size, device=device, compute_type=compute_type)
    except Exception:
        return WhisperModel(model_size, device=device, compute_type="int8_float16")


def transcribe_track(
    model,
    audio_path: str | Path,
    *,
    language: str = "ja",
    filler_prompt: str | None = FILLER_PROMPT,
    vad_filter: bool = False,
    beam_size: int = 5,
) -> list[Word]:
    """1トラックを word単位で文字起こしする（faster-whisper）。

    フィラー保持のため ``vad_filter=False``（短く弱いフィラーがVADに消されやすい）、
    ``initial_prompt`` にフィラー例文、``condition_on_previous_text=False``（幻覚抑制）を既定。
    """
    kwargs = dict(
        beam_size=beam_size,
        word_timestamps=True,
        language=language,
        vad_filter=vad_filter,
        condition_on_previous_text=False,
    )
    if filler_prompt:
        kwargs["initial_prompt"] = filler_prompt

    segments, _info = model.transcribe(str(audio_path), **kwargs)
    return [
        Word(text=w.word, start=w.start, end=w.end)
        for seg in segments
        for w in seg.words
    ]


# ── WhisperX バックエンド（強制アライメント）────────────────────────
@dataclass
class WhisperxBundle:
    """WhisperX の ASRモデル＋日本語アライナをまとめて保持。"""

    asr: object
    align_model: object
    align_meta: object
    device: str
    language: str


def load_whisperx(
    model_size: str = "large-v3",
    device: str = "cuda",
    compute_type: str = "float16",
    *,
    language: str = "ja",
    filler_prompt: str | None = FILLER_PROMPT,
    align_model_name: str = WAV2VEC2_JA,
    beam_size: int = 5,
    vad_method: str = "silero",
) -> WhisperxBundle:
    """WhisperX の ASR本体＋日本語wav2vec2アライナをロードする。

    話者分離(pyannote diarization)は使わない（話者別トラック＝話者なので不要・VRAM節約）。
    VADは ``silero`` 既定（pyannote VADは lightning が inspect.stack 経由で speechbrain の
    LazyModule→k2(Windows導入困難) を誤起動し落ちるため回避）。
    float16 が載らなければ int8_float16 にフォールバック。
    """
    import torch
    import whisperx

    asr_options = {
        "condition_on_previous_text": False,
        "word_timestamps": True,
        "beam_size": beam_size,
    }
    if filler_prompt:
        asr_options["initial_prompt"] = filler_prompt

    def _load(ct: str):
        return whisperx.load_model(
            model_size,
            device=device,
            compute_type=ct,
            language=language,
            asr_options=asr_options,
            vad_method=vad_method,
        )

    # torch>=2.6 は torch.load の weights_only 既定がTrueになり、pyannote VADモデルの
    # ロードが弾かれる。自前DLの信頼済みモデルなので、ロード中だけ weights_only=False に。
    _orig_load = torch.load

    def _patched_load(*a, **k):
        # lightning は weights_only=True を明示指定するため setdefault では不可。強制上書き。
        k["weights_only"] = False
        return _orig_load(*a, **k)

    torch.load = _patched_load
    try:
        try:
            asr = _load(compute_type)
        except Exception:
            asr = _load("int8_float16")

        align_model, align_meta = whisperx.load_align_model(
            language_code=language, device=device, model_name=align_model_name
        )
    finally:
        torch.load = _orig_load
    return WhisperxBundle(
        asr=asr,
        align_model=align_model,
        align_meta=align_meta,
        device=device,
        language=language,
    )


def transcribe_track_whisperx(
    bundle: WhisperxBundle,
    audio_path: str | Path,
    *,
    batch_size: int = 8,
) -> list[Word]:
    """1トラックを WhisperX で文字起こし→強制アライメントし word列を返す。

    日本語は文字レベルで整列されるため、整列に失敗した(start/endがNaN/欠落)語は捨てる。
    """
    import math

    import whisperx

    audio = whisperx.load_audio(str(audio_path))
    result = bundle.asr.transcribe(audio, batch_size=batch_size, language=bundle.language)
    aligned = whisperx.align(
        result["segments"],
        bundle.align_model,
        bundle.align_meta,
        audio,
        bundle.device,
        return_char_alignments=False,
    )

    words: list[Word] = []
    for seg in aligned.get("segments", []):
        for w in seg.get("words", []):
            text = (w.get("word") or "").strip()
            s = w.get("start")
            e = w.get("end")
            if not text or s is None or e is None:
                continue
            if isinstance(s, float) and math.isnan(s):
                continue
            if isinstance(e, float) and math.isnan(e):
                continue
            if e > s:
                words.append(Word(text=text, start=float(s), end=float(e)))
    return words
