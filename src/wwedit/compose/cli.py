"""``wwedit compose`` サブコマンド（EDL→mp4 合成 / fcpxml 書き出し）。"""

from __future__ import annotations

from pathlib import Path

import typer
from rich import print as rprint

from wwedit.compose.fcpxml import write_fcpxml
from wwedit.compose.ffmpeg_compose import compose_audio_kept, compose_kept
from wwedit.edl.schema import TimeRange, load_edl

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
    bgm_avoid_desktop: bool = typer.Option(
        False, "--bgm-avoid-desktop",
        help="PCシステム音が鳴っている間だけBGMを止める。**その回の音そのものを聴かせる時**"
             "だけ付ける（音楽生成の聴き比べ等）。既定OFF＝いつもどおりBGMを敷く"),
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
    chibi: bool = typer.Option(
        None, "--chibi/--no-chibi",
        help="[V] 左下・右下にちびキャラ2体を表示（話者側だけ口パク）。"
             "未指定は EDL.chibi.enabled に従う"),
    chibi_height: int = typer.Option(0, help="ちびキャラの表示高さpx（0=EDL既定320）"),
    chibi_margin_x: int = typer.Option(-1, help="画面端からの余白X（-1=EDL既定24）"),
    chibi_margin_y: int = typer.Option(-1, help="画面端からの余白Y（-1=EDL既定24）"),
    chibi_left: str = typer.Option("", help="左下に置く話者（EDL.chibi.sides の一時上書き）"),
    chibi_right: str = typer.Option("", help="右下に置く話者（同上）"),
    chibi_mouth_step: float = typer.Option(0.0, help="口形1段の秒数（0=既定0.045）"),
    infographic: bool = typer.Option(
        None, "--infographic/--no-infographic",
        help="[I] 本編冒頭に要約インフォグラフィックを表示。"
             "未指定は EDL.infographic.enabled に従う"),
    infographic_seconds: float = typer.Option(
        0.0, help="図解の表示秒数（0=EDL既定10秒・このレンダ限りの上書き）"),
    speedup: bool = typer.Option(
        False, help="[S] 発話の間を一定に揃える（無音を高速化して詰める・最後に掛かる）"),
    speedup_factor: float = typer.Option(
        8.0, help="[S] 高速化の**下限**倍率（既定8倍）。目標の間に届かない所だけ上がる"),
    speedup_max_factor: float = typer.Option(
        80.0, help="[S] 倍率の上限。長い間を目標へ潰しきるために上げる幅"),
    speedup_gap: float = typer.Option(
        0.0, help="[S] 揃える間の長さ(秒)。0=実測から自動（発話が連続する所の中央値）"),
    speedup_refresh: bool = typer.Option(
        False, help="[S] PC音声の鳴っている区間を測り直す（キャッシュを捨てる）"),
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

    # [V] ちびキャラ: 未指定(None)は EDL.chibi.enabled に従う。sides の一時上書きは非破壊
    chibi_on = chibi if chibi is not None else bool(edl.chibi and edl.chibi.enabled)
    if chibi_on:
        if not (edl.chibi and edl.character_cast):
            raise typer.BadParameter(
                "chibi には character_cast が要る（先に publish voice-cast → chibi ensure）")
        if chibi_left or chibi_right:
            sides = dict(edl.chibi.sides or {})
            if chibi_left:
                sides["left"] = chibi_left
            if chibi_right:
                sides["right"] = chibi_right
            edl.chibi.sides = sides  # このレンダ限り（保存しない）
    ch_margin = None
    if chibi_margin_x >= 0 or chibi_margin_y >= 0:
        base_m = edl.chibi.margin_px if edl.chibi else (24, 24)
        ch_margin = (chibi_margin_x if chibi_margin_x >= 0 else base_m[0],
                     chibi_margin_y if chibi_margin_y >= 0 else base_m[1])

    # [I] 要約インフォグラフィック: 未指定(None)は EDL.infographic.enabled に従う。
    # **投稿単位が2本目以降のときは出さない**（図解は収録1本ぶんの要約なので冒頭だけ）。
    ig_on = (infographic if infographic is not None
             else bool(edl.infographic and edl.infographic.enabled))
    if ig_on:
        if not (edl.infographic and edl.infographic.path):
            raise typer.BadParameter(
                "infographic には画像が要る（先に publish infographic を実行）")
        if post_unit_index > 0:
            rprint("[yellow]投稿単位2本目以降なので図解は出さない[/]")
            ig_on = False
    if ig_on and infographic_seconds > 0:
        edl.infographic.duration_s = infographic_seconds  # このレンダ限り（保存しない）

    # --bgm-avoid-desktop を付けた回だけ、PC音声が鳴っている間の BGM を止める
    mute_spans: list[tuple[float, float]] = []
    if bgm_path and bgm_avoid_desktop:
        mute_spans = _bgm_mute_spans(edl, edl_path, eff, refresh=speedup_refresh)
        if mute_spans:
            muted = sum(b - a for a, b in mute_spans)
            rprint(f"[dim]BGMを止める区間[/]: {len(mute_spans)}件 / {muted:.0f}秒"
                   "（PCシステム音が鳴っている所・--bgm-avoid-desktop）")

    result = compose_kept(
        edl, out_path, crf=crf, preset=preset, audio=audio,
        framed=framed, subtitles=subtitles, bgm=bgm_path, bgm_gain_db=bgm_gain_db,
        bgm_target_lufs=target, bgm_mute_spans=mute_spans, max_ranges=n, ranges=sel_ranges,
        chapter_ribbon=chapter_ribbon, ribbon_date=ribbon_date, overlays=overlays,
        chibi=chibi_on, chibi_height=chibi_height, chibi_margin=ch_margin,
        chibi_mouth_step=chibi_mouth_step or None, infographic=ig_on,
        data_dir=edl_path.parent,
    )
    size_mb = result.stat().st_size / 1e6
    rprint(f"[green]合成完了[/]: {result} ({size_mb:.1f}MB)")

    # 後段パスは **アイキャッチ → 高速化** の順に固定する。章時刻の補正もこの順で1回ずつ。
    ec_inserted = False
    ch_lines: list[str] = []
    if eyecatch:
        if not edl.chapters:
            rprint("[yellow]chapters が無いのでアイキャッチを挿入できません（先に chapter）[/]")
        else:
            from wwedit.compose.eyecatch_insert import insert_eyecatches

            ec_out = result.with_name(result.stem + "_ec.mp4")
            snd = "キャラの一言(Qwen3-TTS)" if eyecatch_voice else f"ジングル={eyecatch_jingle_dir}"
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
            result, ec_inserted = ec_path, True

    if speedup:
        _apply_speedup(
            edl, edl_path, result, eff, sel_ranges,
            ec_inserted=ec_inserted, ch_lines=ch_lines, factor=speedup_factor,
            max_factor=speedup_max_factor, target_gap=speedup_gap,
            refresh=speedup_refresh, crf=crf, preset=preset,
        )


def _desktop_spans(
    edl, edl_path: Path, *, refresh: bool, use_voice_path: bool = False,
) -> list[tuple[float, float]]:
    """PCシステム音声が鳴っている**素材秒**の区間（全 desktop トラックの和）。

    測るのに実時間で数十秒かかるので ``desktop_active.json`` にキャッシュする。

    ``use_voice_path`` は「EDL の時間軸で鳴っている区間が欲しい」とき。方式Bの
    ワープ後 EDL では desktop トラックの ``voice_path`` が**ワープ済みPC音声**で、
    素材そのものとは時間軸が違う（BGMを止める区間の算出はこちらが正しい）。
    """
    import json

    from wwedit.compose.speedup import desktop_active_spans, merge_spans

    cache_path = edl_path.parent / "desktop_active.json"
    cache = {}
    if cache_path.exists() and not refresh:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    spans: list[tuple[float, float]] = []
    dirty = False
    for track in edl.source.audio_tracks:
        if not track.is_desktop_audio:
            continue
        key = track.path  # 既定は素材そのもの。voice_path（σ版/ワープ版）ではない
        if use_voice_path and track.voice_path:
            key = str(track.voice_path)
        if key not in cache:
            rprint(f"[dim]PC音声の鳴っている区間を計測中: {Path(key).name}[/]")
            sp, info = desktop_active_spans(key)
            cache[key] = {"spans": [[a, b] for a, b in sp], "info": info}
            dirty = True
        ent = cache[key]
        i = ent.get("info", {})
        rprint(f"  {Path(key).name}: 鳴っている区間 {len(ent['spans'])}件 "
               f"({i.get('active_ratio', 0) * 100:.1f}%・"
               f"床{i.get('floor_db', '?')}dB→閾値{i.get('threshold_db', '—')}dB)")
        spans += [(a, b) for a, b in ent["spans"]]
    if dirty:
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    return merge_spans(spans)


def _bgm_mute_spans(
    edl, edl_path: Path, ranges, *, refresh: bool = False,
) -> list[tuple[float, float]]:
    """**PCシステム音が鳴っている出力秒**（＝BGMを止める区間）。

    音楽生成の聴き比べのように **PC音声そのものを聴かせる回**があるので、鳴っている
    間は BGM を敷かない（ユーザー指示・2026-08-06・方式A/B とも）。
    """
    from wwedit.compose.speedup import src_spans_to_out

    src = _desktop_spans(edl, edl_path, refresh=refresh, use_voice_path=True)
    return src_spans_to_out(src, ranges, edl.freezes)


def _apply_speedup(
    edl, edl_path: Path, src_mp4: Path, eff, sel_ranges, *,
    ec_inserted: bool, ch_lines: list[str], factor: float, max_factor: float,
    target_gap: float, refresh: bool, crf: int, preset: str,
) -> None:
    """[S] 発話の間を一定に揃えた mp4 と、補正済みチャプター行を書き出す。"""
    from wwedit.common.media import probe
    from wwedit.compose.eyecatch_insert import shifted_chapter_lines
    from wwedit.compose.speedup import (
        apply_speedups,
        effective_plan,
        eyecatch_inserts,
        shift_chapter_lines,
        shift_plan_by_inserts,
        speedup_plan,
    )

    dsk = _desktop_spans(edl, edl_path, refresh=refresh)
    base, info = speedup_plan(edl, eff, freezes=edl.freezes, desktop_src_spans=dsk,
                              target_gap=target_gap, factor=factor, max_factor=max_factor)
    rprint(f"  揃える間 = {info['target_gap_s']:.2f}秒"
           f"（{'実測から自動' if info['auto_target'] else '指定'}）・"
           f"発話ブロック{info['n_blocks']}件 / 速くできない区間{info['n_blocked']}件")
    if not base:
        rprint("[yellow]縮める間がありません（もともと詰まっている）[/]")
        return
    # アイキャッチ挿入後の mp4 に掛けるので、計画も挿入ぶんだけ後ろへ写す（挿入点で分割）
    plan = (shift_plan_by_inserts(base, eyecatch_inserts(edl, sel_ranges))
            if ec_inserted else base)
    # 章時刻の補正は**フレームに丸めた後の計画**で行う（丸め前だと出力とずれる）
    fps = int(edl.source.fps or 30)
    plan = effective_plan(plan, probe(src_mp4).duration_s, fps=fps)
    if not plan:
        rprint("[yellow]縮める間がありません（フレーム換算で短すぎる）[/]")
        return
    saved = sum((b - a) * (1.0 - 1.0 / f) for a, b, f in plan)
    rprint(f"[dim]高速化中[/]: {len(plan)}区間・倍率 {info['factor_min']:g}〜"
           f"{info['factor_max']:g}倍(中央{info['factor_median']:g}) → 約{saved:.1f}秒短縮 ...")
    out_path = src_mp4.with_name(src_mp4.stem + "_sp.mp4")
    try:
        sp_path = apply_speedups(src_mp4, out_path, plan,
                                 fps=fps, crf=crf, preset=preset)
    except (RuntimeError, ValueError) as e:
        rprint(f"[red]高速化に失敗[/]: {e}")
        raise typer.Exit(1) from e
    # 章時刻: アイキャッチ補正 → 高速化補正 の順で**1回ずつ**通す（二重補正しない）
    lines = ch_lines or (shifted_chapter_lines(edl, sel_ranges, duration=0.0)
                         if edl.chapters else [])
    if lines:
        lines = shift_chapter_lines(lines, plan)
        cl_path = sp_path.with_name(sp_path.stem + "_chapters.txt")
        cl_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        rprint(f"  補正チャプター → {cl_path}（概要欄はこの時刻を使う）")
    rprint(f"[green]高速化完了[/]: {sp_path} ({sp_path.stat().st_size / 1e6:.1f}MB)")


@compose_app.command()
def warp(
    edl_path: Path = typer.Argument(..., help="方式Bの EDL（voice_tts_report.json が要る）"),
    out_edl: Path = typer.Option(None, help="出力EDL（既定 <edl>.warped.json）"),
    gap: float = typer.Option(0.15, help="[S2] 発話と発話のあいだ（秒）。常にこの長さになる"),
    speech_rate: float = typer.Option(8.0, help="[S2] 発話中の映像倍率の上限"),
    gap_rate: float = typer.Option(80.0, help="[S2] 間の映像倍率の上限"),
    lookahead: float = typer.Option(
        5.0, help="[S2] 読み上げが長いとき、次の発話の映像へ何秒まで食い込んでよいか"
                  "（超えたらフリーズ）"),
    crf: int = typer.Option(18, help="ワープ素材のCRF（中間素材なので高画質側）"),
    preset: str = typer.Option("veryfast", help="x264 preset"),
    refresh: bool = typer.Option(False, help="PC音声の計測キャッシュを作り直す"),
    dry_run: bool = typer.Option(False, help="計画だけ出してレンダしない"),
) -> None:
    """[S2] **合成の手前で**収録映像だけを可変速にし、ワープ済み素材＋新EDLを作る。

    読み上げ・字幕・口パクは通常速度のまま最後まで流れ、**収録映像だけ**が縮む。
    出来上がった EDL を `compose video` に渡せば、以降は普通の素材として扱える。
    """
    import json as _json

    from wwedit.compose.ffmpeg_compose import out_total as _out_total
    from wwedit.compose.timewarp import anchors_with_rows, build_warp
    from wwedit.compose.warp_apply import (
        render_voice_track,
        render_warped_audio,
        render_warped_footage,
        warp_edl,
        warp_pieces,
        write_warped_report,
    )
    from wwedit.publish.voice_tts import (
        REPORT_NAME,
        load_decisions,
        load_terms,
        subtitles_from_reading,
    )

    edl = load_edl(edl_path)
    d = edl_path.parent
    rep_path = d / REPORT_NAME
    if not rep_path.exists():
        rprint(f"[red]{REPORT_NAME} が無い[/]（先に publish voice-tts-finalize）")
        raise typer.Exit(1)
    # **全部がアンカー**。台詞は重ねないので、読み上げの並びがそのまま出力の並びになる。
    rows = _json.loads(rep_path.read_text(encoding="utf-8"))["rows"]
    rg, frz = edl.kept_ranges(), edl.freezes
    tot = _out_total(rg, frz)
    fps = int(edl.source.fps or 25)

    hold = _hold_spans(edl, edl_path, rg, frz, refresh=refresh)
    pairs = anchors_with_rows(rows, rg, freezes=frz)
    w = build_warp([a for a, _ in pairs], rg, freezes=frz, total=tot, target_gap=gap,
                   speech_max_rate=speech_rate, gap_max_rate=gap_rate,
                   hold_spans=hold, lookahead=lookahead, fps=fps)
    pieces = warp_pieces(w, rg, frz, fps=fps)
    rates = sorted(s.rate for s in w.segs if s.kind == "speech")
    rprint(f"[dim]ワープ計画[/]: {tot:.1f}s → [bold]{w.out_total:.1f}s[/] "
           f"（-{tot - w.out_total:.1f}s）・区間{len(w.segs)}（レンダ片{len(pieces)}）\n"
           f"  発話中の映像倍率 中央{rates[len(rates) // 2]:.2f}倍 / 最大{rates[-1]:.2f}倍"
           f"（等速のまま {sum(1 for r in rates if r < 1.01)}/{len(rates)}本）\n"
           f"  間 {gap:.2f}秒で一定 / PC音声は等速固定 {len(hold)}区間")
    if dry_run:
        return

    # 1) 映像（音は入れない。音を伸縮しないのがこの方式の肝）
    warped = d / "footage_warped.mp4"
    rprint(f"[dim]映像をワープ中[/] → {warped.name} ...")
    render_warped_footage(edl.source.video_path, pieces, warped,
                          fps=fps, crf=crf, preset=preset)
    # 2) PC音声（切って詰めるだけ）
    desktop: dict[str, Path] = {}
    for t in edl.source.audio_tracks:
        if not t.is_desktop_audio:
            continue
        o = d / f"warp_{Path(t.path).stem}.wav"
        rprint(f"[dim]PC音声をワープ中[/] → {o.name} ...")
        desktop[t.path] = render_warped_audio(t.path, pieces, o)
    # 3) 読み上げ（置き直すだけ・一切加工しない）
    new_rows: list[dict] = []
    for (start, _dur), (_a, r) in zip(w.placements, pairs, strict=True):
        new_rows.append({**r, "out_start": round(start, 3), "out_want": round(start, 3),
                         "drift": 0.0})
    new_rows.sort(key=lambda r: (r["out_start"], r["idx"]))
    voices: dict[str, Path] = {}
    for t in edl.source.audio_tracks:
        if t.is_desktop_audio:
            continue
        clips = [(float(r["out_start"]), Path(r["clip"]))
                 for r in new_rows
                 if r["speaker"] == t.speaker and Path(r["clip"]).exists()]
        if not clips:
            continue
        o = d / f"warp_{t.speaker}_tts.wav"
        rprint(f"[dim]読み上げを配置中[/] → {o.name}（{len(clips)}本）...")
        voices[t.speaker] = render_voice_track(clips, o, total=w.out_total)
    # 4) 字幕は新しい out 座標で作り直す（写像では合わない）
    one = [TimeRange(start=0.0, end=w.out_total)]
    subs = subtitles_from_reading(new_rows,
                                  load_decisions(d / "voice_tts_decisions.json"),
                                  one, terms=load_terms(d / "voice_tts_terms.json"))
    new = warp_edl(edl, w, warped, ranges=rg, freezes=frz, voice_paths=voices,
                   desktop_paths=desktop, subtitles=subs, report_rows=new_rows)
    out_edl = out_edl or edl_path.with_suffix(".warped.json")
    out_edl.write_text(new.model_dump_json(indent=1), encoding="utf-8")
    write_warped_report(new_rows, out_edl.parent / f"warped_{REPORT_NAME}",
                        out_total=w.out_total)
    rprint(f"[green]ワープ完了[/]: {out_edl}（{w.out_total:.1f}s・字幕{len(subs)}枚）\n"
           f"  次: compose video {out_edl} --framed --subtitles --chibi ... "
           f"[dim](--speedup は不要)[/]")


def _hold_spans(edl, edl_path: Path, ranges, freezes, *, refresh: bool):
    """PCシステム音声が鳴っている **src' 区間**（＝倍率1.0を強制する所）。"""
    from wwedit.compose.speedup import merge_spans, src_spans_to_out

    return merge_spans(src_spans_to_out(
        _desktop_spans(edl, edl_path, refresh=refresh), ranges, freezes))


@compose_app.command()
def speedup(
    edl_path: Path = typer.Argument(..., help="対象 EDL"),
    src_mp4: Path = typer.Argument(..., help="合成済みmp4（この後段パスだけを掛け直す）"),
    factor: float = typer.Option(8.0, help="[S] 高速化の**下限**倍率"),
    max_factor: float = typer.Option(80.0, help="[S] 倍率の上限"),
    gap: float = typer.Option(0.0, help="[S] 揃える間の長さ(秒)。0=実測から自動"),
    eyecatch: bool = typer.Option(False, help="src_mp4 がアイキャッチ挿入済みなら付ける"),
    refresh: bool = typer.Option(False, help="PC音声の計測キャッシュを作り直す"),
    crf: int = typer.Option(20, help="x264 CRF"),
    preset: str = typer.Option("medium", help="x264 preset"),
) -> None:
    """[S] **合成済みの mp4 に高速化だけを掛け直す**（`compose video` をやり直さない）。

    通しレンダは20分以上かかるので、目標の間を変えて試すたびに合成し直すのは高い。
    合成が途中で落ちた／親プロセスを止めた後の復旧にも使う。
    """
    edl = load_edl(edl_path)
    if not src_mp4.exists():
        rprint(f"[red]mp4 が見つかりません[/]: {src_mp4}")
        raise typer.Exit(1)
    eff = edl.kept_ranges()
    _apply_speedup(
        edl, edl_path, src_mp4, eff, None, ec_inserted=eyecatch, ch_lines=[],
        factor=factor, max_factor=max_factor, target_gap=gap, refresh=refresh,
        crf=crf, preset=preset,
    )


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
