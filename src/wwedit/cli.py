"""wwedit CLI のエントリポイント。

各工程は `wwedit <stage>` のサブコマンドとして追加していく。
M0 時点では ingest（取り込み/正規化）のみ。
"""

from __future__ import annotations

import typer
from rich import print as rprint

from wwedit import __version__

app = typer.Typer(
    name="wwedit",
    help="「わくわくべんきょ会」収録動画の自動編集＆投稿AIエージェント",
    no_args_is_help=True,
    add_completion=False,
)


@app.command()
def version() -> None:
    """バージョンを表示。"""
    rprint(f"wwedit {__version__}")


# ── 工程サブコマンドの登録（実装済みのものから順次追加）─────────────
# 注: [C] 無音検出は STT 先行・動的閾値方式に再設計するため、STT 実装後に登録する。
from wwedit.chapter.cli import chapter_app  # noqa: E402
from wwedit.compose.cli import compose_app  # noqa: E402
from wwedit.cut.cli import cut_app  # noqa: E402
from wwedit.drp.cli import drp_app  # noqa: E402
from wwedit.framing.cli import framing_app  # noqa: E402
from wwedit.ingest.cli import ingest_app  # noqa: E402
from wwedit.privacy.cli import privacy_app  # noqa: E402
from wwedit.publish.cli import publish_app  # noqa: E402
from wwedit.subtitle.cli import subtitle_app  # noqa: E402
from wwedit.transcribe.cli import transcribe_app  # noqa: E402
from wwedit.webapp.cli import edit_app  # noqa: E402

app.add_typer(ingest_app, name="ingest")
app.add_typer(transcribe_app, name="transcribe")
app.add_typer(cut_app, name="cut")
app.add_typer(framing_app, name="framing")
app.add_typer(drp_app, name="drp")
app.add_typer(compose_app, name="compose")
app.add_typer(chapter_app, name="chapter")
app.add_typer(subtitle_app, name="subtitle")
app.add_typer(privacy_app, name="privacy")
app.add_typer(publish_app, name="publish")
app.add_typer(edit_app, name="edit")


if __name__ == "__main__":
    app()
