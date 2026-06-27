"""[M4] YouTube 概要欄テキストの組み立て。

タイトル・本文要約は内容依存なので**LLMで別途生成して渡す**（コスト規律：本文を主ループに
載せない）。本モジュールは EDL のチャプター＋定型フッターと合体させる**決定的な組み立て**だけを
担い、ユニットテスト可能にする。秘匿語(PII)は置換済み入力前提・ここには直書きしない。
"""

from __future__ import annotations

from wwedit.chapter.detect import youtube_chapter_lines
from wwedit.edl.schema import Edl

# 本編字幕と同じ趣旨の固定注意書き（AI生成の免責）。
AI_DISCLAIMER = (
    "※字幕・概要欄はAIが自動生成しているため、用語や内容に誤りが含まれる場合があります。"
)
CHANNEL_URL = "https://www.youtube.com/@mossan_hoshi"


def build_description(
    edl: Edl,
    *,
    title: str,
    summary: str,
    channel_url: str = CHANNEL_URL,
    extra_links: list[tuple[str, str]] | None = None,
    chapter_lines: list[str] | None = None,
) -> str:
    """概要欄テキストを組み立てる（タイトル＋要約＋チャプター＋フッター）。

    - title/summary: LLM 生成済みのタイトル行と本文要約（数行）。
    - チャプター: 既定は `youtube_chapter_lines`（出力時刻・先頭 00:00）。投稿単位[K]は
      `chapter_lines` に単位内の章行を渡す。**最低1行(00:00)が要る**ので章があるときだけ節を付ける。
    - extra_links: [(ラベル, URL), ...] を「リンク」節に。
    """
    blocks: list[str] = []
    blocks.append(title.strip())
    if summary.strip():
        blocks.append(summary.strip())

    ch_lines = chapter_lines if chapter_lines is not None else youtube_chapter_lines(edl)
    if ch_lines and ch_lines[0].startswith(("0:00", "00:00")):
        blocks.append("チャプター\n" + "\n".join(ch_lines))

    if extra_links:
        blocks.append("リンク\n" + "\n".join(f"・{label} {url}" for label, url in extra_links))

    footer = AI_DISCLAIMER
    if channel_url:
        footer += f"\n\nチャンネル: {channel_url}"
    blocks.append(footer)

    return "\n\n".join(blocks).rstrip() + "\n"
