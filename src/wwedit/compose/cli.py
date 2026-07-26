"""``wwedit compose`` サブコマンド（EDL→mp4 合成 / fcpxml 書き出し）。"""

from __future__ import annotations

from pathlib import Path

import typer
from rich import print as rprint

from wwedit.compose.fcpxml import write_fcpxml
from wwedit.compose.ffmpeg_compose import compose_audio_kept, compose_kept
from wwedit.edl.schema import load_edl

compose_app = typer.Typer(help="合成（EDL→mp4 / EDL→fcpxml）", no_args_is_help=True)


@compose_app.command()
def video(
    edl_path: Path = typer.Argument(..., help="対象 EDL（segments が必要）"),
    out: Path = typer.Option(None, help="出力mp4（既定 data/<date>/cut_preview.mp4）"),
    crf: int = typer.Option(20, help="x264 CRF（小さいほど高画質）"),
    preset: str = typer.Option("medium", help="x264 preset"),
    audio: str = typer.Option("speakers", help="speakers=話者別整音 / embedded=映像内蔵音声"),
    framed: bool = typer.Option(False, help="EDL.framing の bbox で crop+scale を適用([E]反映)"),
    subtitles: bool = typer.Option(False, help="EDL.subtitles を二重枠で焼き込む([I])"),
    bgm: str = typer.Option("", help="本編BGM。ファイル or フォルダ(同ジャンル連続)。空=無し"),
    bgm_gain_db: float = typer.Option(-20.0, help="本編BGM素材への相対ゲイン(dB・目標未指定時)"),
    bgm_target_lufs: float = typer.Option(
        -34.0,
        help="本編BGMの最終ラウドネス(LUFS)。声-16の下にカフェBGM並みに敷く。"
        "既定-34。0以上を渡すと無効化し --bgm-gain-db を使う",
    ),
    max_ranges: int = typer.Option(0, help="先頭N区間だけ合成（0=全部・動作確認用）"),
    post_unit_index: int = typer.Option(
        -1, help="投稿単位[K]。0始まりのindexでその単位の区間だけ合成（-1=収録まるごと）"),
    eyecatch: bool = typer.Option(
        False, help="[H] 各チャプター冒頭に2秒アイキャッチ(generative art＋キャラの一言)を挿入"),
    eyecatch_voice: bool = typer.Option(
        True, help="アイキャッチの音＝のべつべ!キャラの一言(SBV2・章ごとにランダム＋右上に名前)"),
    eyecatch_jingle_dir: Path = typer.Option(
        None, help="音声合成できない時に使う音楽ジングル群（退避用・章ごとに seed で選曲）"),
    chapter_ribbon: bool = typer.Option(
        False, help="左上に収録日＋章名の2段リボンを常時表示（章ごとに話者色で色分け）"),
    overlays: bool = typer.Option(
        True, help="編集ツールで置いたオーバーレイ(画像/テキスト)を最上位に焼き込む"),
) -> None:
    """EDL の keep区間を連結した mp4 を書き出す（無音カット適用済み）。

    audio=speakers では映像音声をミュートし、話者別m4aをミックス＋ラウドネス正規化([F])。
    framed で各区間にフレーミング(メイン領域へのcrop+拡大)を反映する。
    `--post-unit-index N` で1収録の N 番目の投稿だけを出力（字幕/framing/BGMも単位内に整合）。
    """
    edl = load_edl(edl_path)
    keep = edl.kept_ranges()
    if not keep:
        raise typer.BadParameter("keep区間が無い（先に cut auto-vad を実行）")
    sel_ranges = None
    if post_unit_index >= 0:
        from wwedit.edl.postunit import n_post_units, post_unit_ranges

        nu = n_post_units(edl)
        if post_unit_index >= nu:
            raise typer.BadParameter(f"post-unit-index {post_unit_index} 範囲外（投稿単位{nu}件）")
        sel_ranges = post_unit_ranges(edl, post_unit_index)
        if not sel_ranges:
            raise typer.BadParameter(f"投稿単位 {post_unit_index} に keep 区間が無い")
    if framed and not edl.framing:
        raise typer.BadParameter("framing が空（先に framing scenes/assign を実行）")
    if subtitles and not edl.subtitles:
        # 未生成なら発話から自動生成（保存はしない・このレンダリング限り）
        from wwedit.subtitle.build import subtitles_from_utterances

        edl.subtitles = subtitles_from_utterances(edl)
        if not edl.subtitles:
            raise typer.BadParameter("字幕にできる utterances がありません")

    bgm_path: str | list[str] | None = None
    bgm_label = "なし"
    if bgm:
        p = Path(bgm)
        if p.is_dir():
            # フォルダ指定＝同ジャンル**全曲を決定的順で連続再生**（1曲ループにしない）
            from wwedit.bgm.select import list_bgms, order_bgms

            tracks = order_bgms(list_bgms(p), key=edl.recording_dir or "main")
            if not tracks:
                raise typer.BadParameter(f"BGMフォルダに正規曲が無い: {bgm}")
            bgm_path = [str(t) for t in tracks]
            bgm_label = f"{p.name}/全{len(tracks)}曲連続({Path(bgm_path[0]).stem}…)"
        elif p.is_file():
            bgm_path = str(p)
            bgm_label = p.name
        else:
            raise typer.BadParameter(f"BGMが見つからない: {bgm}")

    default_name = (
        f"cut_preview_p{post_unit_index}.mp4" if post_unit_index >= 0 else "cut_preview.mp4")
    out_path = out or (edl_path.parent / default_name)
    n = max_ranges or None
    eff = sel_ranges if sel_ranges is not None else keep
    eff = eff[:n] if n else eff
    kept_s = sum(r.duration for r in eff)
    # LUFS は負値。0以上は「目標無効＝相対ゲイン(bgm_gain_db)を使う」センチネル。
    target = bgm_target_lufs if bgm_target_lufs < 0 else None
    pu = f" post-unit={post_unit_index}" if post_unit_index >= 0 else ""
    rprint(
        f"[dim]合成中[/]: {len(eff)}区間/{kept_s:.1f}s{pu} "
        f"audio={audio} framed={framed} subtitles={subtitles} "
        f"bgm={bgm_label}@{f'{target:g}LUFS' if target is not None else f'{bgm_gain_db:g}dB'}"
        f" → {out_path}"
    )
    ribbon_date = ""
    if chapter_ribbon:
        from wwedit.compose.chapter_ribbon import format_rec_date

        ribbon_date = format_rec_date(edl.recording_dir or edl_path.parent.name)
    result = compose_kept(
        edl, out_path, crf=crf, preset=preset, audio=audio,
        framed=framed, subtitles=subtitles, bgm=bgm_path, bgm_gain_db=bgm_gain_db,
        bgm_target_lufs=target, max_ranges=n, ranges=sel_ranges,
        chapter_ribbon=chapter_ribbon, ribbon_date=ribbon_date, overlays=overlays,
    )
    size_mb = result.stat().st_size / 1e6
    rprint(f"[green]合成完了[/]: {result} ({size_mb:.1f}MB)")

    if eyecatch:
        if not edl.chapters:
            rprint("[yellow]chapters が無いのでアイキャッチを挿入できません（先に chapter）[/]")
        else:
            from wwedit.compose.eyecatch_insert import insert_eyecatches

            ec_out = result.with_name(result.stem + "_ec.mp4")
            snd = "キャラの一言(SBV2)" if eyecatch_voice else f"ジングル={eyecatch_jingle_dir}"
            rprint(f"[dim]アイキャッチ挿入中（全章冒頭・音={snd}）...[/]")
            try:
                ec_path, ch_lines = insert_eyecatches(
                    result, edl, ec_out, ranges=sel_ranges, voice=eyecatch_voice,
                    jingle_dir=eyecatch_jingle_dir, crf=crf, preset=preset,
                )
            except (RuntimeError, ValueError) as e:
                rprint(f"[red]アイキャッチ挿入失敗[/]: {e}")
                raise typer.Exit(1) from e
            # 補正済みチャプター行（概要欄はこれを --chapter で渡す）
            cl_path = ec_path.with_name(ec_path.stem + "_chapters.txt")
            cl_path.write_text("\n".join(ch_lines) + "\n", encoding="utf-8")
            ec_mb = ec_path.stat().st_size / 1e6
            rprint(f"[green]アイキャッチ挿入完了[/]: {ec_path} ({ec_mb:.1f}MB・{len(ch_lines)}章)\n"
                   f"  補正チャプター → {cl_path}（概要欄はこの時刻を使う）")


@compose_app.command()
def audio(
    edl_path: Path = typer.Argument(..., help="対象 EDL（segments が必要）"),
    out: Path = typer.Option(None, help="出力音声（既定 data/<date>/cut_audio.m4a）"),
    source: str = typer.Option("video", help="video=映像内蔵音声 / speakers=話者別整音"),
) -> None:
    """EDL の keep区間を連結した音声を書き出す（カット試聴用）。"""
    edl = load_edl(edl_path)
    keep = edl.kept_ranges()
    if not keep:
        raise typer.BadParameter("keep区間が無い（先に cut auto-vad を実行）")
    out_path = out or (edl_path.parent / "cut_audio.m4a")
    rprint(f"[dim]音声合成[/]: {len(keep)}区間/{sum(r.duration for r in keep):.1f}s "
           f"source={source} → {out_path}")
    result = compose_audio_kept(edl, out_path, source=source)
    rprint(f"[green]完了[/]: {result} ({result.stat().st_size/1e6:.1f}MB)")


@compose_app.command()
def fcpxml(
    edl_path: Path = typer.Argument(..., help="対象 EDL（segments が必要）"),
    out: Path = typer.Option(None, help="出力fcpxml（既定 data/<date>/edit.fcpxml）"),
) -> None:
    """EDL のカットタイムラインを .fcpxml に書き出す（Resolve手修正用）。"""
    edl = load_edl(edl_path)
    if not edl.kept_ranges():
        raise typer.BadParameter("keep区間が無い（先に cut auto-vad を実行）")
    out_path = out or (edl_path.parent / "edit.fcpxml")
    write_fcpxml(edl, out_path)
    rprint(f"[green]fcpxml書き出し[/]: {out_path}")
