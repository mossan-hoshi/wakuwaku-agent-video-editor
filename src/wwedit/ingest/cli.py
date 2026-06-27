"""``wwedit ingest`` サブコマンド。"""

from __future__ import annotations

from pathlib import Path

import typer
from rich import print as rprint
from rich.table import Table

from wwedit.common.media import probe
from wwedit.compose.fcpxml import keep_ranges_to_segments, read_keep_ranges
from wwedit.edl.schema import Edl, SourceMedia, load_edl, save_edl
from wwedit.ingest.normalize import normalize_folder_name
from wwedit.ingest.tracks import detect_tracks

ingest_app = typer.Typer(help="取り込み/正規化（フォルダ名・トラック判別）", no_args_is_help=True)


@ingest_app.command()
def scan(folder: Path = typer.Argument(..., help="収録フォルダのパス")) -> None:
    """フォルダのトラック構成を表示する（書き込みなし）。"""
    tracks = detect_tracks(folder)
    try:
        canonical = normalize_folder_name(Path(folder).name)
    except ValueError:
        canonical = "(日付抽出不可)"

    rprint(f"[bold]収録フォルダ[/]: {folder}")
    rprint(f"[bold]正規化名[/]: {canonical}")
    rprint(f"[bold]メイン映像[/]: {tracks.video_path}  (id={tracks.video_id})")
    rprint(f"[bold]合成音声[/]: {tracks.combined_audio_path}")

    table = Table("話者", "デスクトップ音声?", "パス")
    for t in tracks.speaker_tracks:
        table.add_row(t.speaker, "○" if t.is_desktop_audio else "", t.path)
    rprint(table)


@ingest_app.command()
def init(
    folder: Path = typer.Argument(..., help="収録フォルダのパス"),
    data_root: Path = typer.Option(Path("data"), help="作業データの出力ルート"),
) -> None:
    """正規化＋プローブを行い、初期 EDL を ``data/<YYYY-MM-DD>/edl.json`` に生成する。"""
    canonical = normalize_folder_name(Path(folder).name)
    tracks = detect_tracks(folder)
    info = probe(tracks.video_path)

    edl = Edl(
        recording_dir=str(folder),
        source=SourceMedia(
            video_path=tracks.video_path,
            fps=info.fps or 30,
            width=info.width or 1920,
            height=info.height or 1080,
            duration_s=info.duration_s,
            audio_tracks=tracks.speaker_tracks,
        ),
        meta={"canonical_date": canonical, "video_id": tracks.video_id},
    )

    out_dir = data_root / canonical
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "edl.json"
    save_edl(edl, out_path)

    rprint(f"[green]初期EDLを生成[/]: {out_path}")
    rprint(
        f"  映像 {info.width}x{info.height} @ {info.fps}fps, "
        f"{info.duration_s:.1f}s / 話者 {len(tracks.speaker_tracks)}本"
    )


@ingest_app.command("import-cuts")
def import_cuts(
    edl_path: Path = typer.Argument(..., help="対象 EDL (data/<date>/edl.json)"),
    fcpxml: Path = typer.Option(
        None, help="既存 Recut 編集の .fcpxml（省略時は収録フォルダ内を自動探索）"
    ),
) -> None:
    """既存 .fcpxml の残す区間を EDL の segments に取り込む（無音カットのベースライン）。"""
    edl = load_edl(edl_path)
    if fcpxml is None:
        rec = Path(edl.recording_dir)
        candidates = sorted(rec.glob("video*.fcpxml")) or sorted(rec.glob("*.fcpxml"))
        if not candidates:
            raise typer.BadParameter(f".fcpxml が見つからない: {rec}")
        fcpxml = candidates[0]

    ranges = read_keep_ranges(fcpxml)
    edl.segments = keep_ranges_to_segments(ranges, source_duration_s=edl.source.duration_s)
    save_edl(edl, edl_path)

    kept = [s for s in edl.segments if not s.invalid]
    cut = [s for s in edl.segments if s.invalid]
    kept_dur = sum(s.duration for s in kept)
    cut_dur = sum(s.duration for s in cut)
    rprint(f"[green]カット取り込み完了[/]: {fcpxml.name}")
    rprint(
        f"  残す {len(kept)}区間/{kept_dur:.1f}s, 無音 {len(cut)}区間/{cut_dur:.1f}s "
        f"(元尺 {edl.source.duration_s:.1f}s)"
    )
