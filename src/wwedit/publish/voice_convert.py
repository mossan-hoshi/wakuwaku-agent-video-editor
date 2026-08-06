"""[V] 声差し替えの共通部品（方式A: Seed-VC / 方式B: TTS で共用）。

- ``speech_spans``: 発話区間だけを変換対象にする（無音は変換しない＝1時間収録でも実変換は
  発話分のみ。Seed-VC の無音幻覚も回避）。
- ``extract_span_wav`` / ``assemble_track``: 元トラックからの切り出しと、変換済みチャンクを
  **元の位置に元の尺で**戻す組み立て（atrim/apad でサンプル誤差を元尺に強制＝タイミング維持）。
- ``normalize_voice_wav``: 組み立てた**合成声トラックを収録音と同じ基準へ正規化**する
  （:data:`VOICE_LUFS`）。Seed-VC も Qwen3-TTS も出力レベルが揃わないので、ここで揃える。
- manifest（``data/<date>/voice/manifest.json``）が分割実行・再開の SSOT。チャンクの完了判定は
  変換済み wav の存在（[[background-tasks-get-reaped]] 対策で ``--max-chunks`` ずつ前景実行）。
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

from wwedit.edl.schema import (
    VOICE_SEC_PER_CHAR,
    Edl,
    TimeRange,
    Utterance,
    voiced_word_spans,
)

__all__ = [
    "speech_spans", "extract_span_wav", "assemble_track",
    "build_manifest", "load_manifest", "pending_chunks",
    "VOICE_LUFS", "VOICE_TP_DB", "VOICE_LRA",
    "measure_loudness", "loudnorm_filter", "normalize_voice_wav",
]

# [V] 合成声トラックの正規化目標。**収録音の整音と同じ値**にする
# （compose の ``LOUDNORM = loudnorm=I=-16:TP=-1.5:LRA=11``）。合成の出力レベルは
# 話者ごとにばらつく（実測 2026-08-06: TTS が mossan -16.00 / Taniguchi -18.64 LUFS、
# Seed-VC は -16.39 / -18.81 かつ **True Peak が +0.38dB でクリップ**していた）。
VOICE_LUFS = -16.0
VOICE_TP_DB = -1.5
VOICE_LRA = 11.0


def _split_long(utt: Utterance, max_len: float) -> list[tuple[float, float]]:
    """max_len 超の1発話を語間ギャップで分割する（語情報が無ければ等分）。"""
    if utt.end - utt.start <= max_len:
        return [(utt.start, utt.end)]
    if not utt.words:
        n = int((utt.end - utt.start) // max_len) + 1
        step = (utt.end - utt.start) / n
        return [(utt.start + i * step, utt.start + (i + 1) * step) for i in range(n)]
    pieces: list[tuple[float, float]] = []
    piece_start = utt.start
    prev_end = utt.words[0].end
    for w in utt.words[1:]:
        if w.end - piece_start > max_len:
            # この語を入れると超える → 直前の語間で切る
            pieces.append((piece_start, prev_end))
            piece_start = w.start
        prev_end = w.end
    pieces.append((piece_start, utt.end))
    return pieces


def speech_spans(
    edl: Edl, speaker: str, *,
    pad: float = 0.5, merge_gap: float = 1.5, max_len: float = 45.0,
) -> list[TimeRange]:
    """話者の発話区間（ソース秒・±pad・近接マージ・max_len 分割済み）を返す。

    区間は utterance の start/end ではなく **word から起こした有声区間**（
    :func:`voiced_word_spans`）を元にする。utterance は相槌をまたぐ数十秒の塊なので、
    その範囲をそのまま変換すると 6割以上が無音になり、GPU時間を浪費する。
    ``merge_gap`` 未満の間はそのまま残すので、自然な息継ぎは保たれる。

    判定は音量ではなく文字起こしなので、**小さい声でも文字起こしされていれば残る**。
    語の打ち切りも ``VOICE_SEC_PER_CHAR``（口パク用より大幅に緩い）を使い、
    ゆっくりした発話を切らない側に倒している。
    """
    utts = sorted((u for u in edl.utterances if u.speaker == speaker), key=lambda u: u.start)
    if not utts:
        return []
    total = edl.source.duration_s or None
    raw: list[tuple[float, float]] = []
    for u in utts:
        voiced = (voiced_word_spans(u.words, max_sec_per_char=VOICE_SEC_PER_CHAR)
                  if u.words else [])
        if not voiced:
            raw.extend(_split_long(u, max_len))
            continue
        # 有声区間を merge_gap 未満の間で連結してから max_len で割る
        merged: list[list[float]] = []
        for s, e in voiced:
            if merged and s - merged[-1][1] < merge_gap:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        for s, e in merged:
            if e - s <= max_len:
                raw.append((s, e))
                continue
            n = int((e - s) // max_len) + 1
            step = (e - s) / n
            raw.extend((s + i * step, s + (i + 1) * step) for i in range(n))
    spans: list[list[float]] = []
    for s, e in raw:
        s2 = max(0.0, s - pad)
        e2 = min(e + pad, total) if total else e + pad
        if e2 <= s2:
            continue
        if spans and s2 - spans[-1][1] < merge_gap and e2 - spans[-1][0] <= max_len:
            spans[-1][1] = max(spans[-1][1], e2)
        else:
            spans.append([s2, e2])
    return [TimeRange(start=s, end=e) for s, e in spans]


def _run_ffmpeg(cmd: list[str], what: str) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"{what} 失敗:\n{(proc.stderr or '')[-800:]}")


_LOUDNORM_JSON_RE = re.compile(r"\{[^{}]*input_i[^{}]*\}", re.S)


_MEASURED_KEYS = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")


def measure_loudness(path: str | Path) -> dict[str, float]:
    """wav の ITU-R BS.1770 ラウドネスを測る（``loudnorm`` の1パス目）。

    **ゲート付き**なので、トラックの大半が無音でも「喋っている間の音量」が出る
    （合成声トラックは無音が半分以上を占める）。返り値をそのまま
    :func:`loudnorm_filter` に渡すと2パス目になる。
    """
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-i", str(path),
         "-af", f"loudnorm=I={VOICE_LUFS:g}:TP={VOICE_TP_DB:g}:LRA={VOICE_LRA:g}"
                ":print_format=json",
         "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    m = _LOUDNORM_JSON_RE.findall(proc.stderr or "")
    if not m:
        raise RuntimeError(f"ラウドネス測定 失敗 {Path(path).name}:\n{(proc.stderr or '')[-800:]}")
    raw = json.loads(m[-1])
    out: dict[str, float] = {}
    for k in _MEASURED_KEYS:
        try:
            out[k] = float(raw.get(k))
        except (TypeError, ValueError):        # 無音だと "-inf" が返る
            out[k] = -99.0 if k != "target_offset" else 0.0
    return out


def loudnorm_filter(
    measured: dict[str, float],
    *, target_lufs: float = VOICE_LUFS, target_tp: float = VOICE_TP_DB,
    lra: float = VOICE_LRA,
) -> str:
    """1パス目の実測値を渡す ``loudnorm`` の2パス目（**収録音と同じ整音**）。

    一定ゲインでは目標に届かない。合成声は True Peak がほぼ 0dBFS なのに integrated が
    -16〜-19 LUFS で、素の音量調整だとクリップが先に来て頭打ちになる（実測 2026-08-06:
    Taniguchi は +2.64dB 要るのに TP 制約で -0.67dB しか動かせなかった）。収録音が通っている
    のと同じ ``loudnorm`` なら、リミッタ込みで目標へ届く。``linear=true`` なので**可能なら
    一定ゲイン**で済ませ、無理なときだけ ffmpeg が動的モードへ落ちる。
    """
    return (
        f"loudnorm=I={target_lufs:g}:TP={target_tp:g}:LRA={lra:g}"
        f":measured_I={measured['input_i']:.2f}"
        f":measured_TP={measured['input_tp']:.2f}"
        f":measured_LRA={measured['input_lra']:.2f}"
        f":measured_thresh={measured['input_thresh']:.2f}"
        f":offset={measured.get('target_offset', 0.0):.2f}"
        f":linear=true"
    )


def normalize_voice_wav(
    path: str | Path,
    *, target_lufs: float = VOICE_LUFS, target_tp: float = VOICE_TP_DB,
    lra: float = VOICE_LRA, sr: int = 48000,
) -> dict[str, float]:
    """合成声の全長トラックを収録音と同じ基準へ**その場で**正規化する（2パス loudnorm）。

    返り値は ``{"before", "after", "tp"}``。無音トラック（実質 -70 LUFS 以下）は触らない。
    """
    path = Path(path)
    before = measure_loudness(path)
    if before["input_i"] <= -70.0:
        return {"before": before["input_i"], "after": before["input_i"], "tp": before["input_tp"]}
    tmp = path.with_name(path.stem + ".norm.wav")
    _run_ffmpeg(
        ["ffmpeg", "-y", "-i", str(path),
         "-af", loudnorm_filter(before, target_lufs=target_lufs, target_tp=target_tp, lra=lra),
         "-ar", str(sr), "-ac", "1", str(tmp)],
        f"声トラック正規化 {path.name}",
    )
    path.unlink()
    tmp.rename(path)
    after = measure_loudness(path)
    return {"before": before["input_i"], "after": after["input_i"], "tp": after["input_tp"]}


def extract_span_wav(track_path: str | Path, span: TimeRange, out_wav: str | Path,
                     *, sr: int = 44100) -> Path:
    """元トラックから区間を mono wav で切り出す（変換のソース）。"""
    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(
        ["ffmpeg", "-y", "-i", str(track_path),
         "-ss", f"{span.start:.3f}", "-t", f"{span.duration:.3f}",
         "-ac", "1", "-ar", str(sr), str(out_wav)],
        f"チャンク切り出し {out_wav.name}",
    )
    return out_wav


def assemble_track(
    placements: list[tuple], total_dur_s: float,
    out_wav: str | Path, *, sr: int = 48000, normalize: bool = False,
) -> Path:
    """変換済みチャンクを元の位置に配置した全長トラックを作る。

    ``placements`` = [(開始秒, wav, 尺秒)] または [(開始秒, wav, 尺秒, クリップ内オフセット秒)]。
    各チャンクは ``atrim/apad`` で**指定尺に強制**（変換でサンプル数が僅かに変わっても位置が
    ずれない）。オフセット付きは wav の途中から切り出して置く（[V] 方式Bのカット穴跨ぎ分割）。
    チャンク間は無音。

    ``normalize=True`` で :func:`normalize_voice_wav` を掛ける。**合成した声トラックには
    必ず付ける**（方式A/B とも）。PCシステム音のトラックには付けない＝共有された音楽の
    ダイナミクスと相対音量をそのまま残すため。
    """
    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    if not placements:
        _run_ffmpeg(
            ["ffmpeg", "-y", "-f", "lavfi", "-i",
             f"anullsrc=r={sr}:cl=mono:d={total_dur_s:.3f}", str(out_wav)],
            "無音トラック生成",
        )
        return out_wav

    cmd = ["ffmpeg", "-y"]
    lines: list[str] = []
    labels: list[str] = []
    for i, p in enumerate(placements):
        start, wav, dur = p[0], p[1], p[2]
        off = p[3] if len(p) > 3 else 0.0
        cmd += ["-i", str(wav)]
        delay_ms = int(round(start * 1000))
        lines.append(
            f"[{i}:a]aresample={sr},aformat=channel_layouts=mono,"
            f"atrim={off:.3f}:{off + dur:.3f},asetpts=PTS-STARTPTS,"
            f"apad=whole_dur={dur:.3f},"
            f"adelay={delay_ms}:all=1[c{i}]"
        )
        labels.append(f"[c{i}]")
    lines.append(
        f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:duration=longest,"
        f"apad=whole_dur={total_dur_s:.3f},atrim=0:{total_dur_s:.3f}[out]"
    )
    script = Path(tempfile.mkdtemp()) / "assemble.txt"
    script.write_text(";\n".join(lines), encoding="utf-8")
    cmd += ["-filter_complex_script", str(script), "-map", "[out]",
            "-ar", str(sr), "-ac", "1", str(out_wav)]
    _run_ffmpeg(cmd, f"トラック組み立て {out_wav.name}")
    if normalize:
        normalize_voice_wav(out_wav, sr=sr)
    return out_wav


# ---- manifest（分割実行・再開の SSOT）----

def build_manifest(
    edl: Edl, *, method: str, work_dir: Path,
    pad: float = 0.5, merge_gap: float = 1.5, max_len: float = 45.0,
) -> dict:
    """話者ごとの発話チャンク一覧を作り、ソース wav を切り出して manifest を返す。

    チャンクはマイクトラック単位（話者に複数マイクトラックがあれば各々変換して合算）。
    完了判定は ``out`` の存在なので manifest 自体に状態は持たない。
    """
    # ランナーは別CWD（seed-vcルート）で走るので、manifest のパスは**絶対パス**で持つ
    work_dir = work_dir.resolve()
    chunks: list[dict] = []
    src_dir = work_dir / "chunks"
    for ti, track in enumerate(edl.source.audio_tracks):
        if track.is_desktop_audio:
            continue
        spans = speech_spans(edl, track.speaker, pad=pad, merge_gap=merge_gap, max_len=max_len)
        for si, span in enumerate(spans):
            cid = f"t{ti}_{si:04d}"
            src = src_dir / f"{cid}.wav"
            extract_span_wav(track.path, span, src)
            chunks.append({
                "id": cid, "track_index": ti, "speaker": track.speaker,
                "start": round(span.start, 3), "end": round(span.end, 3),
                "src": str(src), "out": str(work_dir / "converted" / f"{cid}.wav"),
            })
    manifest = {
        "method": method,
        "cast": dict(edl.character_cast),
        "params": {"pad": pad, "merge_gap": merge_gap, "max_len": max_len},
        "chunks": chunks,
    }
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def load_manifest(work_dir: Path) -> dict | None:
    p = work_dir / "manifest.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def pending_chunks(manifest: dict) -> list[dict]:
    """未変換のチャンク（変換済み wav が無いもの）。"""
    return [c for c in manifest["chunks"] if not Path(c["out"]).exists()]
