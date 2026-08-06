"""``wwedit chibi`` サブコマンド（[V] ゆっくり風ちびキャラ）。"""

from __future__ import annotations

from pathlib import Path

import typer
from rich import print as rprint

from wwedit.edl.schema import load_edl, save_edl

chibi_app = typer.Typer(help="ゆっくり風ちびキャラ（アセット/感情/口パク）", no_args_is_help=True)
emotions_app = typer.Typer(help="感情割当（chibi-emotion-assigner スキルの入出力）",
                           no_args_is_help=True)
chibi_app.add_typer(emotions_app, name="emotions")


@chibi_app.command(name="base")
def base_cmd(
    char: str = typer.Argument(..., help="キャラID（noa/suzu/...）"),
    force: bool = typer.Option(False, "--force", help="既存の背景抜き結果を作り直す"),
) -> None:
    """ベースちび画像を novtube から取り込み背景抜きする（課金なし）。"""
    from wwedit.chibi.assets import ensure_base

    try:
        p = ensure_base(char, force=force)
    except FileNotFoundError as e:
        rprint(f"[red]{e}[/]")
        raise typer.Exit(1) from e
    rprint(f"[green]ベース[/]: {p}")


@chibi_app.command(name="gen")
def gen_cmd(
    char: str = typer.Argument(..., help="キャラID"),
    emotion: str = typer.Argument(
        ..., help="感情（normal/smile/surprised/troubled/angry/thinking）"
    ),
    model: str = typer.Option(None, help="画像モデル（既定=nano banana 2 lite。"
                                         "精度不足なら gemini-3-pro-image）"),
    yes: bool = typer.Option(False, "--yes", help="承認ゲートをスキップ（承認済みの時のみ）"),
    force: bool = typer.Option(False, "--force", help="既存を作り直す（1枚勝負の明示リテイク）"),
    redraw_closed: bool = typer.Option(
        False, "--redraw-closed",
        help="normal の口閉じもAIに描かせる（ベースの口が笑い口で口パクに合わないキャラ用）"),
) -> None:
    """感情×口閉/口開ペアを生成する（**課金**・承認ゲートあり・1枚勝負）。"""
    from wwedit.chibi.assets import (
        CHIBI_EMOTIONS,
        DEFAULT_CHIBI_MODEL,
        check_pair_alignment,
        chibi_emotion_prompt,
        generate_mouth_image,
        mouth_pair_paths,
    )

    if emotion not in CHIBI_EMOTIONS:
        raise typer.BadParameter(f"感情は {'/'.join(CHIBI_EMOTIONS)} のいずれか")
    model = model or DEFAULT_CHIBI_MODEL
    closed_p, open_p = mouth_pair_paths(char, emotion)
    todo = [m for m, p in (("closed", closed_p), ("open", open_p))
            if force or not p.exists()]
    paid = [m for m in todo
            if not (m == "closed" and emotion == "normal" and not redraw_closed)]
    if todo:
        rprint(f"[cyan]生成対象[/]: {char}/{emotion} → {', '.join(todo)}"
               f"（課金 {len(paid)} 枚・model={model}）")
        for m in todo:
            rprint(f"[dim]--- prompt ({m}) ---\n{chibi_emotion_prompt(char, emotion, m)}[/]")
        if not yes and not typer.confirm("生成しますか?（課金が発生します）"):
            raise typer.Exit(1)
        for m in todo:
            try:
                p = generate_mouth_image(char, emotion, m, model=model, force=force,
                                         reuse_base=not redraw_closed)
            except (FileExistsError, FileNotFoundError, RuntimeError) as e:
                rprint(f"[red]{e}[/]")
                raise typer.Exit(1) from e
            rprint(f"  {m}: {p}")
    if closed_p.exists() and open_p.exists():
        drift = check_pair_alignment(closed_p, open_p)
        flag = "[yellow]位置ドリフト大（顔が泳ぐ可能性）[/]" if drift > 0.05 else "OK"
        rprint(f"  整合: 口以外の差分率 {drift:.3f} {flag}")


@chibi_app.command(name="ensure")
def ensure_cmd(
    edl_path: Path = typer.Argument(..., help="対象 EDL（voice-cast/感情割当 済み）"),
    yes: bool = typer.Option(False, "--yes", help="承認ゲートをスキップ"),
    model: str = typer.Option(None, help="画像モデル（既定=nano banana 2 lite）"),
) -> None:
    """EDLに必要なちびアセット（キャラ×使用感情）の不足分を列挙→承認→一括生成する。"""
    from wwedit.chibi.assets import DEFAULT_CHIBI_MODEL, missing_assets

    edl = load_edl(edl_path)
    if not edl.character_cast:
        raise typer.BadParameter("character_cast が無い（先に publish voice-cast）")
    chars = sorted(set(edl.character_cast.values()))
    emotions = sorted({u.emotion for u in edl.utterances if u.emotion} | {"normal"})
    missing = missing_assets(chars, emotions)
    if not missing:
        rprint(f"[green]アセットは揃っている[/]（{'/'.join(chars)} × {'/'.join(emotions)}）")
        return
    paid = [m for m in missing if m[2] in ("closed", "open")
            and not (m[2] == "closed" and m[1] == "normal")]
    rprint(f"[cyan]不足アセット[/] {len(missing)} 件（うち課金 {len(paid)} 枚・"
           f"model={model or DEFAULT_CHIBI_MODEL}）:")
    for c, e, w in missing:
        rprint(f"  {c}/{e or '-'}: {w}")
    if not yes and not typer.confirm("生成しますか?（課金が発生します）"):
        raise typer.Exit(1)
    # base → gen(closed/open) の順で埋める
    done_pairs: set[tuple[str, str]] = set()
    for c, e, w in missing:
        if w == "base":
            from wwedit.chibi.assets import ensure_base

            ensure_base(c)
            rprint(f"  base: {c} OK")
        elif w in ("closed", "open") and (c, e) not in done_pairs:
            gen_cmd(char=c, emotion=e, model=model, yes=True, force=False,
                    redraw_closed=False)
            done_pairs.add((c, e))
    rprint("[green]chibi ensure 完了[/]")


@chibi_app.command(name="preview")
def preview_cmd(
    edl_path: Path = typer.Argument(..., help="対象 EDL（voice-cast/感情/アセット 済み）"),
    seconds: float = typer.Option(30.0, help="出力の先頭からこの秒数だけレンダする"),
    out: Path = typer.Option(None, help="出力mp4（既定 data/<date>/chibi_preview.mp4）"),
) -> None:
    """ちびキャラ付きの短いプレビューをレンダする（口パク同期・サイズ感の目視用）。"""
    from wwedit.compose.ffmpeg_compose import compose_kept
    from wwedit.edl.schema import TimeRange

    edl = load_edl(edl_path)
    keep = edl.kept_ranges()
    if not keep:
        raise typer.BadParameter("keep区間が無い")
    # 出力の先頭 seconds 分に相当する keep 区間を切り出す
    sel: list[TimeRange] = []
    acc = 0.0
    for r in keep:
        if acc + r.duration >= seconds:
            sel.append(TimeRange(start=r.start, end=r.start + (seconds - acc)))
            break
        sel.append(r)
        acc += r.duration
    out_path = out or (edl_path.parent / "chibi_preview.mp4")
    rprint(f"[dim]プレビュー合成中[/]: 先頭{seconds:.0f}s → {out_path}")
    result = compose_kept(
        edl, out_path, ranges=sel, framed=bool(edl.framing), subtitles=bool(edl.subtitles),
        chibi=True, data_dir=edl_path.parent,
    )
    rprint(f"[green]プレビュー完了[/]: {result}")


@emotions_app.command(name="audio")
def emotions_audio(
    edl_path: Path = typer.Argument(..., help="対象 EDL（transcribe 済み）"),
    device: str = typer.Option("cuda", help="推論デバイス"),
    refresh: bool = typer.Option(False, help="既存の結果を捨てて測り直す"),
) -> None:
    """[E] **元の収録マイク音声**を emotion2vec+ に掛けて、有声区間ごとの感情を測る。

    重いのは一度だけ。結果は ``chibi_audio_emotion.json`` に残り、閾値の調整は後処理で行う。
    """
    from wwedit.chibi.audio_emotion import AUDIO_EMOTION_JSON, analyze_spans, audio_spans

    edl = load_edl(edl_path)
    out = edl_path.parent / AUDIO_EMOTION_JSON
    if out.exists() and not refresh:
        rprint(f"[yellow]既にある[/]: {out}（測り直すなら --refresh）")
        return
    items = audio_spans(edl)
    if not items:
        rprint("[red]判定できる発話区間がありません[/]")
        raise typer.Exit(1)
    rprint(f"[dim]感情を測定中[/]: {len(items)}区間（元の収録音声・合成音ではない）...")
    analyze_spans(items, out, device=device)
    rprint(f"[green]音声の感情[/]: {out}")
    rprint("[dim]次: chibi emotions prepare（この結果を手がかりに付ける）[/]")


@emotions_app.command(name="prepare")
def emotions_prepare(
    edl_path: Path = typer.Argument(..., help="対象 EDL（transcribe/cut 済み）"),
) -> None:
    """発話TSVを書き出す（→ chibi-emotion-assigner スキルで感情を割当てる）。

    ``chibi_audio_emotion.json`` があれば **audio 列**として手がかりに入る。
    """
    from wwedit.chibi.audio_emotion import AUDIO_EMOTION_JSON
    from wwedit.chibi.emotion import EMOTION_TSV, write_emotion_input

    edl = load_edl(edl_path)
    tsv = edl_path.parent / EMOTION_TSV
    audio = edl_path.parent / AUDIO_EMOTION_JSON
    n = write_emotion_input(edl, tsv, audio_json=audio if audio.exists() else None)
    hint = "音声判定つき" if audio.exists() else "テキストのみ（先に chibi emotions audio）"
    rprint(f"[green]感情入力[/]: {tsv}（{n}区間・{hint}）")
    rprint("[dim]次: chibi-emotion-assigner スキル → chibi emotions apply[/]")


@emotions_app.command(name="apply")
def emotions_apply(
    edl_path: Path = typer.Argument(..., help="対象 EDL"),
    decisions: Path = typer.Option(
        None, help="決定JSON（既定 data/<date>/chibi_emotion_decisions.json）"
    ),
) -> None:
    """決定JSONを EDL へ適用する（key形式なら ``emotion_cues``＝時刻付きキュー）。"""
    from wwedit.chibi.emotion import EMOTION_DECISIONS, apply_emotion_decisions

    edl = load_edl(edl_path)
    dec = decisions or (edl_path.parent / EMOTION_DECISIONS)
    if not dec.exists():
        raise typer.BadParameter(f"{dec} が無い（先に chibi-emotion-assigner スキル）")
    n = apply_emotion_decisions(edl, dec)
    save_edl(edl, edl_path)
    rprint(f"[green]感情適用[/]: {n}件（未割当=normal）"
           f"{f' / キュー{len(edl.emotion_cues)}件' if edl.emotion_cues else ''}")
