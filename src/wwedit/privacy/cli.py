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


@privacy_app.command(name="ng-mosaic")
def ng_mosaic(
    edl_path: Path = typer.Argument(..., help="対象 EDL（framing 済み）"),
    margin: float = typer.Option(0.8, help="検出box寸法に対する四方の余裕（大きめに隠す）"),
    strength: float = typer.Option(28.0, help="モザイクの粗さ(px・ソース基準)"),
    max_span: float = typer.Option(30.0, help="長い区間へ追加サンプルを取る間隔(秒)"),
    refresh: bool = typer.Option(False, help="OCRキャッシュを作り直す（既定は再利用）"),
    replace: bool = typer.Option(True, help="前回の自動付与ぶん(ngmask*)を置き換える"),
) -> None:
    """[PII] 画面OCR結果から、秘匿語/NGワードが写った箇所へモザイク重ねを自動付与する。

    語は .env の WWEDIT_MASK_TERMS ∪ WWEDIT_CUT_NGWORDS（コード非埋め込み）。
    **カットせずモザイクで隠す**方針（本編の流れを切らない）。座標はソースフレーム基準で
    EDL.overlays に入るので、G2 の編集ツールで位置/サイズを手直しできる。

    OCRは `screen_ocr.json` の**共有キャッシュを使い回す**（章の固有名補正と同じ1回の推論）。
    どの語に当たったかは秘匿のため出力せず、件数のみ報告する。
    """
    from wwedit.edl.schema import load_edl, save_edl
    from wwedit.privacy.ocr_mosaic import load_screen_terms, scan_ng_mosaics

    edl = load_edl(edl_path)
    words = load_screen_terms()
    if not words:
        rprint(
            "[yellow]秘匿語/NGワードが未設定（.env の WWEDIT_MASK_TERMS / "
            "WWEDIT_CUT_NGWORDS）。何もしません。[/]"
        )
        return

    def _progress(i: int, n: int) -> None:
        if i % 25 == 0:
            rprint(f"[dim]  OCR {i}/{n} フレーム[/]")

    rprint(f"[dim]画面OCR（キャッシュ優先・語{len(words)}件）...[/]")
    overlays = scan_ng_mosaics(
        edl,
        cache_path=edl_path.parent / "screen_ocr.json",
        refresh=refresh,
        margin=margin,
        strength=strength,
        max_span=max_span,
        progress_fn=_progress,
    )
    kept = [o for o in edl.overlays if not (replace and o.id.startswith("ngmask"))]
    n_removed = len(edl.overlays) - len(kept)
    edl.overlays = kept + overlays
    save_edl(edl, edl_path)
    if not overlays:
        rprint(f"[green]画面NG走査完了[/]: 該当なし（既存の自動分 {n_removed}件を削除）。")
        return
    total = sum(o.duration for o in overlays)
    rprint(
        f"[green]画面NGモザイク[/]: {len(overlays)}箇所・計{total:.1f}s を自動付与"
        f"（既存の自動分 {n_removed}件を置換）→ {edl_path}"
    )


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
