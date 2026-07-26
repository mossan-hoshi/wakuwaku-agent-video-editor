"""``wwedit publish`` サブコマンド（[M4] 投稿パッケージ）。"""

from __future__ import annotations

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
    hashtags: str = typer.Option("", help="ハッシュタグ行（例 '#個人開発 #生成ai #aicoding'）"),
    links_file: Path = typer.Option(
        None, help="関連リンク（各行 'ラベル<TAB>URL'。タブが無ければ最後の空白で分割）"),
    out: Path = typer.Option(None, help="出力（既定 data/<date>/youtube_description.txt）"),
    post_unit_index: int = typer.Option(
        -1, help="投稿単位[K]。その単位の章で概要欄を作る（-1=収録まるごと）"),
    chapter_lines_file: Path = typer.Option(
        None, help="章行を明示上書き（[H]アイキャッチ挿入時の補正章行 *_ec_chapters.txt）"),
) -> None:
    """YouTube 概要欄を**チャンネル実フォーマット**で組み立てる。

    形式: ``Agenda「テーマ」`` → 関連リンク(任意) → ``#タグ`` → ``00:00 - start`` 以下の
    タイムスタンプ。要約/AI免責/チャンネルURL/タイトル再掲は**入れない**（実投稿に無い）。
    タイトルは動画 snippet 側で別途付ける。`--post-unit-index N` で単位内の章・時刻。
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

    text = build_description(edl, agenda=agenda_text, links=links or None,
                             hashtags=hashtags or None, chapter_lines=ch_lines)
    default = (
        f"youtube_description_p{post_unit_index}.txt" if post_unit_index >= 0
        else "youtube_description.txt")
    out_path = out or (edl_path.parent / default)
    out_path.write_text(text, encoding="utf-8")
    n_ch = len(ch_lines) if ch_lines is not None else len(edl.chapters or [])
    rprint(f"[green]概要欄[/]: {out_path}（Agenda/{'リンク' if links else '—'}/"
           f"{'#タグ' if hashtags else '—'}/タイムスタンプ{n_ch}）")


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
) -> None:
    """[L] サムネ生成（**nano banana 2 一発生成**）。

    キャラ・背景・**日本語タイトル文字まで一括でモデルが描く**。``--char`` の立ち姿
    ``<id>_a*.webp`` を参照に絵柄/キャラ同一性を固定。旧来の「背景だけ生成＋PIL帯合成」は廃止。
    """
    from wwedit.publish.thumbnail import generate_thumbnail

    out_path = out or (edl_path.parent / "thumbnail.png")
    rprint(f"[dim]サムネ一発生成中（{model}・参照={char or 'なし'}・課金あり）...[/]")
    generate_thumbnail(prompt, out_path, char=char or None, model=model)
    rprint(f"[green]サムネ[/]: {out_path}（nano banana 2 一発生成・文字込み）")


@publish_app.command()
def tts(
    text: str = typer.Option(..., help="読み上げテキスト（内容/尺の判断はスキル側）"),
    out: Path = typer.Option(..., help="出力 wav（44100/mono）"),
    voice: str = typer.Option("noa", help="AIVISキャラ（noa 等）"),
    style: str = typer.Option("", help="スタイル名（空=キャラ既定の normal）"),
    synth_url: str = typer.Option("http://127.0.0.1:8123", help="SBV2 合成サーバ"),
) -> None:
    """[G] AIVis(SBV2)で音声合成し wav 保存（**決定的**・尺を表示）。

    SBV2 サーバ起動済み前提（未起動なら手順を出して停止）。尺が長い等の判断は呼び出し側。
    """
    from wwedit.publish.aivis import synth_to_file

    try:
        dur = synth_to_file(text, out, voice=voice, style=style or None, synth_url=synth_url)
    except RuntimeError as e:
        rprint(f"[red]{e}[/]")
        raise typer.Exit(1) from e
    flag = "OK(<=10s)" if dur <= 10.0 else "[yellow]>10s（イントロは詰める）[/]"
    rprint(f"[green]音声[/]: {out}  実尺={dur:.2f}s {flag}  voice={voice}")


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

    disp = name or full_name(char)
    try:
        p = compose_intro(video, script, out, name=disp,
                          jingle=str(jingle) if jingle else None)
    except RuntimeError as e:
        rprint(f"[red]{e}[/]")
        raise typer.Exit(1) from e
    rprint(f"[green]イントロ完成[/]: {p}（FullHD＋右上ロゴ/{disp}＋ピンク字幕＋ジングル）")


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
