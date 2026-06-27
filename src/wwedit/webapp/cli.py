"""``wwedit edit`` — 自前の手修正エディタ（ローカルWebアプリ）の起動。"""

from __future__ import annotations

from pathlib import Path

import typer

edit_app = typer.Typer(help="手修正エディタ（EDLを非破壊編集・修正ログ蓄積）", no_args_is_help=True)


@edit_app.command()
def serve(
    edl_path: Path = typer.Argument(..., help="対象 EDL (data/<date>/edl.json)"),
    preview: Path = typer.Option(None, help="レンダリング結果mp4（ビューワー用・任意）"),
    host: str = typer.Option("127.0.0.1", help="待受ホスト"),
    port: int = typer.Option(8800, help="待受ポート"),
) -> None:
    """タイムラインNLEで EDL を手修正するローカルエディタを起動（DaVinciではなくこちらが主舞台）。

    ソース時間軸でカット/フレーミング/字幕/章/BGM を可視化・編集し、EDL を非破壊保存＋修正ログ蓄積。
    Resolve準拠ショートカット: I/O=イン/アウト, J/K/L=逆/停/順再生, ←/→=1フレーム, ↑/↓=編集点移動。
    """
    import uvicorn

    from wwedit.webapp.editor import create_editor_app

    uvicorn.run(
        create_editor_app(edl_path, preview_path=preview),
        host=host, port=port, log_level="warning",
    )
