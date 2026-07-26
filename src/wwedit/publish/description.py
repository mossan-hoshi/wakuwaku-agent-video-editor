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

import re
import unicodedata

from wwedit.chapter.detect import youtube_chapter_lines
from wwedit.edl.schema import Edl

# YouTube がチャプターを生成する条件（https://support.google.com/youtube/answer/9884579）。
# **1つでも破ると章リスト全体が無効化され、章が1つも出ない**（#101 は先頭章が9秒で全滅した）。
MIN_CHAPTER_SECONDS = 10
MIN_CHAPTER_COUNT = 3

# 行頭のタイムスタンプ。`MM:SS` / `M:SS` / `H:MM:SS`。直後は空白か行末。
_TS_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?(?=\s|$)")


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


def _looks_like_timestamp(line: str) -> bool:
    """「時刻のつもりの行」か（数字で始まり、直後あたりにコロンがある）。

    全角数字/全角コロンで書いた行を**書式エラーとして拾う**ための判定。
    ``2026年…`` のような数字始まりの本文を拾わないよう、コロンの存在も見る。
    """
    return bool(line) and unicodedata.category(line[0]) == "Nd" and any(
        c in line[:9] for c in ":："
    )


def parse_timestamps(text: str) -> list[tuple[int, str]]:
    """概要欄から **YouTube が章として読む行**を ``(秒, ラベル)`` で拾う（書式不正は捨てる）。"""
    out: list[tuple[int, str]] = []
    for raw in text.splitlines():
        m = _TS_RE.match(raw.strip())
        if not m:
            continue
        a, b, c = m.group(1), m.group(2), m.group(3)
        secs = (int(a) * 3600 + int(b) * 60 + int(c)) if c else (int(a) * 60 + int(b))
        out.append((secs, raw.strip()[m.end():].strip(" -–—\t")))
    return out


def _mmss(sec: int) -> str:
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def chapter_problems(text: str) -> list[str]:
    """概要欄が YouTube のチャプター条件を満たすか検査し、**違反の説明**を返す（空＝OK）。

    章は「条件を1つでも破ると全部出ない」仕様なので、**投稿前に弾く**のが唯一の防ぎ方。
    検査するのは公式の条件そのもの: 先頭 ``00:00`` / 3個以上 / 昇順 / **各章10秒以上**。
    加えて、全角数字・全角コロンで書かれた時刻行（YouTube は認識しない）を書式エラーにする。
    """
    problems: list[str] = []

    for raw in text.splitlines():
        line = raw.strip()
        if _looks_like_timestamp(line) and not _TS_RE.match(line):
            problems.append(f"時刻の書式が不正（半角の M:SS / H:MM:SS ＋空白）: {line[:30]!r}")

    stamps = parse_timestamps(text)
    if not stamps:
        return problems + ["タイムスタンプ行がありません"]

    if stamps[0][0] != 0:
        problems.append(f"先頭が 00:00 ではありません（{_mmss(stamps[0][0])}）")
    if len(stamps) < MIN_CHAPTER_COUNT:
        problems.append(f"章が {len(stamps)} 個（{MIN_CHAPTER_COUNT} 個以上必要）")

    for (s0, l0), (s1, l1) in zip(stamps, stamps[1:], strict=False):
        if s1 <= s0:
            problems.append(f"時刻が昇順ではありません: {_mmss(s0)} → {_mmss(s1)}")
        elif s1 - s0 < MIN_CHAPTER_SECONDS:
            problems.append(
                f"章が {s1 - s0} 秒（{MIN_CHAPTER_SECONDS} 秒以上必要）: "
                f"{_mmss(s0)} {l0!r} → {_mmss(s1)} {l1!r}"
            )
    return problems
