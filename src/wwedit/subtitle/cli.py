"""``wwedit subtitle`` サブコマンド（[I] style字幕）。"""

from __future__ import annotations

from pathlib import Path

import typer
from rich import print as rprint

from wwedit.edl.schema import load_edl, save_edl
from wwedit.subtitle.build import subtitles_from_utterances
from wwedit.subtitle.summarize import apply_captions, write_caption_input

subtitle_app = typer.Typer(help="style字幕（メイリオ二重枠）", no_args_is_help=True)


@subtitle_app.command()
def color(
    edl_path: Path = typer.Argument(..., help="対象 EDL"),
    speaker: str = typer.Argument(..., help="話者名（例 mossan-hoshi / Taniguchi）"),
    name: str = typer.Argument(..., help="色: red/purple/blue/green / auto(自動に戻す)"),
) -> None:
    """話者ごとの本編字幕色を切り替える（EDL.subtitle_speaker_colors に保存・合成で反映）。

    自動では sakamoto/mossan-hoshi=寒色・taniguchi=暖色を割当てる。ここで個別に上書きできる。
    """
    from wwedit.subtitle.ass import MAIN_PALETTE

    edl = load_edl(edl_path)
    if name == "auto":
        edl.subtitle_speaker_colors.pop(speaker, None)
        msg = f"{speaker}=auto（寒色/暖色で自動割当）"
    elif name in MAIN_PALETTE:
        edl.subtitle_speaker_colors[speaker] = name
        msg = f"{speaker}={name}（{MAIN_PALETTE[name]}）"
    else:
        raise typer.BadParameter(f"色は {', '.join(MAIN_PALETTE)} / auto のいずれか")
    save_edl(edl, edl_path)
    rprint(f"[green]話者字幕色[/]: {msg}")


@subtitle_app.command(name="transcript-range")
def transcript_range(
    edl_path: Path = typer.Argument(..., help="対象 EDL（transcribe 済み）"),
    start: float = typer.Option(..., help="対象期間の開始（ソース秒）"),
    end: float = typer.Option(..., help="対象期間の終了（ソース秒）"),
    pad: float = typer.Option(20.0, help="前後に付ける文脈秒（±pad）"),
) -> None:
    """指定期間 [start-pad, end-pad] の文字起こしを話者付きで標準出力に返す。

    字幕付け工程で、LLM(Haiku等)が「全文を抱え込まず」その都度この狭い窓だけ取得するための部品。
    出力は ``[mm:ss] speaker: text`` 行。対象期間の語は ``*`` で先頭マークする。
    """
    edl = load_edl(edl_path)
    if not edl.utterances:
        raise typer.BadParameter("utterances が空（先に transcribe）")
    lo, hi = max(0.0, start - pad), end + pad

    def mmss(t: float) -> str:
        return f"{int(t // 60):02d}:{int(t % 60):02d}"

    # 語を時刻順に集め、連続する同一話者をまとめて1行に。対象期間内かどうかも保持。
    words: list[tuple[float, str, str, bool]] = []
    for u in edl.utterances:
        for w in u.words:
            if w.text and w.text.strip() and lo <= w.start <= hi:
                words.append((w.start, u.speaker, w.text, start <= w.start <= end))
    words.sort(key=lambda x: x[0])
    if not words:
        rprint("(該当期間に発話なし)")
        return
    lines, cur_spk, cur_t, buf, cur_in = [], None, None, [], False
    for t, spk, txt, inside in words:
        if spk != cur_spk:
            if buf:
                lines.append((cur_in, f"[{mmss(cur_t)}] {cur_spk}: {''.join(buf)}"))
            cur_spk, cur_t, buf, cur_in = spk, t, [txt], inside
        else:
            buf.append(txt)
            cur_in = cur_in or inside
    if buf:
        lines.append((cur_in, f"[{mmss(cur_t)}] {cur_spk}: {''.join(buf)}"))
    for inside, line in lines:
        print(("* " if inside else "  ") + line)


@subtitle_app.command(name="prepare-captions")
def prepare_captions(
    edl_path: Path = typer.Argument(..., help="対象 EDL（transcribe 済み）"),
    screen_text: Path = typer.Option(
        None, help="画面OCRテキスト(screen_text.txt)。固有名補正の文脈に付ける。既定は同フォルダ"
    ),
) -> None:
    """要約字幕LLM(caption-summarizer)用の窓TSVを書き出す。

    固有名補正のため、画面OCRテキスト（`chapter screen-text` / `framing assign`後に生成）が
    あれば末尾に文脈として付ける（章タイトルと同じ補正を字幕にも効かせる）。
    """
    edl = load_edl(edl_path)
    if not edl.utterances:
        raise typer.BadParameter("utterances が空（先に transcribe）")
    tsv = edl_path.parent / "caption_input.tsv"
    out = write_caption_input(edl, tsv)

    st = screen_text or (edl_path.parent / "screen_text.txt")
    ocr_note = ""
    if st.exists():
        block = st.read_text(encoding="utf-8").strip()
        with tsv.open("a", encoding="utf-8") as f:
            f.write(
                "\n\n# --- 画面テキスト(OCR) ---"
                "（固有名はこちらを正とする。STTの聞き取り誤りを補正）\n" + block + "\n"
            )
        ocr_note = f"（OCR文脈 {st.name} を付与）"

    dec = edl_path.parent / "caption_decisions.json"
    rprint(f"[green]字幕入力[/]: {out} {ocr_note}")
    rprint(f"  → caption-summarizer スキル(Sonnetサブエージェント)で {dec} を作成")


@subtitle_app.command(name="apply-captions")
def apply_captions_cmd(
    edl_path: Path = typer.Argument(..., help="対象 EDL"),
    decisions: Path = typer.Option(None, help="LLM決定JSON（既定 caption_decisions.json）"),
) -> None:
    """caption-summarizer の要約字幕決定を EDL.subtitles に反映する。各字幕は発話チャンクの

    開始〜終了まで表示（早く消えない・次字幕開始は越えない）。
    """
    edl = load_edl(edl_path)
    dec = decisions or (edl_path.parent / "caption_decisions.json")
    if not dec.exists():
        raise typer.BadParameter(f"決定JSONが無い: {dec}（prepare-captions→LLM が必要）")
    apply_captions(edl, dec)
    save_edl(edl, edl_path)
    rprint(f"[green]要約字幕 {len(edl.subtitles)}件[/] を EDL.subtitles に反映")


@subtitle_app.command()
def build(
    edl_path: Path = typer.Argument(..., help="対象 EDL（transcribe 済み）"),
    style: str = typer.Option("main", help="main=緑〜水色 / intro=ピンク"),
    max_chars: int = typer.Option(28, help="1字幕の最大文字数（超は分割）"),
) -> None:
    """EDL.utterances から字幕を生成し EDL.subtitles に保存する（ソース時刻・非破壊）。"""
    edl = load_edl(edl_path)
    if not edl.utterances:
        raise typer.BadParameter("utterances が空（先に transcribe）")
    subs = subtitles_from_utterances(edl, style=style, max_chars=max_chars)  # type: ignore[arg-type]
    edl.subtitles = subs
    save_edl(edl, edl_path)
    rprint(f"[green]字幕 {len(subs)}件[/]（style={style}）を EDL.subtitles に保存")
