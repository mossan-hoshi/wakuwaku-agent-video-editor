"""``wwedit drp`` サブコマンド — Resolve 最終編集(.drp)の検査と正解抽出。"""

from __future__ import annotations

from pathlib import Path

import typer
from rich import print as rprint
from rich.table import Table

from wwedit.compose.fcpxml import read_keep_ranges
from wwedit.drp.reader import DEFAULT_DRP, final_timeline_for_day, read_timelines

drp_app = typer.Typer(help="Resolve最終編集(.drp)の検査・正解抽出", no_args_is_help=True)


@drp_app.command("list")
def list_timelines(
    drp: Path = typer.Option(DEFAULT_DRP, help=".drp パス"),
    with_video_only: bool = typer.Option(True, help="映像クリップを持つタイムラインのみ"),
) -> None:
    """`.drp` 内のタイムラインを日付ごとに一覧表示。"""
    timelines = read_timelines(drp)
    table = Table("日付", "タイムライン", "映像clip", "音声clip")
    rows = 0
    for t in sorted(timelines, key=lambda x: (x.primary_day or "", x.uuid)):
        if with_video_only and not t.video_clips:
            continue
        table.add_row(
            t.primary_day or "?",
            t.uuid[:8],
            str(len(t.video_clips)),
            str(len(t.clips) - len(t.video_clips)),
        )
        rows += 1
    rprint(table)
    rprint(f"[dim]{rows} タイムライン（映像あり）[/]")


@drp_app.command()
def cuts(
    day: str = typer.Argument(..., help="収録日 YYYY-MM-DD"),
    drp: Path = typer.Option(DEFAULT_DRP, help=".drp パス"),
    fcpxml: Path = typer.Option(None, help="比較する Recut fcpxml（省略時は突合なし）"),
) -> None:
    """指定日の最終編集タイムラインの規模を表示し、Recut fcpxml と比較する。

    .drp の Start/Duration は **出力（カット後）タイムライン**で、Recut の offset/duration と
    同じ意味。クリップ数・総尺が一致すれば「映像カットは Recut のまま（意味的カット無し）」、
    差があれば最終編集で映像のカットが追加/変更されたことを示す。
    """
    tl = final_timeline_for_day(day, drp)
    if tl is None:
        rprint(f"[yellow]{day} の映像タイムラインが .drp に無い[/]")
        raise typer.Exit(1)
    vclips = tl.video_clips
    final_n = len(vclips)
    final_dur = sum(c.duration_s for c in vclips)
    rprint(
        f"[green]{day} 最終編集[/]: 映像{final_n}クリップ / 出力尺{final_dur:.1f}s "
        f"(音声{len(tl.clips) - final_n}クリップ)"
    )

    if fcpxml:
        keep = read_keep_ranges(fcpxml)
        recut_n = len(keep)
        recut_dur = sum(r.duration for r in keep)
        same = final_n == recut_n and abs(final_dur - recut_dur) < 0.5
        verdict = (
            "一致 → 映像カットは Recut のまま（意味的カット無し）"
            if same
            else "差あり → 最終編集で映像カットが追加/変更された"
        )
        rprint(
            f"[cyan]Recut比較[/]: Recut={recut_n}クリップ/{recut_dur:.1f}s, "
            f"最終={final_n}クリップ/{final_dur:.1f}s → {verdict}"
        )
