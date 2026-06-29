"""[M4] YouTube 概要欄テキストの組み立て（**チャンネル実フォーマット準拠**）。

@mossan_hoshi（わく枠べんきょ会）の実投稿を分析した型に合わせる（[[youtube-description-format]]）:

    Agenda「<その回のテーマ>」
    （空行）
    <関連リンク: ラベル→URL・任意・ブロック間は空行>
    （空行）
    #tag #tag #tag                ← 半角#・スペース区切り
    （空行）
    00:00 - start
    MM:SS - <章ラベル>            ← " - " 区切り
    …

**入れない**（実投稿に無い）: プロローグ要約 / AI免責 / チャンネルURLフッター /「チャプター」
見出し / 本文へのタイトル再掲（タイトルは動画 snippet 側）。Agenda/タグ/リンクは内容依存なので
呼び出し側（auto-edit の Claude）が決めて渡す。秘匿語(PII)は置換済み入力前提。
"""

from __future__ import annotations

from wwedit.chapter.detect import youtube_chapter_lines
from wwedit.edl.schema import Edl


def _ts_line(line: str) -> str | None:
    """``"MM:SS ラベル"`` → ``"MM:SS - ラベル"``。先頭00:00は start 行に集約するため除外。"""
    parts = line.strip().split(" ", 1)
    if len(parts) != 2:
        return line.strip() or None
    ts, label = parts
    if ts in ("0:00", "00:00"):
        return None  # 00:00 は "00:00 - start" に集約
    return f"{ts} - {label}"


def build_description(
    edl: Edl,
    *,
    agenda: str,
    links: list[tuple[str, str]] | None = None,
    hashtags: str | list[str] | None = None,
    chapter_lines: list[str] | None = None,
    start_label: str = "start",
) -> str:
    """概要欄を**チャンネル実フォーマット**で組み立てる（決定的・テスト可能）。

    - agenda: その回のテーマ。``Agenda「…」`` に入れる（必須）。
    - links: ``[(ラベル, URL), …]``。各ブロックは ``ラベル\\nURL``、ブロック間は空行。
    - hashtags: ``"#a #b"`` の文字列、または ``["a","b"]``（``#`` は自動付与）。
    - chapter_lines: ``"MM:SS ラベル"`` の章行（既定 ``youtube_chapter_lines``）。先頭に
      ``00:00 - start`` を必ず置き、各章は ``MM:SS - ラベル`` に整形（00:00章は start に集約）。
    """
    blocks: list[str] = [f"Agenda「{agenda.strip()}」"]

    if links:
        blocks.append("\n\n".join(f"{label}\n{url}" for label, url in links))

    if hashtags:
        tagline = hashtags if isinstance(hashtags, str) else " ".join(
            t if t.startswith("#") else f"#{t}" for t in hashtags)
        if tagline.strip():
            blocks.append(tagline.strip())

    ch_lines = chapter_lines if chapter_lines is not None else youtube_chapter_lines(edl)
    ts = [f"00:00 - {start_label}"]
    for ln in (ch_lines or []):
        formatted = _ts_line(ln)
        if formatted:
            ts.append(formatted)
    blocks.append("\n".join(ts))

    return "\n\n".join(blocks).rstrip() + "\n"
