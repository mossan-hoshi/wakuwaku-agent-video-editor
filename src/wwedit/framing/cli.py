"""``wwedit framing`` サブコマンド。"""

from __future__ import annotations

from pathlib import Path

import typer
from rich import print as rprint

from wwedit.edl.schema import load_edl, save_edl
from wwedit.framing.codec_motion import detect_stable_regions_codec
from wwedit.framing.motion import detect_stable_regions

framing_app = typer.Typer(help="フレーミング（動き検出・メイン領域bbox）", no_args_is_help=True)


@framing_app.command()
def scenes(
    edl_path: Path = typer.Argument(..., help="対象 EDL (data/<date>/edl.json)"),
    method: str = typer.Option("codec", help="検出法: codec(超軽量・既定) / scenedetect"),
    # codec 法のパラメータ
    k: float = typer.Option(6.0, help="[codec] 適応閾値の係数(中央値+k*MAD)"),
    floor_bytes: float = typer.Option(800.0, help="[codec] 動き判定のバイト下限"),
    min_region: float = typer.Option(1.0, help="最小区間長(秒)"),
    # scenedetect 法のパラメータ
    adaptive_threshold: float = typer.Option(3.0, help="[scenedetect] AdaptiveDetector 閾値"),
    downscale: int = typer.Option(0, help="[scenedetect] フレーム縮小率(0=auto)"),
) -> None:
    """安定フレーミング区間を検出し EDL.framing に書き込む。

    既定は codec 法（ffprobe でフレーム符号化サイズを読むだけ。ピクセル非デコードで超軽量）。
    """
    edl = load_edl(edl_path)
    if method == "codec":
        regions = detect_stable_regions_codec(
            edl.source.video_path, k=k, floor_bytes=floor_bytes, min_region_s=min_region
        )
    elif method == "scenedetect":
        regions = detect_stable_regions(
            edl.source.video_path,
            adaptive_threshold=adaptive_threshold,
            min_scene_len_s=min_region,
            downscale=downscale or None,
        )
    else:
        raise typer.BadParameter("method は codec / scenedetect")

    edl.framing = regions
    save_edl(edl, edl_path)

    static = [r for r in regions if r.kind == "static"]
    moving = [r for r in regions if r.kind != "static"]
    durs = [r.end - r.start for r in regions]
    avg = sum(durs) / len(durs) if durs else 0.0
    rprint(
        f"[green]フレーミング区間検出[/] (method={method}): "
        f"全{len(regions)}区間 (静止{len(static)}/動き{len(moving)}, 平均{avg:.1f}s)"
    )


@framing_app.command()
def dataset(
    out_dir: Path = typer.Option(Path("data/framing_ds"), help="データセット出力先"),
    timelines: str = typer.Option(
        "", help="対象TLのuuid前方一致をカンマ区切り(空=フレーミング有りTLを自動検出)"
    ),
    drp: Path = typer.Option(None, help=".drp パス(既定はreader.DEFAULT_DRP)"),
    zoom_min: float = typer.Option(1.05, help="この倍率超のクリップだけ対象"),
) -> None:
    """`.drp` のフレーミングクリップから (フレーム画像＋初期bbox) データセットを抽出する。"""
    from wwedit.drp.framing import framing_clips_for_timeline
    from wwedit.drp.reader import DEFAULT_DRP, read_timelines
    from wwedit.framing.dataset import build_dataset

    drp_path = str(drp) if drp else DEFAULT_DRP
    if timelines.strip():
        uuids = [t.strip() for t in timelines.split(",") if t.strip()]
    else:
        rprint("[dim]フレーミング有りTLを検出中...[/]")
        uuids = []
        for tl in read_timelines(drp_path):
            cs = framing_clips_for_timeline(tl.uuid, drp_path)
            if any(c.is_reframed for c in cs):
                uuids.append(tl.uuid)
        rprint(f"[dim]対象TL {len(uuids)}本[/]")

    items = build_dataset(uuids, out_dir, drp_path=drp_path, zoom_min=zoom_min)
    done = sum(1 for i in items if i.corrected)
    rprint(f"[green]データセット {len(items)}件[/] → {out_dir}/dataset.json (補正済 {done})")


@framing_app.command(name="classify-motion")
def classify_motion(
    edl_path: Path = typer.Argument(..., help="対象 EDL (data/<date>/edl.json)"),
    thr: float = typer.Option(0.6, help="画面切替と判定する spread 閾値"),
    samples: int = typer.Option(6, help="区間あたりのフロー計算サンプル数"),
) -> None:
    """pending(動き)区間を 画面切替=loading / コンテンツ内動画=pending+警告 に分類する。

    オプティカルフローの空間的広がり(spread)で判別。loading は後段でローディング画面へ置換、
    コンテンツ内動画は警告のみ付けて作業は止めずユーザー確認に回す。
    """
    from wwedit.framing.motion_type import classify_pending_region

    edl = load_edl(edl_path)
    pend = [r for r in edl.framing if r.kind == "pending"]
    if not pend:
        rprint("[yellow]pending 区間がありません。先に `framing scenes` を実行してください[/]")
        raise typer.Exit(1)

    rprint(f"[dim]pending {len(pend)}区間を分類中（オプティカルフロー）...[/]")
    for r in pend:
        classify_pending_region(edl.source.video_path, r, thr=thr, samples=samples)
    save_edl(edl, edl_path)

    loading = sum(1 for r in edl.framing if r.kind == "loading")
    still_pending = sum(1 for r in edl.framing if r.kind == "pending")
    rprint(
        f"[green]分類完了[/]: 画面切替(loading) {loading} / "
        f"コンテンツ内動画(pending+警告) {still_pending}"
    )


@framing_app.command()
def assign(
    edl_path: Path = typer.Argument(..., help="対象 EDL (data/<date>/edl.json)"),
    aggressive: bool = typer.Option(
        False, help="OmniParser判定＋固定crop箱を自動適用（実験用・過剰crop注意）"
    ),
    thr: float = typer.Option(0.95, help="[aggressive] no_crop 判定の要素span閾値"),
    conf: float = typer.Option(0.1, help="[aggressive] 要素検出のconf下限"),
) -> None:
    """static フレーミング区間の bbox を割り当てる。

    **既定は保守的＝全区間 no_crop（全画面）**。crop位置は画像から決められないと検証で確定
    （[[framing-cv-heuristic-below-floor]]）。画面共有は全画面充填で span 判定も効かず、自動の
    汎用固定箱は的外れな寄せを全区間に乗せて破綻する。よって**自動では寄せず、cropは Web
    アプリの人手アノテで各区間に bbox を付ける**（SDD方針）。

    ``--aggressive`` は旧挙動（OmniParser no_crop判定＋固定箱）を残すが過剰cropになりやすい。
    """
    edl = load_edl(edl_path)
    statics = [r for r in edl.framing if r.kind == "static"]
    if not statics:
        rprint("[yellow]static 区間がありません。先に `framing scenes` を実行してください[/]")
        raise typer.Exit(1)

    if not aggressive:
        for r in statics:
            r.bbox = None  # 全画面（no_crop）。crop は人手アノテで付与する
        save_edl(edl, edl_path)
        rprint(
            f"[green]保守的割当[/]: static {len(statics)}区間を全画面(no_crop)に。"
            "crop は Web アプリ人手で各区間に付与してください。"
        )
        return

    import tempfile

    from wwedit.common.media import probe
    from wwedit.framing.dataset import _extract_frame
    from wwedit.framing.motion import representative_time
    from wwedit.framing.omniparser import detect_elements
    from wwedit.framing.predict import bbox_norm_to_pixels, predict_framing

    video = edl.source.video_path
    info = probe(video)
    w, h = info.width or 1920, info.height or 1080
    rprint(f"[dim]static {len(statics)}区間に bbox 割当中（OmniParser・GPU推論）...[/]")
    tmp = Path(tempfile.mkdtemp())
    n_crop = n_nocrop = 0
    for r in statics:
        png = tmp / "rep.png"
        if not _extract_frame(video, representative_time(r), png):
            r.warning = "代表フレーム抽出失敗"
            continue
        no_crop, bbox = predict_framing(png, detector=detect_elements, thr=thr, conf=conf)
        r.bbox = bbox_norm_to_pixels(bbox, w, h)
        n_nocrop += no_crop
        n_crop += not no_crop

    save_edl(edl, edl_path)
    rprint(f"[green]bbox 割当完了[/]: crop {n_crop} / no_crop {n_nocrop}（pending除く）")


@framing_app.command(name="loading-clips")
def loading_clips(
    edl_path: Path = typer.Argument(..., help="対象 EDL（classify-motion 済み）"),
    out_dir: Path = typer.Option(None, help="出力先（既定 data/<date>/loading/）"),
    default_label: str = typer.Option("画面を準備", help="loading_label 未設定時の○○"),
    fps: int = typer.Option(10, help="生成fps"),
) -> None:
    """各 loading(画面切替)区間に、のべつべ!ロゴのローディング画面クリップを生成する。

    label は region.loading_label（将来LLMが会話文脈から設定）を優先、無ければ default_label。
    クリップ長は区間長に合わせる。出力は loading_<idx>.mp4。
    """
    from wwedit.framing.loading_screen import build_loading_screen

    edl = load_edl(edl_path)
    loadings = [r for r in edl.framing if r.kind == "loading"]
    if not loadings:
        rprint("[yellow]loading 区間がありません（先に `framing classify-motion`）[/]")
        raise typer.Exit(1)

    out = out_dir or (edl_path.parent / "loading")
    out.mkdir(parents=True, exist_ok=True)
    rprint(f"[dim]ローディング画面 {len(loadings)}本 生成中...[/]")
    for i, r in enumerate(loadings):
        label = r.loading_label or default_label
        build_loading_screen(label, r.end - r.start, out / f"loading_{i:03d}.mp4", fps=fps)
    rprint(f"[green]ローディング画面 {len(loadings)}本[/] → {out}")


@framing_app.command(name="omni-cache")
def omni_cache(
    dataset_dir: Path = typer.Option(Path("data/framing_ds"), help="dataset.json のある場所"),
    conf: float = typer.Option(0.05, help="YOLO検出のconf下限"),
    imgsz: int = typer.Option(1280, help="推論解像度"),
) -> None:
    """OmniParser要素検出を全corrected フレームに1回かけ、結果をJSONキャッシュする（GPU・重い）。"""
    from wwedit.framing.evaluate import load_gt
    from wwedit.framing.omniparser import DEFAULT_CACHE, build_cache

    gt = load_gt(dataset_dir)
    rprint(f"[dim]OmniParser検出 {len(gt)}フレーム（GPU推論・1回のみ）...[/]")
    cache = build_cache(gt, dataset_dir, conf=conf, imgsz=imgsz)
    total = sum(len(v) for v in cache.values())
    rprint(f"[green]検出キャッシュ {len(cache)}フレーム / {total}要素[/] → {DEFAULT_CACHE}")


@framing_app.command()
def eval(
    dataset_dir: Path = typer.Option(Path("data/framing_ds"), help="dataset.json のある場所"),
    predictor: str = typer.Option(
        "omni", help="予測器: omni(要素検出ハイブリッド・本命) / cv(エッジ密度) / mean(GT平均=床)"
    ),
) -> None:
    """予測bboxをGT(アノテ済)とIoU比較する。dataset.jsonは読むだけ。

    omni は OmniParser 検出キャッシュ(`framing omni-cache`で事前生成)を使う。
    """
    from wwedit.framing.evaluate import (
        analyze_gt,
        evaluate,
        load_gt,
        mean_bbox_predictor,
        omni_bbox_predictor,
        predict_content_bbox,
    )

    gt = load_gt(dataset_dir)
    if not gt:
        rprint("[red]corrected な項目がありません[/]")
        raise typer.Exit(1)

    a = analyze_gt(gt)
    rprint(
        f"[cyan]GT分析[/]: 全{a['n']}件 / no_crop {a['no_crop']}件"
        f"({a['no_crop_rate']:.0%}) / クロップ有り {a['cropped']}件"
    )
    cx, cy, wd = a["center_x"], a["center_y"], a["width"]
    rprint(
        f"  中心x μ={cx['mean']:.3f}±{cx['std']:.3f}  "
        f"中心y μ={cy['mean']:.3f}±{cy['std']:.3f}  "
        f"幅 μ={wd['mean']:.3f}±{wd['std']:.3f} [{wd['min']:.3f}..{wd['max']:.3f}]"
    )

    if predictor == "omni":
        predict_fn = omni_bbox_predictor(gt, dataset_dir)
    elif predictor == "cv":
        predict_fn = predict_content_bbox
    elif predictor == "mean":
        predict_fn = mean_bbox_predictor(gt)
    else:
        raise typer.BadParameter("predictor は omni / cv / mean")

    r = evaluate(predict_fn, gt, dataset_dir)
    rprint(
        f"[green]IoU[/] (predictor={predictor}, n={r['n']}): "
        f"mean={r['mean_iou']:.3f}  median={r['median_iou']:.3f}  "
        f">0.5: {r['over_0.5']}件  >0.7: {r['over_0.7']}件"
    )


@framing_app.command(name="crop-cv")
def crop_cv(
    root: Path = typer.Option(Path("data/framing_anno_full"), help="dataset.json と frames/"),
    cache_dir: Path = typer.Option(None, help="特徴キャッシュ先（既定 <root>/_feat_cache）"),
    backbone: str = typer.Option(
        "vit_small_patch14_dinov2.lvd142m", help="timm frozen backbone（DINOv2既定・差替可）"
    ),
    rows: int = typer.Option(28, help="patchグリッド行数"),
    cols: int = typer.Option(50, help="patchグリッド列数"),
    epochs: int = typer.Option(250, help="ヘッド学習エポック"),
    k: int = typer.Option(5, help="grouped CV の fold 数"),
) -> None:
    """[E] 専用クロップモデルの収録単位 grouped CV。frozen dense特徴＋軽量ヘッドが床(定数IoU≈0.62)を
    超えるか測る。featは <cache_dir> に1回だけ抽出してキャッシュ（resume可）。"""
    import json

    from wwedit.framing.croptrain import DEFAULT_CACHE, run_cv

    cache = str(cache_dir) if cache_dir else (str(root / "_feat_cache") or DEFAULT_CACHE)
    rprint(f"[dim]grouped {k}-fold CV（backbone={backbone}, grid={rows}x{cols}）...[/]")
    rep = run_cv(
        root, cache, backbone=backbone, grid=(rows, cols), k=k, epochs=epochs
    )
    beats = rep["model_mean_iou"] > rep["const_mean_iou"]
    color = "green" if beats else "yellow"
    rprint(
        f"[{color}]model mean IoU={rep['model_mean_iou']:.4f}[/] "
        f"(median {rep['model_median_iou']:.4f}, fold {rep['model_fold_mean']:.4f}"
        f"±{rep['model_fold_std']:.4f}) vs 定数床 {rep['const_mean_iou']:.4f}  "
        f"{'床超え✓' if beats else '床未満✗'}  "
        f">0.5:{rep['over_0.5']} >0.7:{rep['over_0.7']} / n={rep['n']}"
    )
    out = root / "crop_cv_report.json"
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    rprint(f"[dim]詳細 → {out}[/]")


@framing_app.command(name="crop-cv-ft")
def crop_cv_ft(
    root: Path = typer.Option(Path("data/framing_anno_full"), help="dataset.json と frames/"),
    extra_root: list[Path] = typer.Option(
        None, help="追加データ root（手修正 corrections 等・継続学習検証。複数可）"
    ),
    epochs: int = typer.Option(35, help="最大エポック（early stop あり）"),
    batch: int = typer.Option(96, help="バッチ（VRAM上限内。GPU飽和のため大きめ既定）"),
    unfreeze: int = typer.Option(2, help="DINOv2 後段の解凍ブロック数"),
    k: int = typer.Option(5, help="grouped CV の fold 数"),
    mem_fraction: float = typer.Option(
        0.8, help="VRAM 上限割合（安全装置・単一ジョブ前提）"
    ),
    num_workers: int = typer.Option(6, help="DataLoader 並列decode数（GPU飢餓回避）"),
) -> None:
    """[E] DINOv2 部分fine-tune の収録単位 grouped CV（aug付き実学習・床超え本命）。

    **GPU安全**: VRAM上限を必ず設定し単一ジョブで回す。残存pythonプロセスは kill しない。
    scale/pan 同変aug で size(zoom) を増幅し、frozen probe の床(IoU≈0.62)を超える。
    `--extra-root data/framing_corrections` で手修正データ込みの汎化をリーク無しで検証。
    """
    import json

    from wwedit.framing.croptrain_ft import run_cv_ft

    extra = [str(p) for p in extra_root] if extra_root else None
    rprint(f"[dim]部分fine-tune grouped {k}-fold（unfreeze={unfreeze}, VRAM≤{mem_fraction}"
           f"{'・+corrections' if extra else ''}）...[/]")
    rep = run_cv_ft(
        root, k=k, epochs=epochs, batch=batch, unfreeze=unfreeze,
        mem_fraction=mem_fraction, extra_roots=extra, num_workers=num_workers,
    )
    beats = rep["model_mean_iou"] > rep["const_mean_iou"]
    color = "green" if beats else "yellow"
    rprint(
        f"[{color}]model mean IoU={rep['model_mean_iou']:.4f}[/] "
        f"(median {rep['model_median_iou']:.4f}, fold {rep['model_fold_mean']:.4f}"
        f"±{rep['model_fold_std']:.4f}) vs 定数床 {rep['const_mean_iou']:.4f}  "
        f"{'床超え✓' if beats else '床未満✗'}  "
        f">0.7:{rep['over_0.7']} / n={rep['n']}"
    )
    out = root / "crop_cv_ft_report.json"
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    rprint(f"[dim]詳細 → {out}[/]")


@framing_app.command(name="harvest-corrections")
def harvest_corrections_cmd(
    edl_paths: list[Path] = typer.Argument(..., help="編集済みEDL（横に correction_log.jsonl）"),
    out: Path = typer.Option(
        Path("data/framing_corrections"), help="corrections データセット出力先（追加蓄積）"
    ),
    log: Path = typer.Option(None, help="correction_log のパス（既定=各EDL横）"),
    trust_final: bool = typer.Option(
        False,
        help="log の touch判定を省き最終EDLの全static bboxを人手GTとみなす"
        "（白紙から手で全crop引いた等、最終状態を全面信頼できる時）",
    ),
) -> None:
    """[E] 編集ツールの手修正crop を教師データへ収穫する（継続学習の入口）。

    各EDLの最終 static 区間 bbox を**人手GT**として `data/framing_corrections` に追加蓄積。
    既定は log で人手が触った区間のみ（保守的）。`--trust-final` で最終EDL全 crop を採用。
    保護対象（framing_anno_full / framing_ds）は触らない。収穫後は
    `framing crop-train --extra-root <out>` で再学習し `crop-apply` で次回フレーミングが改善。
    """
    from wwedit.framing.corrections import harvest_corrections

    tot_c = tot_n = tot_s = 0
    for ep in edl_paths:
        rep = harvest_corrections(ep, out, log_path=log, trust_final=trust_final)
        if rep.get("error"):
            rprint(f"[yellow]{ep.name}: {rep['error']}[/]")
            continue
        tot_c += rep["added"]
        tot_n += rep["no_crop"]
        tot_s += rep["skipped"]
        rprint(f"[dim]{ep.name}: crop {rep['added']} / no_crop {rep['no_crop']} "
               f"/ 抽出失敗 {rep['skipped']}[/]")
    rprint(
        f"[green]収穫完了[/]: crop {tot_c} / no_crop {tot_n}（抽出失敗 {tot_s}）→ {out}。"
        f"  次: [cyan]crop-cv-ft --extra-root {out}[/]（検証）→ "
        f"[cyan]crop-train --extra-root {out}[/]（再学習）"
    )


@framing_app.command(name="crop-train")
def crop_train(
    root: Path = typer.Option(Path("data/framing_anno_full"), help="dataset.json と frames/"),
    out: Path = typer.Option(
        Path("data/framing_pred/crop_model.pt"), help="本番モデル保存先（永続化）"
    ),
    extra_root: list[Path] = typer.Option(
        None, help="追加データ root（手修正 corrections 等・継続学習。複数可）"
    ),
    epochs: int = typer.Option(25, help="学習エポック（val無し・固定）"),
    batch: int = typer.Option(96, help="バッチ（VRAM上限内。GPU飽和のため大きめ既定）"),
    unfreeze: int = typer.Option(2, help="DINOv2 後段の解凍ブロック数"),
    mem_fraction: float = typer.Option(0.8, help="VRAM 上限割合（安全装置・単一ジョブ前提）"),
    num_workers: int = typer.Option(6, help="DataLoader 並列decode数（GPU飢餓回避）"),
) -> None:
    """[E] 全 crop 項目で本番クロップモデルを学習し crop_model.pt へ保存する。

    **GPU安全**: VRAM上限を必ず設定し単一ジョブで回す（[[no-heavy-gpu-without-consent]]）。
    `--extra-root data/framing_corrections` で手修正データを足して**継続学習**できる。
    保存後は `framing crop-apply <edl>` で各 EDL の framing.bbox へ書き戻せる。
    """
    from wwedit.framing.croptrain_ft import save_crop_model, train_final

    extra = [str(p) for p in extra_root] if extra_root else None
    rprint(
        f"[dim]本番学習（unfreeze={unfreeze}, batch={batch}, nw={num_workers}, VRAM≤{mem_fraction}"
        f"{'・継続学習 +' + ','.join(extra) if extra else ''}）"
        "...重いGPU処理。事前に nvidia-smi で空きVRAMを確認済みであること[/]"
    )
    model, pmu, psd, device = train_final(
        root, epochs=epochs, batch=batch, unfreeze=unfreeze,
        mem_fraction=mem_fraction, extra_roots=extra, num_workers=num_workers,
    )
    path = save_crop_model(model, pmu, psd, out, unfreeze=unfreeze)
    rprint(f"[green]本番モデル保存[/] (device={device}) → {path}")


@framing_app.command(name="crop-apply")
def crop_apply(
    edl_path: Path = typer.Argument(..., help="対象 EDL (data/<date>/edl.json)"),
    model: Path = typer.Option(
        Path("data/framing_pred/crop_model.pt"), help="学習済みモデル（crop-train で作成）"
    ),
    device: str = typer.Option(
        "cpu", help="推論デバイス（既定cpu＝安全。区間数が多いなら cuda）"
    ),
    static_only: bool = typer.Option(
        True, help="static区間のみ対象（loading/pendingは除外）"
    ),
) -> None:
    """[E] 学習済みモデルで各 framing 区間の代表フレームを推論し framing.bbox へ書き戻す。

    書き戻した bbox は `compose video --framed` / 編集ツールの crop 枠に反映される。
    既定 device=cpu で安全（数十秒）。区間が多く速度が要るときだけ `--device cuda`。
    """
    from wwedit.framing.croptrain_ft import apply_model_to_edl

    if not model.exists():
        rprint(f"[red]モデルがありません: {model}（先に `framing crop-train`）[/]")
        raise typer.Exit(1)
    rprint(f"[dim]crop 推論・書き戻し中（model={model.name}, device={device}）...[/]")
    n = apply_model_to_edl(edl_path, model, device=device, static_only=static_only)
    rprint(f"[green]crop 書き戻し完了[/]: {n}区間の framing.bbox を更新 → {edl_path}")


@framing_app.command()
def annotate(
    dataset_dir: Path = typer.Option(Path("data/framing_ds"), help="dataset.json のある場所"),
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8000),
) -> None:
    """bbox補正アノテータ(CVAT風)をローカル起動する。ブラウザで http://host:port を開く。"""
    import uvicorn

    from wwedit.webapp.server import create_app

    rprint(f"[green]アノテータ起動[/]: http://{host}:{port}  (Ctrl+C で停止)")
    uvicorn.run(create_app(dataset_dir), host=host, port=port, log_level="warning")
