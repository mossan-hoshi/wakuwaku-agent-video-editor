"""``wwedit transcribe`` サブコマンド。"""

from __future__ import annotations

from pathlib import Path

import typer
from rich import print as rprint

from wwedit.edl.schema import load_edl, save_edl
from wwedit.transcribe.merge import merge_speakers
from wwedit.transcribe.stt import (
    load_model,
    load_whisperx,
    transcribe_track,
    transcribe_track_whisperx,
)

transcribe_app = typer.Typer(help="文字起こし（話者別→統合）", no_args_is_help=True)


@transcribe_app.command()
def run(
    edl_path: Path = typer.Argument(..., help="対象 EDL (data/<date>/edl.json)"),
    backend: str = typer.Option(
        "whisperx", help="whisperx(本命・強制アライメント) / faster-whisper"
    ),
    model_size: str = typer.Option("large-v3", help="Whisperモデル"),
    compute_type: str = typer.Option("float16", help="float16 / int8_float16"),
    vad: bool = typer.Option(False, help="VADフィルタ(faster-whisperのみ・既定オフ)"),
    gap: float = typer.Option(1.0, help="発話を区切る無音の閾値(秒)"),
) -> None:
    """話者別 m4a を文字起こしし、統合トランスクリプトを EDL.utterances に書き込む。"""
    edl = load_edl(edl_path)
    tracks = [t for t in edl.source.audio_tracks if not t.is_desktop_audio]
    if not tracks:
        raise typer.BadParameter("話者トラックが無い")

    use_x = backend == "whisperx"
    rprint(f"[dim]{backend} {model_size}/{compute_type} をロード中...[/]")
    bundle = (
        load_whisperx(model_size, compute_type=compute_type)
        if use_x
        else load_model(model_size, compute_type=compute_type)
    )

    # 同じ話者が**複数トラックを持つことがある**（Zoomが端末/再入室ごとにトラックを分ける。
    # 例: audioTaniguchi2 / audioTaniguchi3）。上書き代入すると先のトラックが丸ごと消え、
    # その話者の発話が字幕/章から抜け落ちる（2026-08-03 で実際に踏んだ）。**必ず足し合わせる**。
    per_speaker: dict[str, list] = {}
    for t in tracks:
        rprint(f"[cyan]文字起こし[/]: {t.speaker} ({Path(t.path).name})")
        words = (
            transcribe_track_whisperx(bundle, t.path)
            if use_x
            else transcribe_track(bundle, t.path, vad_filter=vad)
        )
        if t.speaker in per_speaker:
            rprint(f"  [yellow]同じ話者の追加トラック[/]: {t.speaker} に足し合わせる")
        per_speaker.setdefault(t.speaker, []).extend(words)
        rprint(f"  → {len(words)} words")

    # 足し合わせたので時刻順に並べ直す（``words_to_utterances`` は順序を前提にする）。
    for words in per_speaker.values():
        words.sort(key=lambda w: w.start)

    edl.utterances = merge_speakers(per_speaker, gap_s=gap)
    save_edl(edl, edl_path)

    # [D] LLM入力用の単一トランスクリプト（話者ラベル付き）も書き出す
    txt_path = edl_path.parent / "transcript.txt"
    lines = [
        f"[{u.start:7.1f}-{u.end:7.1f}] {u.speaker}: {u.text}" for u in edl.utterances
    ]
    txt_path.write_text("\n".join(lines), encoding="utf-8")
    rprint(f"[green]完了[/]: {len(edl.utterances)}発話 → {txt_path}")
