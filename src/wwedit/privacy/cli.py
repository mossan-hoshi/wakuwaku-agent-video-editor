"""``wwedit privacy`` サブコマンド：PIIマスキング。"""

from __future__ import annotations

from pathlib import Path

import typer
from rich import print as rprint

privacy_app = typer.Typer(help="PII（個人情報）マスキング", no_args_is_help=True)


@privacy_app.command()
def terms() -> None:
    """設定中のマスク語の件数を表示する（語そのものは伏せる）。"""
    from wwedit.privacy.masking import MASK_ENV, load_mask_terms

    n = len(load_mask_terms())
    if n:
        rprint(f"[green]マスク語 {n}件 設定済[/]（{MASK_ENV} / .env）")
    else:
        rprint(f"[yellow]マスク語 未設定[/]（{MASK_ENV} を .env に設定してください）")


@privacy_app.command(name="mask-frame")
def mask_frame(
    image: Path = typer.Argument(..., help="入力フレーム画像"),
    out: Path = typer.Option(..., "--out", "-o", help="マスク後の出力PNG"),
) -> None:
    """1フレームをOCRし、秘匿語を含むテキスト領域を読めないぼかしで隠して保存する。

    画面中に見つかった秘匿語は **すべて** マスクする。秘匿語は .env の WWEDIT_MASK_TERMS。
    """
    import cv2

    from wwedit.ocr import run_ocr
    from wwedit.privacy.masking import apply_blur, find_mask_regions, load_mask_terms

    terms = load_mask_terms()
    if not terms:
        rprint("[yellow]マスク語が未設定です（.env の WWEDIT_MASK_TERMS）。何もしません。[/]")
        raise typer.Exit(1)

    boxes = run_ocr(image)
    regions = find_mask_regions(boxes, terms)
    img = cv2.imread(str(image))
    if img is None:
        raise typer.BadParameter(f"画像を読めません: {image}")
    masked = apply_blur(img, regions)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), masked)
    rprint(
        f"[green]マスク完了[/]: OCR {len(boxes)}領域中 {len(regions)}領域をぼかし → {out}"
    )
