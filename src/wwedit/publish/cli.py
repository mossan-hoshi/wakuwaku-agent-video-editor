"""``wwedit publish`` サブコマンド（[M4] 投稿パッケージ）。"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import typer
from rich import print as rprint

from wwedit.edl.schema import load_edl

publish_app = typer.Typer(help="投稿パッケージ（概要欄/タイトル等）", no_args_is_help=True)


@publish_app.command()
def description(
    edl_path: Path = typer.Argument(..., help="対象 EDL（chapters が必要）"),
    agenda: str = typer.Option("", help="Agenda「」のテーマ（未指定は --agenda-file）"),
    agenda_file: Path = typer.Option(None, help="Agendaテーマのテキスト（LLM生成）"),
    intro_file: Path = typer.Option(
        None, help="**Agendaより前**に置く冒頭ブロック（任意）。この動画をどう作ったか等、"
                   "その回だけの前置き。未指定なら従来どおり Agenda から始まる"),
    hashtags: str = typer.Option("", help="ハッシュタグ行（例 '#個人開発 #生成ai #aicoding'）"),
    links_file: Path = typer.Option(
        None, help="関連リンク（各行 'ラベル<TAB>URL'。タブが無ければ最後の空白で分割）"),
    out: Path = typer.Option(None, help="出力（既定 data/<date>/youtube_description.txt）"),
    post_unit_index: int = typer.Option(
        -1, help="投稿単位[K]。その単位の章で概要欄を作る（-1=収録まるごと）"),
    chapter_lines_file: Path = typer.Option(
        None, help="章行を明示上書き（[H]アイキャッチ挿入時の補正章行 *_ec_chapters.txt）"),
    allow_invalid_chapters: bool = typer.Option(
        False, "--allow-invalid-chapters",
        help="章がYouTubeの条件を満たさなくても異常終了しない（既定は弾く）"),
) -> None:
    """YouTube 概要欄を**チャンネル実フォーマット**で組み立てる。

    形式: ``Agenda「テーマ」`` → 関連リンク(任意) → ``#タグ`` → ``00:00 - start`` 以下の
    タイムスタンプ。要約/AI免責/チャンネルURL/タイトル再掲は**入れない**（実投稿に無い）。
    タイトルは動画 snippet 側で別途付ける。`--post-unit-index N` で単位内の章・時刻。

    最後に**章がYouTubeの条件を満たすか検査**し、破っていれば（ファイルは書いた上で）
    異常終了する。1つでも破ると章リストが丸ごと無効化されるため（#101 の先頭章9秒）。
    """
    from wwedit.publish.description import build_description

    edl = load_edl(edl_path)
    if not edl.chapters:
        rprint("[yellow]chapters がありません（先に chapter prepare/apply）[/]")
    ch_lines = None
    if chapter_lines_file and chapter_lines_file.exists():
        # [H] アイキャッチ挿入で章時刻がずれた場合の補正済み章行を優先採用
        ch_lines = [ln for ln in chapter_lines_file.read_text(
            encoding="utf-8").splitlines() if ln.strip()]
    elif post_unit_index >= 0:
        from wwedit.edl.postunit import post_unit_chapter_lines

        ch_lines = post_unit_chapter_lines(edl, post_unit_index)

    agenda_text = agenda or (
        agenda_file.read_text(encoding="utf-8").strip()
        if agenda_file and agenda_file.exists() else "")
    if not agenda_text:
        raise typer.BadParameter("--agenda か --agenda-file が必要（Agenda「」のテーマ）")

    links: list[tuple[str, str]] = []
    if links_file and links_file.exists():
        for ln in links_file.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            label, _, url = (ln.partition("\t") if "\t" in ln else ln.rpartition(" "))
            if label and url:
                links.append((label.strip(), url.strip()))

    intro_text = (intro_file.read_text(encoding="utf-8").strip()
                  if intro_file and intro_file.exists() else "")

    text = build_description(edl, agenda=agenda_text, intro=intro_text, links=links or None,
                             hashtags=hashtags or None, chapter_lines=ch_lines)
    default = (
        f"youtube_description_p{post_unit_index}.txt" if post_unit_index >= 0
        else "youtube_description.txt")
    out_path = out or (edl_path.parent / default)
    out_path.write_text(text, encoding="utf-8")
    n_ch = len(ch_lines) if ch_lines is not None else len(edl.chapters or [])
    rprint(f"[green]概要欄[/]: {out_path}（{'冒頭/' if intro_text else ''}"
           f"Agenda/{'リンク' if links else '—'}/"
           f"{'#タグ' if hashtags else '—'}/タイムスタンプ{n_ch}）")
    _check_chapters(text, allow_invalid=allow_invalid_chapters)


def _check_chapters(text: str, *, allow_invalid: bool = False) -> None:
    """概要欄の章がYouTubeの条件を満たすか検査し、破っていれば止める（共通処理）。"""
    from wwedit.publish.description import chapter_problems

    problems = chapter_problems(text)
    if not problems:
        return
    rprint("[red]YouTubeが章を生成しない概要欄です[/]（1つでも破ると章は1つも出ません）:")
    for p in problems:
        rprint(f"  - {p}")
    if allow_invalid:
        rprint("[yellow]--allow-invalid-chapters のため続行します[/]")
        return
    rprint("[dim]章の区切りを直す（短い章は隣と統合）か --allow-invalid-chapters[/]")
    raise typer.Exit(1)


@publish_app.command()
def thumbnail(
    edl_path: Path = typer.Argument(..., help="対象 EDL（出力先ディレクトリの決定に使用）"),
    prompt: str = typer.Option(
        ..., help="サムネ全体のプロンプト（描画する日本語タイトル・文字サイズ階層・配色・"
                  "構図・キャラの表情/ポーズ・背景まで含めて記述。文字もモデルが描く）"),
    char: str = typer.Option("noa", help="参照する立ち姿キャラID（絵柄/キャラ固定）。空で参照なし"),
    model: str = typer.Option(
        "gemini-3-pro-image", help="画像モデル（既定=nano banana 2＝日本語タイポも崩れにくい）"),
    out: Path = typer.Option(None, help="出力PNG（既定 data/<date>/thumbnail.png）"),
    image_size: str = typer.Option(
        "2K", help="解像度。**lite/flash 系は 2K 非対応なので 1K を渡す**"),
) -> None:
    """[L] サムネ生成（**nano banana 2 一発生成**）。

    キャラ・背景・**日本語タイトル文字まで一括でモデルが描く**。``--char`` の立ち姿
    ``<id>_a*.webp`` を参照に絵柄/キャラ同一性を固定。旧来の「背景だけ生成＋PIL帯合成」は廃止。

    安く試すなら ``--model gemini-3.1-flash-lite-image --image-size 1K``（nano banana 2 lite）。
    """
    from wwedit.publish.thumbnail import generate_thumbnail

    out_path = out or (edl_path.parent / "thumbnail.png")
    rprint(f"[dim]サムネ一発生成中（{model}/{image_size}・参照={char or 'なし'}・課金あり）...[/]")
    generate_thumbnail(prompt, out_path, char=char or None, model=model, image_size=image_size)
    rprint(f"[green]サムネ[/]: {out_path}（nano banana 2 一発生成・文字込み）")


@publish_app.command()
def infographic(
    edl_path: Path = typer.Argument(..., help="対象 EDL（chapters/subtitles を入力にする）"),
    title: str = typer.Option("", help="動画タイトル（未指定は --title-file）"),
    title_file: Path = typer.Option(None, help="タイトルのテキストファイル"),
    desc_file: Path = typer.Option(
        None, help="概要欄（既定 <date>/youtube_description.txt。無ければ概要欄なしで生成）"),
    model: str = typer.Option(
        None, help="画像モデル（既定=nano banana 2。安く試すなら "
                   "gemini-3.1-flash-lite-image ＋ --image-size 1K）"),
    aspect_ratio: str = typer.Option("21:9", help="生成アスペクト（横長）"),
    image_size: str = typer.Option("2K", help="解像度。lite/flash 系は 1K を渡す"),
    out: Path = typer.Option(None, help="出力PNG（既定 data/<date>/infographic.png）"),
    seconds: float = typer.Option(10.0, help="本編冒頭で表示する秒数"),
    prompt_only: bool = typer.Option(
        False, "--prompt-only",
        help="APIを叩かずプロンプトだけ出す（**課金前の査収用**）"),
) -> None:
    """[I] 本編冒頭の**要約インフォグラフィック**を生成し、EDL に表示設定を書く。

    入力は**タイトル・チャプター一覧・概要欄・字幕全文**で、それをそのまま
    nano banana 2 に読ませて図解を1枚描かせる（1-shot・前段LLMなし）。
    表示は上部UI/ちびキャラ/字幕に被らない安全枠へ contain 収め（compose 側が計算）。

    **課金なので1枚勝負**。撮り直しはユーザーが決める（auto-edit の G-I ゲート）。
    先に ``--prompt-only`` で入力を確認できる。
    """
    from wwedit.edl.schema import InfographicConfig, save_edl
    from wwedit.publish.infographic import (
        DEFAULT_MODEL,
        build_prompt,
        build_source_text,
        generate_infographic,
    )

    edl = load_edl(edl_path)
    title_text = title or (
        title_file.read_text(encoding="utf-8").strip()
        if title_file and title_file.exists() else "")
    dfile = desc_file or (edl_path.parent / "youtube_description.txt")
    desc_text = dfile.read_text(encoding="utf-8") if Path(dfile).exists() else ""
    if not title_text and not desc_text and not edl.chapters:
        raise typer.BadParameter(
            "タイトル・概要欄・チャプターのどれも無い（骨子が決まらないので図解を作れない）")

    source = build_source_text(edl, title=title_text, description=desc_text)
    if prompt_only:
        prompt_path = edl_path.parent / "infographic_prompt.txt"
        prompt_path.write_text(build_prompt(source), encoding="utf-8")
        rprint(f"[green]プロンプト[/]: {prompt_path}（{len(source)}字の入力・API未実行）")
        return

    out_path = out or (edl_path.parent / "infographic.png")
    mdl = model or DEFAULT_MODEL
    rprint(f"[dim]図解生成中（{mdl}/{image_size}/{aspect_ratio}・"
           f"入力{len(source)}字・課金あり・1枚勝負）...[/]")
    saved, prompt = generate_infographic(
        edl, out_path, title=title_text, description=desc_text,
        model=mdl, aspect_ratio=aspect_ratio, image_size=image_size)
    (edl_path.parent / "infographic_prompt.txt").write_text(prompt, encoding="utf-8")

    cfg = edl.infographic or InfographicConfig()
    cfg.enabled = True
    cfg.path = str(saved.resolve())
    cfg.duration_s = seconds
    edl.infographic = cfg
    save_edl(edl, edl_path)
    rprint(f"[green]図解[/]: {saved}（本編冒頭 {seconds:g} 秒に表示・EDL更新済み）")
    for t, title in _chapters_inside(edl, cfg.start_s, cfg.start_s + seconds):
        rprint(f"[yellow]注意[/]: 表示中({t:.1f}s)に章境界『{title}』がある。"
               "`--eyecatch` を使うとここでアイキャッチが割り込んで図解が分断される "
               f"→ `--infographic-seconds {max(1.0, t - cfg.start_s):.0f}` 等で短くする")
    rprint("[dim]次: compose video --infographic で確認（安全枠に自動収め）[/]")


def _chapters_inside(edl, out_start: float, out_end: float) -> list[tuple[float, str]]:
    """[out_start, out_end) の**内側**に落ちる章境界（出力秒, タイトル）を返す。

    `--eyecatch` は章境界に2秒のアイキャッチを割り込ませるので、図解の表示中に境界があると
    図解が真っ二つになる。先頭(0秒)の境界は図解より前に出るだけなので含めない。
    """
    from wwedit.chapter.detect import source_to_output

    out: list[tuple[float, str]] = []
    for i, c in enumerate(sorted(edl.chapters, key=lambda c: c.start_at)):
        ot = 0.0 if i == 0 else source_to_output(edl, c.start_at)
        if out_start < ot < out_end:
            out.append((ot, c.chapter_title or f"チャプター{i + 1}"))
    return out


@publish_app.command()
def tts(
    text: str = typer.Option(..., help="読み上げテキスト（内容/尺の判断はスキル側）"),
    out: Path = typer.Option(..., help="出力 wav（44100/mono）"),
    voice: str = typer.Option("noa", help="キャラID（refs/<id>/ が要る）"),
    ref: str = typer.Option("", help="参照セット名（空=そのキャラの先頭。参照長は10〜13秒）"),
    seed: int = typer.Option(0, help="生成シード（同じ値なら同じ音）"),
    dur_sec: float = typer.Option(20.0, help="生成尺の上限(秒)"),
) -> None:
    """[G] **Qwen3-TTS**（ゼロショット音声クローン）で音声合成し wav 保存（**決定的**・尺を表示）。

    参照音声は `refs/<char>/refs.json` の同梱セット（実効10〜13秒）。サーバ起動は不要＝
    専用venvのサブプロセスで走る。尺が長い等の判断は呼び出し側。
    """
    from wwedit.publish.qwen_tts import synth_to_file

    kw = {"seed": seed, "dur": dur_sec}
    if ref:
        kw["ref"] = ref
    try:
        dur = synth_to_file(text, out, voice, **kw)
    except (RuntimeError, FileNotFoundError) as e:
        rprint(f"[red]{e}[/]")
        raise typer.Exit(1) from e
    flag = "OK(<=10s)" if dur <= 10.0 else "[yellow]>10s（イントロは詰める）[/]"
    rprint(f"[green]音声[/]: {out}  実尺={dur:.2f}s {flag}  voice={voice}")


@publish_app.command(name="voice-cast")
def voice_cast(
    edl_path: Path = typer.Argument(..., help="対象 EDL"),
    method: str = typer.Option(..., help="音声方式: seedvc(声質変換) / tts(読み上げ)"),
    chars: str = typer.Option(
        "", help="キャラ指名（カンマ区切り・話者名ソート順に割当。空=ランダム）"),
) -> None:
    """[V] 話者→のべつべキャラの割当（音声変換・字幕色・ちびキャラの共有SoT）。

    参照音声のあるキャラからランダムに選ぶ（リロール=再実行 / 指名=--chars）。
    字幕色は自動でキャラテーマ色になり、ちびキャラ表示も有効化される。
    実行前に承認を取る運用（auto-edit の G-V ゲート）。戻すのは voice-revert。
    """
    from wwedit.edl.schema import save_edl
    from wwedit.publish.qwen_tts import available_voices
    from wwedit.publish.voice_cast import apply_cast, describe_cast, pick_cast

    edl = load_edl(edl_path)
    pool = available_voices()
    char_list = [c.strip() for c in chars.split(",") if c.strip()] or None
    try:
        cast = pick_cast(edl, chars=char_list, pool=pool)
        apply_cast(edl, cast, method=method)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e
    save_edl(edl, edl_path)
    sides = sorted(cast)  # 既定: 話者名ソート順で 左→右
    rprint(f"[green]キャラ割当[/]（method={method}）:")
    for speaker, char, hex_c in describe_cast(cast):
        side = "左下" if sides.index(speaker) == 0 else "右下"
        rprint(f"  {speaker} → [bold]{char}[/]  字幕色={hex_c}  配置={side}")
    rprint("[dim]リロール=同コマンド再実行 / 指名=--chars noa,suzu / 戻す=voice-revert[/]")


@publish_app.command(name="voice-revert")
def voice_revert(
    edl_path: Path = typer.Argument(..., help="対象 EDL"),
) -> None:
    """[V] キャラ声差し替えを全て元に戻す（cast/字幕色/voice_path/freezes/chibi）。"""
    from wwedit.edl.schema import save_edl
    from wwedit.publish.voice_cast import revert_voice

    edl = load_edl(edl_path)
    done = revert_voice(edl)
    if not done:
        rprint("[yellow]戻すものが無い（voice-cast 未適用）[/]")
        return
    save_edl(edl, edl_path)
    for msg in done:
        rprint(f"  - {msg}")
    rprint("[green]voice-revert 完了[/]")


@publish_app.command(name="voice-convert")
def voice_convert_cmd(
    edl_path: Path = typer.Argument(..., help="対象 EDL（voice-cast 済み・method=seedvc）"),
    max_chunks: int = typer.Option(
        6, help="この実行で変換するチャンク数（前景600s以内に収まる粒度で分割実行）"),
    diffusion_steps: int = typer.Option(30, help="Seed-VC の拡散ステップ数"),
) -> None:
    """[V] 方式A: Seed-VC で発話チャンクをキャラ声に変換する（**分割実行・再開可**）。

    初回に参照音源の構築とチャンク切り出しを行い、以降は未変換チャンクを
    ``--max-chunks`` 件ずつ変換する。**残り0になったら**全長トラックを組み立てて
    ``voice_path`` をセットする（タイミングは元音声と完全一致）。
    残りが出た場合は同コマンドをそのまま再実行すること。
    """
    from wwedit.edl.schema import save_edl
    from wwedit.publish.seedvc import build_char_ref, convert_batch
    from wwedit.publish.voice_convert import (
        assemble_track,
        build_manifest,
        load_manifest,
        measure_loudness,
        pending_chunks,
    )

    edl = load_edl(edl_path)
    if not edl.character_cast:
        raise typer.BadParameter("character_cast が無い（先に publish voice-cast）")
    method = (edl.meta.get("voice") or {}).get("method")
    if method != "seedvc":
        raise typer.BadParameter(
            f"method={method}（voice-convert は seedvc 専用。TTSは voice-tts）")

    work = edl_path.parent / "voice"
    manifest = load_manifest(work)
    if manifest is None:
        rprint("[cyan]初回: 発話チャンクを切り出し中…[/]")
        manifest = build_manifest(edl, method="seedvc", work_dir=work)
        rprint(f"  チャンク {len(manifest['chunks'])} 件")

    refs: dict[str, Path] = {}
    for speaker, char in edl.character_cast.items():
        refs[speaker] = build_char_ref(char)

    total = len(manifest["chunks"])
    pending = pending_chunks(manifest)
    if pending:
        batch = pending[:max_chunks]
        rprint(f"[cyan]Seed-VC 変換[/]: {len(batch)} 件（残り {len(pending)}/{total}）…")
        jobs = [{"source": c["src"], "target": str(refs[c["speaker"]]), "out": c["out"]}
                for c in batch]
        try:
            convert_batch(jobs, diffusion_steps=diffusion_steps)
        except (RuntimeError, FileNotFoundError) as e:
            rprint(f"[red]{e}[/]")
            raise typer.Exit(1) from e
        pending = pending_chunks(manifest)

    done = total - len(pending)
    if pending:
        rprint(f"[yellow]進捗 {done}/{total}（残り{len(pending)}）→ 同コマンドを再実行[/]")
        return

    rprint("[cyan]全チャンク変換済み → 全長トラックを組み立て中…[/]")
    for ti, track in enumerate(edl.source.audio_tracks):
        if track.is_desktop_audio:
            continue
        placements = [
            (c["start"], Path(c["out"]), c["end"] - c["start"])
            for c in manifest["chunks"] if c["track_index"] == ti
        ]
        out_wav = work / f"t{ti}_{track.speaker}_seedvc.wav"
        # normalize=True: Seed-VC の出力は話者ごとにレベルがばらつき、TPが0dBを超えることも
        # ある。収録音と同じ基準(-16 LUFS/TP-1.5)へ揃えてから compose へ渡す。
        assemble_track(placements, edl.source.duration_s, out_wav, normalize=True)
        track.voice_path = str(out_wav)
        loud = measure_loudness(out_wav)
        rprint(f"  {track.speaker} → {out_wav.name}（{len(placements)}チャンク・"
               f"{loud['input_i']:.2f} LUFS / TP {loud['input_tp']:.2f}dB）")
    save_edl(edl, edl_path)
    rprint("[green]voice-convert 完了[/]（compose は voice_path を自動で使う）")


@publish_app.command(name="voice-tts-prepare")
def voice_tts_prepare(
    edl_path: Path = typer.Argument(..., help="対象 EDL（voice-cast 済み・method=tts）"),
    screen_text: Path = typer.Option(
        None, help="画面OCRテキスト(screen_text.txt)。**固有名の正しい表記の出どころ**。"
                   "既定は同フォルダ"),
) -> None:
    """[V] 方式B: ターンTSVを書き出す（→ voice-scripter スキルで読み上げ文＋用語表記を作る）。

    固有名補正のため、画面OCRテキスト（`chapter screen-text` 後に生成）があれば末尾に
    文脈として付ける（[[chapter-proper-nouns-need-ocr]]）。スキルはこれを見て
    **読み上げ用のカタカナ**と**字幕用の正式表記**の対応表 ``voice_tts_terms.json`` を作る。
    """
    from wwedit.publish.voice_tts import DECISIONS_NAME, TERMS_NAME, TSV_NAME, write_tts_input

    edl = load_edl(edl_path)
    if not edl.character_cast:
        raise typer.BadParameter("character_cast が無い（先に publish voice-cast --method tts）")
    tsv = edl_path.parent / TSV_NAME
    n = write_tts_input(edl, tsv)

    st = screen_text or (edl_path.parent / "screen_text.txt")
    if st.exists():
        with tsv.open("a", encoding="utf-8") as f:
            f.write(
                "\n\n# --- 画面テキスト(OCR) ---"
                "（固有名の**正しい表記**はこちらを正とする。STTの聞き取り誤りを補正）\n"
                + st.read_text(encoding="utf-8").strip() + "\n"
            )
        rprint(f"[green]TTS入力[/]: {tsv}（{n}ターン・OCR文脈 {st.name} を付与）")
    else:
        rprint(f"[green]TTS入力[/]: {tsv}（{n}ターン）")
        rprint("[yellow]screen_text.txt が無い[/]: 固有名の表記を補正できない"
               "（先に chapter screen-text を実行すると字幕の表記ゆれが減る）")
    rprint(f"[dim]次: voice-scripter スキルで {DECISIONS_NAME} と {TERMS_NAME} を作る "
           "→ publish voice-tts[/]")


@publish_app.command(name="voice-tts-subtitles")
def voice_tts_subtitles(
    edl_path: Path = typer.Argument(..., help="対象 EDL（voice-scripter 済み）"),
) -> None:
    """[V] 方式B: **合成前に**読み上げ文から字幕を貼る（G2 で内容を確認するため）。

    方式Bを選んだ回は、手順8の要約字幕（caption-summarizer）は最終的に捨てられるので
    **走らせない**。代わりにこれで字幕を先に確定させ、G2 で内容を確認してもらう。
    時刻は文字数からの見積りで、合成後に `voice-tts-finalize` が実尺へ貼り直す。
    """
    from wwedit.edl.schema import save_edl
    from wwedit.publish.voice_tts import (
        DECISIONS_NAME,
        TERMS_NAME,
        load_decisions,
        load_terms,
        reading_rows,
        subtitles_from_reading,
        tts_clips,
        tts_units,
    )

    edl = load_edl(edl_path)
    dec_path = edl_path.parent / DECISIONS_NAME
    if not dec_path.exists():
        raise typer.BadParameter(f"{dec_path} が無い（voice-scripter スキルを先に）")
    decisions = load_decisions(dec_path)
    terms = load_terms(edl_path.parent / TERMS_NAME)
    # 台本を最初に読むコマンドなので、**合成に入る前に**長すぎる1文を知らせる
    _warn_long_sentences(tts_clips(tts_units(edl), decisions))
    rows = reading_rows(edl, decisions)
    subs = subtitles_from_reading(rows, decisions, edl.kept_ranges(), (), terms=terms)
    if not subs:
        raise typer.BadParameter("字幕が作れない（decisions が空？）")
    vmeta = edl.meta.setdefault("voice", {})
    if "prev_subtitles" not in vmeta:
        vmeta["prev_subtitles"] = [s.model_dump() for s in edl.subtitles]
    edl.subtitles = subs
    save_edl(edl, edl_path)
    rprint(f"[green]仮字幕[/]: {len(subs)}件（用語表記 {len(terms)}件を適用）")
    rprint("[dim]G2 で内容を確認 → 直しは voice_tts_decisions.json 側で。"
           "合成後 voice-tts-finalize が実尺へ貼り直す[/]")


@publish_app.command(name="voice-tts")
def voice_tts_cmd(
    edl_path: Path = typer.Argument(..., help="対象 EDL"),
    max_jobs: int = typer.Option(
        0, help="この実行で合成するジョブ数（0=全部・分割実行したいときだけ指定）。"
                "**1回の実行につきモデル読み込みが約105秒**かかるので、分けるほど遅くなる"),
    seed: int = typer.Option(0, help="生成シード"),
) -> None:
    """[V] 方式B: 決定JSONの読み上げ文を Qwen3-TTS で**文単位**に一括合成する。

    ターンを ``tts_units`` で切り、さらに ``tts_clips`` で**文ごと**に割る。
    **合成の単位＝後処理（話者チェック・字幕・口パク・感情）の単位**なので、
    3文入りのターンで1文だけ別人になっても、その文だけ引き直せる。
    合成後に ``schedule_clips`` で**重ならないよう直列化**し、ドリフト（元の発話位置
    からのずれ）を報告する。全クリップが揃うと ``voice_tts_report.json`` を書く。
    """
    from wwedit.compose.ffmpeg_compose import _src_to_out, out_total
    from wwedit.publish.qwen_tts import synth_batch
    from wwedit.publish.voice_tts import (
        DECISIONS_NAME,
        REPORT_NAME,
        load_decisions,
        schedule_clips,
        tts_clips,
        tts_units,
        wav_duration,
    )

    edl = load_edl(edl_path)
    dec_path = edl_path.parent / DECISIONS_NAME
    if not dec_path.exists():
        raise typer.BadParameter(f"{dec_path} が無い（voice-scripter スキルを先に）")
    decisions = load_decisions(dec_path)
    work = edl_path.parent / "voice" / "tts"
    work.mkdir(parents=True, exist_ok=True)

    ranges = edl.kept_ranges()
    total = out_total(ranges)
    units = tts_units(edl)
    slots: dict[int, tuple[float, float]] = {}
    for k, un in enumerate(units):
        os_ = _src_to_out(ranges, un["start"])
        oe = _src_to_out(ranges, un["end"])
        nxt = _src_to_out(ranges, units[k + 1]["start"]) if k + 1 < len(units) else total
        slots[un["uid"]] = (max(0.0, oe - os_), max(0.0, nxt - oe))

    missing = [u["uid"] for u in units if u["uid"] not in decisions]
    if missing:
        rprint(f"[yellow]決定が無いターン {len(missing)} 件は元テキストで合成する[/]")
    dropped = sum(1 for u in units if decisions.get(u["uid"], "x") == "")
    if dropped:
        rprint(f"[dim]読み上げないターン {dropped} 件（スキルが隣へまとめた）[/]")

    # 合成台帳: テキストが変わった wav は作り直す（短縮リトライ対応）。
    # ランナーが1本ごとに書く `u####.txt` サイドカーが正で、synth_texts.json は
    # バッチ完走時のまとめ（サイドカー導入前の既存分もこちらで拾える）。
    ledger_path = work / "synth_texts.json"
    ledger: dict[str, str] = (
        json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {})

    def _synthesized_text(out_wav: Path, key: str) -> str | None:
        side = out_wav.with_suffix(".txt")
        if side.exists():
            return side.read_text(encoding="utf-8")
        return ledger.get(key)

    # **合成の単位は「文」**（空文字のターンは読み上げない・キー無しは元テキスト）。
    clips = tts_clips(units, decisions)
    _warn_long_sentences(clips)
    jobs: list[dict] = []
    for c in clips:
        text = c["text"]
        out_wav = work / c["wav"]
        if out_wav.exists() and _synthesized_text(out_wav, c["key"]) == text:
            continue
        # 文は短いので、尺ヒントは**読み上げ文の文字数**から見積もる（実測0.134秒/字）。
        # 枠(slot+gap)から取ると相槌1つに30秒のヒントが付いて生成が遅くなる。
        dur_hint = min(30.0, max(2.0, len(text) * 0.134 * 1.6))
        jobs.append({"text": text, "out": str(out_wav),
                     "char": edl.character_cast.get(c["speaker"], "noa"),
                     "seed": seed, "dur": dur_hint, "_idx": c["key"]})
    if jobs:
        # 0 = 全部。**分けるほど遅くなる**（1実行につきモデル読み込みが約105秒。
        # 実測: 120本を60本ずつ分けたら、合成37.8分に対し読み込みが21分ぶん乗った）。
        # 再開はクリップ横の `u####.txt` サイドカーで効くので、分割しなくても安全。
        batch = jobs[:max_jobs] if max_jobs > 0 else jobs
        rprint(f"[cyan]Qwen3-TTS 合成[/]: {len(batch)} 件（未合成 {len(jobs)} 件）…")
        sim_report: list[dict] = []
        try:
            synth_batch([{k: v for k, v in j.items() if k != "_idx"} for j in batch],
                        report=sim_report)
        except (RuntimeError, FileNotFoundError) as e:
            rprint(f"[red]{e}[/]")
            raise typer.Exit(1) from e
        # 見直し用TSVは**決定JSONと同じ場所**へ（voice/ ではなく EDL の隣）
        _report_speaker_sim(sim_report, batch, edl_path.parent)
        for j in batch:
            ledger[j["_idx"]] = j["text"]
        ledger_path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        if len(jobs) > len(batch):
            rprint(f"[yellow]残り {len(jobs) - len(batch)} 件 → 同コマンドを再実行[/]")
            return

    # クリップを**直列にスケジュール**する（元の発話位置を希望位置として、重なりを解消）
    durs: dict[int, float] = {}
    for i, c in enumerate(clips):
        out_wav = work / c["wav"]
        if not out_wav.exists():
            rprint(f"[red]未合成のまま: {out_wav.name}[/]")
            raise typer.Exit(1)
        durs[i] = wav_duration(out_wav)

    # 同じターンを割った文には、**元発話の区間を読み上げ実尺の比で按分**して配る。
    # 映像側（`timewarp.anchors_with_rows`）は行ごとに src 区間を見るので、3文が全部
    # 同じ区間を指すとアンカーが重なって速度計画が壊れる。
    src_span = _split_turn_spans(clips, durs, ranges)
    items = [(_src_to_out(ranges, src_span[i][0]), durs[i], i) for i in range(len(clips))]
    want_by_i = {k: w for w, d, k in items}
    # **PCシステム音が鳴っている区間は詰めない**（デモの音がそのまま鳴っているので、
    # 間を 0.15 に縮めると音が途切れる。映像側も等速固定になる区間）。
    holds = _reading_hold_spans(edl, edl_path, ranges)
    src_ends = {i: _src_to_out(ranges, src_span[i][1]) for i in range(len(clips))}

    rows: list[dict] = []
    for start, dur, i in schedule_clips(items, hold_spans=holds, src_ends=src_ends):
        c = clips[i]
        slot, gap = slots[c["uid"]]
        rows.append({
            "idx": c["uid"], "sub": c["sub"], "speaker": c["speaker"], "u_idx": c["u_idx"],
            "text": c["text"],
            "src_start": round(src_span[i][0], 3), "src_end": round(src_span[i][1], 3),
            "slot": round(slot, 3), "gap": round(gap, 3),
            "tts_s": round(dur, 3), "out_want": round(want_by_i[i], 3),
            "out_start": round(start, 3), "drift": round(start - want_by_i[i], 3),
            "clip": str(work / c["wav"]),
        })

    drifts = sorted((r["drift"] for r in rows), reverse=True)
    end_at = max((r["out_start"] + r["tts_s"] for r in rows), default=0.0)
    report_path = edl_path.parent / REPORT_NAME
    report_path.write_text(
        json.dumps({"rows": rows, "out_total": round(total, 3),
                    "scheduled_end": round(end_at, 3)}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    rprint(f"[green]スケジュール確定[/]: {len(rows)}クリップ / 読み上げ計 "
           f"{sum(r['tts_s'] for r in rows):.0f}秒（出力尺 {total:.0f}秒）→ {report_path}")
    rprint(f"[dim]ドリフト: 最大 {drifts[0]:.1f}秒 / 中央値 "
           f"{drifts[len(drifts) // 2]:.1f}秒[/]")
    if end_at > total:
        rprint(f"[yellow]読み上げが出力尺を {end_at - total:.1f}秒 超過[/]"
               "（finalize が末尾にフリーズを1つ入れて吸収する）")
    rprint("[dim]次: publish voice-tts-finalize[/]")


def _warn_long_sentences(clips: list[dict]) -> None:
    """**60字を超える1文**を警告する（合成の単位が文なので、そこだけ粒度が粗くなる）。

    止めはしない（合成そのものはできる）。voice-scripter に「。」を打たせる合図。
    """
    from wwedit.publish.voice_tts import SENT_MAX_CHARS, long_sentences

    longs = long_sentences(clips)
    if not longs:
        return
    rprint(f"[yellow]長すぎる1文 {len(longs)}件[/]（{SENT_MAX_CHARS}字超・"
           "そこだけ粒度がターン単位に戻る。voice-scripter で「。」を打つ）:")
    for c in sorted(longs, key=lambda x: -len(x["text"]))[:5]:
        rprint(f"  [dim]{c['key']:>6}[/] {len(c['text'])}字  {c['text'][:36]}…")


def _split_turn_spans(
    clips: list[dict], durs: dict[int, float], ranges,
) -> dict[int, tuple[float, float]]:
    """ターンの元発話区間を、**その中の文へ読み上げ実尺の比で按分**する。

    返り値は ``{clips の添字: (src開始, src終了)}``（ソース秒）。按分は出力座標で
    行ってからソース秒へ戻す（ターンの途中にカット穴があっても比率が狂わない）。

    これをやらないと、同じターンを割った文が全部**同じ src 区間**を指し、
    `timewarp.anchors_with_rows` のアンカーが重なって映像の速度計画が壊れる。
    """
    from wwedit.compose.ffmpeg_compose import _src_to_out, out_to_src

    by_uid: dict[int, list[int]] = {}
    for i, c in enumerate(clips):
        by_uid.setdefault(c["uid"], []).append(i)
    out: dict[int, tuple[float, float]] = {}
    for idxs in by_uid.values():
        c0 = clips[idxs[0]]
        if len(idxs) == 1:
            out[idxs[0]] = (float(c0["start"]), float(c0["end"]))
            continue
        o_s, o_e = _src_to_out(ranges, c0["start"]), _src_to_out(ranges, c0["end"])
        total_d = sum(durs[i] for i in idxs) or 1.0
        pos = o_s
        for k, i in enumerate(idxs):
            nxt = o_e if k == len(idxs) - 1 else pos + (o_e - o_s) * (durs[i] / total_d)
            out[i] = (out_to_src(ranges, pos), out_to_src(ranges, max(pos, nxt)))
            pos = nxt
    return out


def _reading_hold_spans(edl, edl_path: Path, ranges) -> list[tuple[float, float]]:
    """**PCシステム音が鳴っている src' 区間**（＝読み上げの間を詰めない所）。

    デモの音がそのまま鳴っている区間で間を 0.15 秒に縮めると音が途切れる。
    映像側（`timewarp` の ``hold_spans``）と**同じ区間**を使う。
    """
    from wwedit.compose.cli import _desktop_spans
    from wwedit.compose.speedup import merge_spans, src_spans_to_out

    return merge_spans(src_spans_to_out(
        _desktop_spans(edl, edl_path, refresh=False), ranges, edl.freezes))


def _report_speaker_sim(report: list[dict], batch: list[dict], data_dir: Path) -> None:
    """話者同一性の結果をまとめ、**まだ別人の行だけ**を台本見直しへ回す。

    シードと参照セットを振っても閾値を割り続ける行は、たいてい**文そのもの**が原因。
    棒読みの参照音に対して「まじか！」のような強い感情表現を当てるとスコアが落ちる
    （ユーザー指摘）。落ち着いた言い回しへ書き直させるため
    ``voice_tts_recheck.tsv`` を出す → voice-scripter（見直しモード）→ voice-tts 再実行。
    """
    from wwedit.publish.voice_tts import RECHECK_NAME, SPEAKER_SIM_NAME

    if not report:
        return
    by_out = {str(j["out"]): j for j in batch}
    ng = [r for r in report if not r.get("sim_ok", True)]
    redone = [r for r in report if r.get("tries", 1) > 1]
    rprint(f"[dim]話者チェック[/]: {len(report)}件中 引き直し {len(redone)}件 / "
           f"それでも別人 {len(ng)}件")
    # **合格した行も含めて全部残す**（分割実行なので前のパスの結果に上書き合流する）。
    sim_path = data_dir / SPEAKER_SIM_NAME
    kept: dict[str, dict] = {}
    if sim_path.exists():
        with contextlib.suppress(json.JSONDecodeError):
            kept = {str(r.get("clip") or r.get("out")): r
                    for r in json.loads(sim_path.read_text(encoding="utf-8"))}
    for r in report:
        j = by_out.get(str(r["out"]), {})
        kept[Path(r["out"]).name] = {
            "clip": Path(r["out"]).name, "idx": j.get("_idx"), "char": j.get("char"),
            "sim": r.get("sim"), "sim_min": r.get("sim_min"),
            "sim_min_at": r.get("sim_min_at"), "n_win": r.get("n_win"),
            "octave": r.get("octave"), "tries": r.get("tries"),
            "ok": bool(r.get("sim_ok", True)),
        }
    def _idx_key(r: dict) -> tuple:
        """``"17"`` / ``"17.1"``（ターン.文）を数値順に並べる。文字列順だと 10 < 2 になる。"""
        k = r.get("idx")
        if k is None:
            return (1, 0, 0)
        uid, _, sub = str(k).partition(".")
        return (0, int(uid) if uid.lstrip("-").isdigit() else 0, int(sub) if sub.isdigit() else 0)

    sim_path.write_text(
        json.dumps(sorted(kept.values(), key=_idx_key), ensure_ascii=False, indent=1),
        encoding="utf-8")
    path = data_dir / RECHECK_NAME
    if not ng:
        path.unlink(missing_ok=True)
        return
    lines = ["idx\tchar\tsim\tsim_min\tat\ttext"]
    for r in sorted(ng, key=lambda x: min(x["sim"], x.get("sim_min", 1.0))):
        j = by_out.get(str(r["out"]), {})
        lines.append(f"{j.get('_idx', '')}\t{j.get('char', '')}\t"
                     f"{r['sim']:.3f}\t{r.get('sim_min', 1.0):.3f}\t"
                     f"{r.get('sim_min_at', 0.0):.1f}\t{j.get('text', '')}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    rprint(f"[yellow]台本の見直しが要る行[/]: {len(ng)}件 → {path}\n"
           "[dim]  voice-scripter を見直しモードで実行 → voice-tts を再実行[/]")


@publish_app.command(name="voice-tts-finalize")
def voice_tts_finalize(
    edl_path: Path = typer.Argument(..., help="対象 EDL（voice-tts 済み）"),
    subtitles_only: bool = typer.Option(
        False, "--subtitles-only",
        help="字幕と口パク区間だけ貼り直す（**音声トラックを作り直さない**）。"
             "読み上げ文や用語表記だけ直した時に使う"),
) -> None:
    """[V] 方式B: freezes を確定し、σタイムライン上に全長トラックを組み立てて voice_path をセット。

    全長トラックは話者ごとに約100MBあり、組み立てに数十秒かかる。**テキストだけ直した時は
    `--subtitles-only`** を使う（音声は既にできているので作り直す必要がない）。
    """
    from wwedit.compose.ffmpeg_compose import out_total, stretch_time
    from wwedit.edl.schema import Freeze, save_edl
    from wwedit.publish.voice_convert import assemble_track, measure_loudness
    from wwedit.publish.voice_tts import (
        DECISIONS_NAME,
        REPORT_NAME,
        TERMS_NAME,
        load_decisions,
        load_terms,
        out_to_sigma_segments,
        place_clip,
        subtitles_from_reading,
        wav_duration,
    )

    edl = load_edl(edl_path)
    report_path = edl_path.parent / REPORT_NAME
    if not report_path.exists():
        raise typer.BadParameter(f"{report_path} が無い（先に publish voice-tts）")
    rows = json.loads(report_path.read_text(encoding="utf-8"))["rows"]
    ranges = edl.kept_ranges()

    # freezes: 直列スケジュールが出力尺を超えた分だけ**末尾に1つ**入れて吸収する
    # （ターン単位にしてからは per-発話のフリーズは不要になった）。
    total = out_total(ranges)
    end_at = max((r["out_start"] + r["tts_s"] for r in rows), default=0.0)
    freezes: list[Freeze] = []
    if end_at > total + 1e-3 and ranges:
        freezes.append(Freeze(at=round(ranges[-1].end - 0.01, 3),
                              extra=round(end_at - total, 3), note="tts-overflow"))
    edl.freezes = freezes

    # スケジュール済みの出力秒へクリップを配置（カット穴・フリーズを跨ぐ分は分割）
    segs = out_to_sigma_segments(ranges, freezes)
    placements_by_speaker: dict[str, list[tuple]] = {}
    clips_meta: list[dict] = []
    for r in rows:
        clip = Path(r["clip"])
        clip_dur = wav_duration(clip)
        out_at = float(r["out_start"])
        for off, sigma_pos, dur in place_clip(segs, out_at, clip_dur):
            placements_by_speaker.setdefault(r["speaker"], []).append(
                (sigma_pos, clip, dur, off))
        clips_meta.append({"speaker": r["speaker"], "out_start": round(out_at, 3),
                           "out_end": round(out_at + clip_dur, 3)})

    total_extra = sum(f.extra for f in freezes)
    sigma_total = (edl.source.duration_s or 0.0) + total_extra

    # マイクトラック: 話者の先頭トラックへ TTS 配置、同話者の2本目以降は無音化
    # （--subtitles-only では組み立てを飛ばす＝約100MB×人数の書き出しを省く）
    # normalize=True: Qwen3-TTS の出力レベルは話者ごとにばらつくので、収録音と同じ基準
    # (-16 LUFS/TP-1.5)へ揃える。無音トラックには掛けない（掛けても意味が無い）。
    seen_speaker: set[str] = set()
    for track in edl.source.audio_tracks:
        if track.is_desktop_audio:
            continue
        out_wav = edl_path.parent / "voice" / f"{track.speaker}_tts.wav"
        note = ""
        if track.speaker in seen_speaker:
            out_wav = edl_path.parent / "voice" / f"{track.speaker}_tts_silence.wav"
            if not subtitles_only:
                assemble_track([], sigma_total, out_wav)
        else:
            if not subtitles_only:
                assemble_track(placements_by_speaker.get(track.speaker, []),
                               sigma_total, out_wav, normalize=True)
                loud = measure_loudness(out_wav)
                note = f"（{loud['input_i']:.2f} LUFS / TP {loud['input_tp']:.2f}dB）"
            seen_speaker.add(track.speaker)
        if not out_wav.exists():
            raise typer.BadParameter(
                f"{out_wav.name} が無い（--subtitles-only は finalize 済みの時だけ）")
        track.voice_path = str(out_wav)
        rprint(f"  {track.speaker} → {Path(out_wav).name}"
               + ("（据え置き）" if subtitles_only else note))

    # PCシステム音声: フリーズ位置に無音を挿入した σ 版を作る（フリーズ無しなら元のまま）
    if freezes and not subtitles_only:
        dur = edl.source.duration_s or 0.0
        ats = [f.at for f in freezes]
        bounds = [0.0, *ats, dur]
        for track in edl.source.audio_tracks:
            if not track.is_desktop_audio:
                continue
            placements = []
            for a, b in zip(bounds, bounds[1:], strict=False):
                if b - a <= 1e-3:
                    continue
                placements.append((stretch_time(a + 1e-9, freezes), track.path, b - a, a))
            out_wav = edl_path.parent / "voice" / f"desktop_{Path(track.path).stem}_sigma.wav"
            assemble_track(placements, sigma_total, out_wav)
            track.voice_path = str(out_wav)
            rprint(f"  {track.speaker}(PC音声) → {Path(out_wav).name}（フリーズ無音挿入）")

    # 字幕を**読み上げ文そのまま**の2行字幕へ差し替える（方式Bは発話内容が確定しているので
    # Whisper由来の字幕を使う理由がない）。元の字幕は初回だけ meta へ退避＝voice-revert で戻る。
    dec_path = edl_path.parent / DECISIONS_NAME
    if dec_path.exists():
        decisions = load_decisions(dec_path)
        terms = load_terms(edl_path.parent / TERMS_NAME)
        if terms:
            rprint(f"  用語表記 {len(terms)}件を字幕へ適用（読み上げはカタカナのまま）")
        subs = subtitles_from_reading(rows, decisions, ranges, freezes, terms=terms)
        if subs:
            vmeta = edl.meta.setdefault("voice", {})
            if "prev_subtitles" not in vmeta:
                vmeta["prev_subtitles"] = [s.model_dump() for s in edl.subtitles]
            edl.subtitles = subs
            rprint(f"  字幕 → 読み上げ文の2行字幕 {len(subs)}件に差し替え")

    # ちびキャラの口パク・感情用に、**実際に音が鳴っている出力区間**を EDL へ残す。
    # 方式Bは元音声のタイミングと一致しないので、Whisper word 由来の口パクを使うと
    # 声と口が全く合わない（実走で発覚）。compose 側はここを見る。
    vmeta = edl.meta.setdefault("voice", {})
    vmeta["clips"] = clips_meta
    rprint(f"  口パク用クリップ区間 {len(clips_meta)}件を meta.voice.clips に記録")

    save_edl(edl, edl_path)
    rprint(f"[green]voice-tts-finalize 完了[/]（freeze {len(freezes)}件・"
           f"出力尺 +{total_extra:.1f}s）。compose がそのまま反映する")


@publish_app.command(name="character-image")
def character_image(
    char: str = typer.Option("noa", help="キャラID（<id>_a*.webp を参照に同一性維持）"),
    situation: str = typer.Option(
        ..., help="変える点＝季節/服装/シチュ（英語prompt断片）。重複回避は intro-builder 側"),
    out: Path = typer.Option(..., help="出力 png"),
    model: str = typer.Option("gemini-3-pro-image", help="nano banana2"),
) -> None:
    """[G] イントロ開始フレーム生成（**決定的**・参照画像＋同一性維持制約＋リップシンク構図）。

    「どんな服装/シチュにするか（過去と非重複・季節合わせ）」の創作は呼び出し側が situation で渡す。
    """
    from wwedit.publish.character import generate_character_image

    p = generate_character_image(char, situation, out, model=model)
    rprint(f"[green]開始フレーム[/]: {p}（{char} 参照＋同一性維持）")


@publish_app.command()
def lipsync(
    image: Path = typer.Option(..., help="開始フレーム png"),
    audio: Path = typer.Option(..., help="音声 wav"),
    out: Path = typer.Option(..., help="出力 mp4（720p/16:9）"),
    seconds: int = typer.Option(0, help="尺(秒)。0=音声尺を切り上げ。**高コスト$0.06/秒**"),
    char: str = typer.Option(
        "", help="キャラID。表情を mascot.md 準拠にする（未指定は中立。笑顔を強制しない）"),
    prompt: str = typer.Option("", help="表情指示（明示するとキャラ既定を上書き）"),
) -> None:
    """[G] DomoAI talking-avatar で開始フレーム＋音声→リップシンク動画（**決定的・外部API課金**）。

    seconds 既定=音声尺の切り上げ。出来の良し悪し（目元/口元）の判断は呼び出し側で目視QA。
    表情は `--char` の設定（mascot.md）に従う＝**全キャラ一律の笑顔にしない**（キャラ崩れ防止）。
    """
    import math
    import subprocess

    from wwedit.publish.character import expression_of
    from wwedit.publish.domoai import generate_talking_avatar

    if seconds <= 0:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", str(audio)], capture_output=True, text=True)
        seconds = max(1, min(60, math.ceil(float(r.stdout.strip() or 1))))
    if not prompt:
        prompt = f"natural talking expression, {expression_of(char)}"
    rprint(f"[dim]DomoAI 生成中（seconds={seconds}・約${seconds * 0.06:.2f}・表情={prompt}）...[/]")
    try:
        p = generate_talking_avatar(image, audio, out, seconds=seconds,
                                    aspect_ratio="16:9", prompt=prompt)
    except (RuntimeError, FileNotFoundError) as e:
        rprint(f"[red]DomoAI 失敗[/]: {e}")
        raise typer.Exit(1) from e
    rprint(f"[green]リップシンク[/]: {p}（seconds={seconds}）")


@publish_app.command(name="intro-compose")
def intro_compose(
    video: Path = typer.Option(..., help="DomoAI リップシンク動画(720p)"),
    script: str = typer.Option(..., help="台本全文（ピンク二重枠字幕で焼く）"),
    out: Path = typer.Option(..., help="出力 mp4（FullHD完成イントロ）"),
    char: str = typer.Option("noa", help="キャラID（本名フルネームをmascot.mdから解決）"),
    name: str = typer.Option("", help="表示名の明示上書き（空=charの本名フルネーム）"),
    jingle: Path = typer.Option(None, help="ジングル音源（選曲は呼び出し側＝スキルの判断）"),
) -> None:
    """[G] イントロ仕上げ合成（**決定的**）: 720p→FullHD＋右上ロゴ/本名＋ピンク字幕＋ジングル。

    キャラ名は本名フルネームを**右上**に表示（mascot.md 由来）。台本/ジングル選曲はスキル判断。
    """
    from wwedit.publish.character import full_name
    from wwedit.publish.intro_compose import compose_intro
    from wwedit.subtitle.ass import intro_color_for

    disp = name or full_name(char)
    color = intro_color_for(char)
    try:
        p = compose_intro(video, script, out, name=disp,
                          jingle=str(jingle) if jingle else None,
                          subtitle_color=color)
    except RuntimeError as e:
        rprint(f"[red]{e}[/]")
        raise typer.Exit(1) from e
    rprint(f"[green]イントロ完成[/]: {p}（FullHD＋右上ロゴ/{disp}＋{char}の配色の字幕＋ジングル）")


@publish_app.command(name="concat-intro")
def concat_intro(
    intro: Path = typer.Option(..., help="完成イントロ mp4（intro_final.mp4）"),
    main: Path = typer.Option(..., help="本編 mp4（アイキャッチ挿入済み *_ec.mp4）"),
    out: Path = typer.Option(..., help="出力 mp4（final.mp4）"),
    chapter_lines_file: Path = typer.Option(
        None, help="章行テキスト（*_ec_chapters.txt）。**イントロ尺ぶんずらして書き出す**"),
    chapters_out: Path = typer.Option(
        None, help="ずらした章行の出力（既定 <out親>/final_chapters.txt）"),
) -> None:
    """[G] イントロを本編の先頭へ連結し、**章時刻をイントロ尺ぶんずらす**。

    イントロと本編は fps/音声規格が違うので、イントロ側を本編に合わせて再エンコードしてから
    連結する。章をずらし忘れると概要欄の章が全部早くなる（#100 で実際に起きた）。
    """
    from wwedit.publish.concat import prepend_intro, shift_chapter_lines

    try:
        p, dur = prepend_intro(intro, main, out)
    except RuntimeError as e:
        rprint(f"[red]{e}[/]")
        raise typer.Exit(1) from e
    rprint(f"[green]連結[/]: {p}（イントロ {dur:.2f}s ＋ 本編）")

    if chapter_lines_file:
        lines = chapter_lines_file.read_text(encoding="utf-8").splitlines()
        shifted = shift_chapter_lines(lines, dur)
        dest = chapters_out or (out.parent / "final_chapters.txt")
        dest.write_text("\n".join(shifted) + "\n", encoding="utf-8")
        rprint(f"[green]章行[/]: {dest}（+{int(dur)}秒シフト・先頭は00:00固定）")


@publish_app.command()
def eyecatch(
    title: str = typer.Option(..., help="アイキャッチ中央に出すタイトル（章名等）"),
    out: Path = typer.Option(..., help="出力 mp4（2秒・generative art＋ジングル）"),
    seed: int = typer.Option(0, help="見た目を決める seed（章ごとに変える＝呼び出し側）"),
    jingle: Path = typer.Option(None, help="ジングル音源（直接指定）"),
    jingle_dir: Path = typer.Option(
        None, help="ジングル群のディレクトリ（seed でランダム選曲）"),
    duration: float = typer.Option(2.0, help="尺(秒)"),
) -> None:
    """[H] チャプター冒頭アイキャッチ生成（**決定的**・generative art＋ランダムジングル）。

    ビジュアルは ffmpeg 生成フィルタ（seed で毎回変化・curated 配色）。`--jingle-dir` 指定時は
    seed でその中から1曲を選ぶ。選曲方針/seed の割り当ては呼び出し側（compose/スキル）の判断。
    """
    import random

    from wwedit.publish.eyecatch import generate_eyecatch

    jpath = jingle
    if jpath is None and jingle_dir and jingle_dir.exists():
        cands = sorted(
            p for p in jingle_dir.rglob("*")
            if p.suffix.lower() in (".wav", ".mp3", ".m4a", ".flac", ".ogg"))
        if cands:
            jpath = random.Random(seed).choice(cands)
    try:
        p = generate_eyecatch(title, out, seed=seed,
                              jingle=str(jpath) if jpath else None, duration=duration)
    except RuntimeError as e:
        rprint(f"[red]{e}[/]")
        raise typer.Exit(1) from e
    jn = jpath.name if jpath else "（無音）"
    rprint(f"[green]アイキャッチ[/]: {p}（seed={seed} {duration}s ジングル={jn}）")


@publish_app.command()
def youtube(
    edl_path: Path = typer.Argument(..., help="対象 EDL"),
    video: Path = typer.Option(..., help="アップロードする mp4"),
    title_file: Path = typer.Option(None, help="タイトル（既定 <date>/yt_title.txt）"),
    desc_file: Path = typer.Option(None, help="概要欄（既定 <date>/youtube_description.txt）"),
    privacy: str = typer.Option("private", help="private(既定・下書相当)/unlisted/public"),
    tags: str = typer.Option(
        "", help="タグをカンマ区切りで明示（既定＝概要欄の#ハッシュタグ行から起こす）"),
    no_tags: bool = typer.Option(False, "--no-tags", help="タグを付けない"),
    thumbnail_file: Path = typer.Option(
        None, help="投稿後に設定するサムネ（既定 <date>/thumbnail.png があれば自動）"),
    no_thumbnail: bool = typer.Option(
        False, "--no-thumbnail", help="サムネを設定しない（YouTube Studio で手動設定する）"),
    dry_run: bool = typer.Option(
        True, help="既定True＝本体JSONを書くだけ（キー不要・検証用）。--no-dry-run で実投稿"
    ),
    allow_invalid_chapters: bool = typer.Option(
        False, "--allow-invalid-chapters",
        help="章がYouTubeの条件を満たさなくても投稿する（既定は弾く）"),
) -> None:
    """[K] 動画を YouTube へ投稿（既定 dry-run＝メタデータ検証のみ・キー不要）。

    タイトル/概要欄は事前生成物（`publish description`）を使う。**タグは概要欄のハッシュタグ行
    から起こす**（内容に合わないタグが付かないように・`--tags`/`--no-tags` で上書き可）。
    実投稿は .env の WWEDIT_YT_* と google-api-python-client が要る（無ければ手順を示して停止）。
    """
    from wwedit.publish.youtube import build_video_resource

    edl_dir = edl_path.parent
    tfile = title_file or (edl_dir / "yt_title.txt")
    dfile = desc_file or (edl_dir / "youtube_description.txt")
    if not dfile.exists():
        rprint(f"[red]概要欄がありません: {dfile}（先に publish description）[/]")
        raise typer.Exit(1)
    title = tfile.read_text(encoding="utf-8").strip() if tfile.exists() else "わくわくべんきょ会"
    desc = dfile.read_text(encoding="utf-8")
    # 概要欄は生成後に手で直されることがある。**投稿直前がチャプター条件の最後の砦**。
    _check_chapters(desc, allow_invalid=allow_invalid_chapters)
    tag_list: list[str] | None = None
    if no_tags:
        tag_list = []
    elif tags.strip():
        tag_list = [t.strip().lstrip("#") for t in tags.split(",") if t.strip()]
    body = build_video_resource(title, desc, tags=tag_list, privacy=privacy)

    if dry_run:
        import json

        req_path = edl_dir / "youtube_upload_request.json"
        req_path.write_text(
            json.dumps({"body": body, "video": str(video)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        rprint(
            f"[green]dry-run[/]: 投稿リクエストを検証・保存 → {req_path}\n"
            f"  title={body['snippet']['title']!r} privacy={body['status']['privacyStatus']} "
            f"desc {len(desc)}字 / video={video.name}\n"
            f"  tags={body['snippet']['tags']}\n"
            "  実投稿は [cyan]--no-dry-run[/]（.env の WWEDIT_YT_* ＋ google-api-python-client 要）"
        )
        return

    from wwedit.publish.youtube import upload_video

    if not video.exists():
        rprint(f"[red]動画がありません: {video}[/]")
        raise typer.Exit(1)
    rprint(f"[dim]YouTube へアップロード中（privacy={privacy}）...[/]")
    try:
        resp = upload_video(str(video), body)
    except (RuntimeError, FileNotFoundError) as e:
        rprint(f"[red]投稿不可[/]: {e}")
        raise typer.Exit(1) from e
    vid = resp.get("id", "?")
    rprint(f"[green]投稿完了[/]: https://youtu.be/{vid} (privacy={privacy})")

    thumb = thumbnail_file or (edl_dir / "thumbnail.png")
    if no_thumbnail or not thumb.exists():
        return
    from wwedit.publish.youtube import set_thumbnail

    try:
        set_thumbnail(vid, str(thumb))
    except Exception as e:  # サムネ失敗で投稿自体を無かったことにはしない
        rprint(f"[yellow]サムネ設定に失敗[/]: {e}\n"
               f"  → YouTube Studio で {thumb} を手動設定してください")
        return
    rprint(f"[green]サムネ設定[/]: {thumb.name}")


@publish_app.command("set-thumbnail")
def set_thumbnail_cmd(
    video_id: str = typer.Argument(..., help="対象の YouTube 動画ID（URL末尾）"),
    image: Path = typer.Option(..., help="設定するサムネ画像（2MB超は自動でJPEG縮小）"),
) -> None:
    """投稿済み動画のサムネを差し替える（`thumbnails.set`）。"""
    from wwedit.publish.youtube import set_thumbnail

    if not image.exists():
        rprint(f"[red]画像がありません: {image}[/]")
        raise typer.Exit(1)
    try:
        set_thumbnail(video_id, str(image))
    except (RuntimeError, FileNotFoundError) as e:
        rprint(f"[red]設定不可[/]: {e}")
        raise typer.Exit(1) from e
    rprint(f"[green]サムネ設定[/]: https://youtu.be/{video_id} ← {image.name}")
