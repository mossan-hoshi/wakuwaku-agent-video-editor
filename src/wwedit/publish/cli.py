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
    title: str = typer.Option("", help="動画タイトル（未指定は --title-file か仮見出し）"),
    title_file: Path = typer.Option(None, help="タイトルを書いたテキスト（LLM生成等）"),
    summary_file: Path = typer.Option(None, help="本文要約テキスト（LLM生成・複数行可）"),
    out: Path = typer.Option(None, help="出力（既定 data/<date>/youtube_description.txt）"),
    post_unit_index: int = typer.Option(
        -1, help="投稿単位[K]。その単位の章で概要欄を作る（-1=収録まるごと）"),
) -> None:
    """YouTube 概要欄テキストを組み立てる（タイトル＋要約＋チャプター＋定型フッター）。

    タイトル/要約は内容依存なので **LLM で別途生成して** `--title-file`/`--summary-file` で渡す
    （コスト規律：本文を主ループに載せない）。本コマンドは EDL のチャプターと合体させるだけ。
    `--post-unit-index N` で1収録の N 番目の投稿の概要欄（単位内の章・時刻）。
    """
    from wwedit.publish.description import build_description

    edl = load_edl(edl_path)
    if not edl.chapters:
        rprint("[yellow]chapters がありません（先に chapter prepare/apply）[/]")
    ch_lines = None
    if post_unit_index >= 0:
        from wwedit.edl.postunit import post_unit_chapter_lines

        ch_lines = post_unit_chapter_lines(edl, post_unit_index)

    ttl = title
    if not ttl and title_file and title_file.exists():
        ttl = title_file.read_text(encoding="utf-8").strip()
    if not ttl:
        chs = sorted(edl.chapters, key=lambda c: c.start_at) if edl.chapters else []
        first = chs[0].chapter_title if chs else ""
        ttl = f"【勉強会】{first}".strip() or "わくわくべんきょ会"

    summary = ""
    if summary_file and summary_file.exists():
        summary = summary_file.read_text(encoding="utf-8").strip()

    text = build_description(edl, title=ttl, summary=summary, chapter_lines=ch_lines)
    default = (
        f"youtube_description_p{post_unit_index}.txt" if post_unit_index >= 0
        else "youtube_description.txt")
    out_path = out or (edl_path.parent / default)
    out_path.write_text(text, encoding="utf-8")
    n_ch = len(ch_lines) if ch_lines is not None else len(edl.chapters or [])
    rprint(f"[green]概要欄[/]: {out_path}（タイトル/要約/チャプター{n_ch}/フッター）"
           f"{'' if summary else '  ※要約未指定＝--summary-file推奨'}")


@publish_app.command()
def thumbnail(
    edl_path: Path = typer.Argument(..., help="対象 EDL（出力先ディレクトリの決定に使用）"),
    top: str = typer.Option(..., help="上帯（トピック）。`[語]`=黄強調。例 [CVPR2026] 最新AI論文"),
    bottom: str = typer.Option("", help="下帯テキスト（フック）。`[語]`=強調色(赤)"),
    prompt: str = typer.Option("", help="背景アートのプロンプト（空=チャンネル傾向の既定）"),
    art: Path = typer.Option(None, help="既存の背景アートを使う（指定時は生成しない＝無課金）"),
    model: str = typer.Option(
        "gemini-2.5-flash-image", help="画像モデル（gemini-3-pro-image で高品質）"
    ),
    out: Path = typer.Option(None, help="出力PNG（既定 data/<date>/thumbnail.png）"),
) -> None:
    """[L] サムネ生成（チャンネル傾向: ゆる×AIイラスト背景＋上下太字バナー）。

    背景は nano banana（ゆる×AI・16:9・文字なし）、上下の太字バナー（`[語]`=赤/黄強調）と
    ロゴは PIL で後合成（モデルの日本語文字崩れ回避）。`--art` で既存背景を使えば**無課金**。
    """
    from wwedit.publish.thumbnail import (
        DEFAULT_ART_PROMPT,
        DEFAULT_MODEL,
        compose_banners,
        generate_image,
        save_image,
    )

    edl_dir = edl_path.parent
    out_path = out or (edl_dir / "thumbnail.png")
    art_path = art
    if art_path is None:
        rprint(f"[dim]背景アート生成中（{model}・課金あり）...[/]")
        data = generate_image(
            prompt or DEFAULT_ART_PROMPT, model=model or DEFAULT_MODEL,
            aspect_ratio="16:9", image_size="2K",
        )
        art_path = edl_dir / "thumbnail_art.png"
        save_image(data, art_path)
        rprint(f"[dim]背景アート → {art_path}[/]")

    logo = Path(__file__).resolve().parents[3] / "assets" / "logo" / "nobetube_logo.png"
    compose_banners(art_path, top, bottom, out_path, logo_path=logo if logo.exists() else None)
    rprint(f"[green]サムネ[/]: {out_path}（背景{'既存' if art else '生成'}＋上下バナー＋ロゴ）")


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
    prompt: str = typer.Option(
        "natural talking expression, gentle friendly smile", help="表情指示"),
) -> None:
    """[G] DomoAI talking-avatar で開始フレーム＋音声→リップシンク動画（**決定的・外部API課金**）。

    seconds 既定=音声尺の切り上げ。出来の良し悪し（目元/口元）の判断は呼び出し側で目視QA。
    """
    import math
    import subprocess

    from wwedit.publish.domoai import generate_talking_avatar

    if seconds <= 0:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", str(audio)], capture_output=True, text=True)
        seconds = max(1, min(60, math.ceil(float(r.stdout.strip() or 1))))
    rprint(f"[dim]DomoAI 生成中（seconds={seconds}・約${seconds * 0.06:.2f}）...[/]")
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
def youtube(
    edl_path: Path = typer.Argument(..., help="対象 EDL"),
    video: Path = typer.Option(..., help="アップロードする mp4"),
    title_file: Path = typer.Option(None, help="タイトル（既定 <date>/yt_title.txt）"),
    desc_file: Path = typer.Option(None, help="概要欄（既定 <date>/youtube_description.txt）"),
    privacy: str = typer.Option("private", help="private(既定・下書相当)/unlisted/public"),
    dry_run: bool = typer.Option(
        True, help="既定True＝本体JSONを書くだけ（キー不要・検証用）。--no-dry-run で実投稿"
    ),
) -> None:
    """[K] 動画を YouTube へ投稿（既定 dry-run＝メタデータ検証のみ・キー不要）。

    タイトル/概要欄は事前生成物（`publish description`）を使う。実投稿は .env の
    WWEDIT_YT_* と google-api-python-client が要る（無ければ手順を示して停止）。
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
    body = build_video_resource(title, desc, privacy=privacy)

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
