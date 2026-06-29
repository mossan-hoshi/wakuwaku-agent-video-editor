"""``wwedit cut`` サブコマンド（STT駆動の無音/フィラーカット）。"""

from __future__ import annotations

from pathlib import Path

import typer
from rich import print as rprint

from wwedit.cut.autocut import (
    build_segments,
    mark_filler_intervals,
    mark_fillers_from_utterances,
    segments_from_keep,
)
from wwedit.edl.schema import load_edl, save_edl
from wwedit.eval.golden import (
    interval_total,
    removed_silence_from_fcpxml,
    score_cuts,
)

cut_app = typer.Typer(help="無音/フィラー検出（VAD駆動・カット候補算出）", no_args_is_help=True)


def _report_compare(edl, cut_intervals: list[tuple[float, float]]) -> None:
    """カット候補を Recut fcpxml と突合して指標を表示。"""
    rec = Path(edl.recording_dir)
    cands = sorted(rec.glob("video*.fcpxml")) or sorted(rec.glob("*.fcpxml"))
    if not cands:
        rprint("[yellow]突合用fcpxmlなし[/]")
        return
    gt = removed_silence_from_fcpxml(cands[0], edl.source.duration_s)
    m = score_cuts(cut_intervals, gt)
    rprint(
        f"[cyan]Recut突合[/]: recall={m['recall']:.1%}(Recutカットの再現), "
        f"precision={m['precision']:.1%}(過剰カットの少なさ), IoU={m['iou']:.1%} "
        f"/ Recut除去={interval_total(gt):.1f}s vs 自動={m['pred_s']:.1f}s"
    )


@cut_app.command()
def auto(
    edl_path: Path = typer.Argument(..., help="対象 EDL（transcribe 済み）"),
    pad: float = typer.Option(0.15, help="語の前後マージン(秒)。語頭/語尾を削らない"),
    bridge: float = typer.Option(0.4, help="keep区間を繋ぐ隙間の上限(秒)"),
    no_fillers: bool = typer.Option(False, help="フィラーカットを無効化"),
    compare_fcpxml: bool = typer.Option(True, help="Recut fcpxml と突合"),
) -> None:
    """STT の発話から keep/無音/フィラーを算出し EDL.segments に書き込む。"""
    edl = load_edl(edl_path)
    if not edl.utterances:
        raise typer.BadParameter("先に transcribe を実行してください（utterances が空）")

    edl.segments = build_segments(edl, pad_s=pad, bridge_s=bridge, cut_fillers=not no_fillers)
    save_edl(edl, edl_path)

    kept = [s for s in edl.segments if not s.invalid]
    cut = [s for s in edl.segments if s.invalid]
    fillers = [s for s in cut if s.reason == "filler"]
    kept_s = sum(s.duration for s in kept)
    cut_s = sum(s.duration for s in cut)
    rprint(
        f"[green]自動カット[/]: 残す{len(kept)}区間/{kept_s:.1f}s, "
        f"カット{len(cut)}区間/{cut_s:.1f}s (うちフィラー{len(fillers)})"
    )

    if compare_fcpxml:
        rec = Path(edl.recording_dir)
        cands = sorted(rec.glob("video*.fcpxml")) or sorted(rec.glob("*.fcpxml"))
        if not cands:
            rprint("[yellow]突合用fcpxmlなし[/]")
            return
        gt = removed_silence_from_fcpxml(cands[0], edl.source.duration_s)
        pred = [(s.start, s.end) for s in cut]
        m = score_cuts(pred, gt)
        rprint(
            f"[cyan]Recut突合[/]: recall={m['recall']:.1%}(Recutカットの再現), "
            f"precision={m['precision']:.1%}(過剰カットの少なさ), IoU={m['iou']:.1%} "
            f"/ Recut除去={interval_total(gt):.1f}s vs 自動={m['pred_s']:.1f}s"
        )
        rprint(
            "[dim]※ Recutは意味的カットも含むため完全一致は出ない。"
            "padを増やすとprecision↑recall↓、減らすと逆。[/]"
        )


@cut_app.command("auto-vad")
def auto_vad(
    edl_path: Path = typer.Argument(..., help="対象 EDL（ingest 済み・話者トラックが必要）"),
    threshold: float = typer.Option(0.5, help="silero発話確率しきい値(録音音量に頑健)"),
    min_silence_ms: int = typer.Option(200, help="これ未満の無音は切らない(語間で過分割しない)"),
    speech_pad_ms: int = typer.Option(80, help="発話区間の前後パディング(語頭/語尾を削らない)"),
    bridge: float = typer.Option(0.4, help="近接keep区間を繋ぐ隙間上限(秒)"),
    fillers: bool = typer.Option(False, help="正規表現フィラーも切る(既定はLLM版を別途使う)"),
    refine: bool = typer.Option(True, help="切れ目を音量の谷へスナップ(ぶつ切り回避)"),
    compare_fcpxml: bool = typer.Option(True, help="Recut fcpxml と突合"),
) -> None:
    """silero VAD で無音をカットし EDL.segments に書き込む（音量の谷へスナップ）。

    フィラーは意味判断が要るため別工程（fillers-prepare→LLM→fillers-apply）が本命。
    ``--fillers`` で簡易な正規表現版も併用できる。
    """
    from wwedit.cut.vad import speech_regions_multi

    edl = load_edl(edl_path)
    tracks = [t.path for t in edl.source.audio_tracks if not t.is_desktop_audio]
    if not tracks:
        raise typer.BadParameter("話者トラックが無い（先に ingest を実行）")

    rprint(f"[dim]silero VAD: {len(tracks)}トラック処理中...[/]")
    keep = speech_regions_multi(
        tracks,
        threshold=threshold,
        min_silence_ms=min_silence_ms,
        speech_pad_ms=speech_pad_ms,
        bridge_s=bridge,
    )
    segments = segments_from_keep(keep, edl.source.duration_s)
    if fillers and edl.utterances:
        segments = mark_fillers_from_utterances(segments, edl.utterances)
    if refine:
        segments = _refine_with_energy(segments, edl)
    edl.segments = segments
    save_edl(edl, edl_path)
    _report_cut(edl)
    if compare_fcpxml:
        cut = [(s.start, s.end) for s in edl.segments if s.invalid]
        _report_compare(edl, cut)


def _refine_with_energy(segments, edl):
    """映像音声の音量エンベロープで切れ目を谷へスナップ（ぶつ切り回避）。"""
    from wwedit.cut.energy import load_envelope, refine_segments

    rprint("[dim]音量エンベロープで切れ目を調整中...[/]")
    env = load_envelope(edl.source.video_path)
    return refine_segments(segments, env)


def _report_cut(edl) -> None:
    cut = [s for s in edl.segments if s.invalid]
    kept = [s for s in edl.segments if not s.invalid]
    sil = [s for s in cut if s.reason == "silence"]
    fil = [s for s in cut if s.reason == "filler"]
    rprint(
        f"[green]カット[/]: 残す{len(kept)}/{sum(s.duration for s in kept):.1f}s, "
        f"無音{len(sil)}/{sum(s.duration for s in sil):.1f}s, "
        f"フィラー{len(fil)}/{sum(s.duration for s in fil):.1f}s"
    )


@cut_app.command("fillers-prepare")
def fillers_prepare(
    edl_path: Path = typer.Argument(..., help="対象 EDL（transcribe 済み）"),
) -> None:
    """フィラー候補を抽出し、LLM(filler-selector)用の候補TSVと対応マップを書き出す。"""
    from wwedit.cut.filler_llm import write_candidate_files

    edl = load_edl(edl_path)
    if not edl.utterances:
        raise typer.BadParameter("utterances が空（先に transcribe を実行）")
    tsv, mp = write_candidate_files(edl, edl_path.parent)
    import json

    n = len(json.loads(mp.read_text(encoding="utf-8")))
    dec = edl_path.parent / "filler_decisions.json"
    rprint(f"[green]候補{n}件[/]: {tsv}\n  → filler-selector スキルで {dec} を作成")


@cut_app.command("fillers-apply")
def fillers_apply(
    edl_path: Path = typer.Argument(..., help="対象 EDL（auto-vad 済みで無音segmentsがある）"),
    decisions: Path = typer.Option(None, help="LLM決定JSON（既定 data/<date>/ 内）"),
    refine: bool = typer.Option(True, help="切れ目を音量の谷へスナップ"),
    compare_fcpxml: bool = typer.Option(True, help="Recut fcpxml と突合"),
) -> None:
    """LLMが選んだフィラー区間を無音segmentsに重ね、音量の谷へスナップする。"""
    from wwedit.cut.filler_llm import load_decisions_to_intervals

    edl = load_edl(edl_path)
    if not edl.segments:
        raise typer.BadParameter("segments が空（先に cut auto-vad を実行）")
    dec = decisions or (edl_path.parent / "filler_decisions.json")
    mp = edl_path.parent / "filler_map.json"
    if not dec.exists() or not mp.exists():
        raise typer.BadParameter(f"決定/マップが無い: {dec} / {mp}（fillers-prepare→LLM が必要）")

    intervals = load_decisions_to_intervals(mp, dec)
    segments = mark_filler_intervals(edl.segments, intervals)
    if refine:
        segments = _refine_with_energy(segments, edl)
    edl.segments = segments
    save_edl(edl, edl_path)
    rprint(f"[dim]LLM選択フィラー {len(intervals)}件を適用[/]")
    _report_cut(edl)
    if compare_fcpxml:
        cut = [(s.start, s.end) for s in edl.segments if s.invalid]
        _report_compare(edl, cut)


@cut_app.command("ngwords")
def ngwords(
    edl_path: Path = typer.Argument(..., help="対象 EDL（transcribe 済み）"),
    refine: bool = typer.Option(True, help="切れ目を音量の谷へスナップ"),
) -> None:
    """NGワード(.env の WWEDIT_CUT_NGWORDS)に言及した発話をまるごとカットする。

    語は PII 同様 .env のみ（コード/リポジトリ非埋め込み）。未設定なら何もしない。
    どの語に当たったかは秘匿のため出力せず、当たった発話数のみ報告する。
    """
    from wwedit.cut.ngwords import apply_ngword_cuts, load_ngwords

    edl = load_edl(edl_path)
    if not edl.utterances:
        raise typer.BadParameter("utterances が空（先に transcribe を実行）")
    terms = load_ngwords()
    if not terms:
        rprint("[yellow]NGワード未設定（.env の WWEDIT_CUT_NGWORDS）。何もしません。[/]")
        return
    segments, n_matched = apply_ngword_cuts(edl, terms)
    if n_matched == 0:
        rprint(f"[dim]NGワード {len(terms)}語: 該当発話なし。[/]")
        return
    if refine:
        segments = _refine_with_energy(segments, edl)
    edl.segments = segments
    save_edl(edl, edl_path)
    rprint(f"[green]NGワードカット[/]: {len(terms)}語で {n_matched} 発話をカット対象に。")
    _report_cut(edl)
