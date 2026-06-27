"""``wwedit chapter`` サブコマンド（[D] チャプター/投稿単位）。"""

from __future__ import annotations

from pathlib import Path

import typer
from rich import print as rprint

from wwedit.chapter.detect import (
    apply_decisions,
    write_chapter_input,
    youtube_chapter_lines,
)
from wwedit.edl.schema import load_edl, save_edl

chapter_app = typer.Typer(help="チャプター/投稿単位（transcript→LLM）", no_args_is_help=True)


@chapter_app.command()
def prepare(
    edl_path: Path = typer.Argument(..., help="対象 EDL（transcribe 済み）"),
) -> None:
    """LLM(chapter-detector)用の発話インデックス付きトランスクリプトを書き出す。"""
    edl = load_edl(edl_path)
    if not edl.utterances:
        raise typer.BadParameter("utterances が空（先に transcribe を実行）")
    out = write_chapter_input(edl, edl_path.parent / "chapter_input.tsv")
    dec = edl_path.parent / "chapter_decisions.json"
    rprint(f"[green]章入力{len(edl.utterances)}発話[/]: {out}")
    rprint(f"  → chapter-detector スキルで {dec} を作成")


@chapter_app.command(name="screen-text")
def screen_text(
    edl_path: Path = typer.Argument(..., help="対象 EDL（framing assign 済みが望ましい）"),
    out: Path = typer.Option(None, help="出力txt（既定 data/<date>/screen_text.txt）"),
    append_input: bool = typer.Option(
        True, help="chapter_input.tsv 末尾に画面テキスト文脈を追記する"
    ),
) -> None:
    """各 static フレーミング区間の代表フレームをメイン領域でOCRし、画面テキスト文脈を作る。

    STT が誤りやすい固有名（モデル名/論文名/ツール名）を、画面に映った文字で補正するための
    文脈。章検出LLM入力(chapter_input.tsv)に追記でき、概要欄/サムネ生成にも使える。
    """
    from wwedit.chapter.ocr_context import build_screen_digest, format_digest

    edl = load_edl(edl_path)
    if not any(r.kind == "static" for r in edl.framing):
        raise typer.BadParameter("static フレーミング区間が無い（先に framing scenes/assign）")
    rprint("[dim]代表フレームをOCR中（メイン領域に切り出し）...[/]")
    digest = build_screen_digest(edl)
    block = format_digest(digest)
    out_path = out or (edl_path.parent / "screen_text.txt")
    out_path.write_text(block + "\n", encoding="utf-8")
    rprint(f"[green]画面テキスト {len(digest)}件[/]: {out_path}")

    if append_input:
        tsv = edl_path.parent / "chapter_input.tsv"
        if tsv.exists():
            with tsv.open("a", encoding="utf-8") as f:
                f.write("\n\n" + block + "\n")
            rprint(f"  → {tsv} に文脈を追記")


@chapter_app.command()
def apply(
    edl_path: Path = typer.Argument(..., help="対象 EDL"),
    decisions: Path = typer.Option(None, help="LLM決定JSON（既定 data/<date>/ 内）"),
) -> None:
    """LLM の章/投稿単位決定を EDL.chapters / post_units に反映する。"""
    edl = load_edl(edl_path)
    dec = decisions or (edl_path.parent / "chapter_decisions.json")
    if not dec.exists():
        raise typer.BadParameter(f"決定JSONが無い: {dec}（prepare→LLM が必要）")
    apply_decisions(edl, dec)
    save_edl(edl, edl_path)
    sections = [c for c in edl.chapters if c.section_title]
    rprint(
        f"[green]章{len(edl.chapters)}[/]（セクション{len(sections)}）, "
        f"投稿単位{len(edl.post_units)}"
    )
    for c in edl.chapters:
        m, s = divmod(int(c.start_at), 60)
        sec = f" [b]{c.section_title}[/]" if c.section_title else ""
        req = "" if c.is_required else " (任意)"
        rprint(f"  {m:02d}:{s:02d}  {c.chapter_title}{sec}{req}")


@chapter_app.command()
def youtube(
    edl_path: Path = typer.Argument(..., help="対象 EDL（chapter apply 済み）"),
    out: Path = typer.Option(None, help="出力txt（既定 data/<date>/youtube_chapters.txt）"),
    post_unit_index: int = typer.Option(
        -1, help="投稿単位[K]。その単位内の章を単位内出力時刻で（-1=収録まるごと）"),
) -> None:
    """YouTube説明欄用のチャプター行を出力タイムライン（カット後）で書き出す。"""
    edl = load_edl(edl_path)
    if not edl.chapters:
        raise typer.BadParameter("chapters が空（先に chapter apply を実行）")
    if post_unit_index >= 0:
        from wwedit.edl.postunit import post_unit_chapter_lines

        lines = post_unit_chapter_lines(edl, post_unit_index)
        out_path = out or (edl_path.parent / f"youtube_chapters_p{post_unit_index}.txt")
    else:
        lines = youtube_chapter_lines(edl)
        out_path = out or (edl_path.parent / "youtube_chapters.txt")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    rprint(f"[green]YouTubeチャプター[/]: {out_path}")
    for ln in lines:
        rprint(f"  {ln}")
