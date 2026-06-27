"""[E] 継続学習: 編集ツールの手修正(crop)を教師データへ取り込む。

編集ツール(`webapp/editor.py`)は crop の手調整を `correction_log.jsonl` に
``framing_edit`` として追記し、最終状態は EDL(SSOT)へ非破壊保存される。本モジュールは
**最終EDLの static 区間 bbox を人手GTとして収穫**し、収録単位 grouped CV を壊さない
別データセット(`data/framing_corrections/`)へ蓄積する。保護対象の
`data/framing_anno_full` / `data/framing_ds/dataset.json` は一切触らない（追加のみ）。

ループ: 編集ツールで crop修正 → `framing harvest-corrections <edl>` → `crop-train --extra-root`
→ 新 `crop_model.pt` → `crop-apply` で次回フレーミングが改善。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

__all__ = [
    "parse_touched_intervals",
    "px_to_norm_box",
    "build_correction_items",
    "harvest_corrections",
]


def parse_touched_intervals(log_lines: Iterable[str]) -> list[dict]:
    """correction_log の行から、人手で crop を触った区間を抽出する。

    ``framing_edit`` のうち bbox が変化したものを対象に、その ``after`` の
    ``{start, end, has_crop}``（has_crop=after.bbox が非None）を返す。途中ドラッグの
    中間値も含む＝最終GTは最終EDL側から取り、ここは「触ったか」の判定にだけ使う。
    """
    out: list[dict] = []
    for line in log_lines:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") != "framing_edit":
            continue
        a, b = d.get("after") or {}, d.get("before") or {}
        if a.get("bbox") == b.get("bbox"):
            continue  # bbox 以外（範囲/種別）の編集は無視
        if a.get("start") is None or a.get("end") is None:
            continue
        out.append({"start": float(a["start"]), "end": float(a["end"]),
                    "has_crop": a.get("bbox") is not None})
    return out


def _overlaps(a0: float, a1: float, b0: float, b1: float) -> bool:
    return a0 < b1 - 1e-6 and b0 < a1 - 1e-6


def px_to_norm_box(bbox_px, width: int, height: int) -> list[float]:
    """編集ツールの bbox (x, y, w, h)px → 学習用の正規化コーナー [x0,y0,x1,y1] (0..1)。"""
    x, y, w, h = bbox_px
    x0 = max(0.0, min(1.0, x / width))
    y0 = max(0.0, min(1.0, y / height))
    x1 = max(0.0, min(1.0, (x + w) / width))
    y1 = max(0.0, min(1.0, (y + h) / height))
    return [x0, y0, x1, y1]


def build_correction_items(
    edl, intervals: list[dict], *, group: str, id_prefix: str, trust_final: bool = False
) -> list[dict]:
    """最終EDLの static 区間から、人手で触った crop/no_crop を学習項目(frame未抽出)に変換する。

    各 item は dataset.json 互換: ``id/timeline/no_crop/bbox/corrected/rejected`` ＋
    抽出すべきソース時刻 ``_t``（frame抽出は呼び出し側）。``image`` は抽出後に確定する。
    bbox は正規化コーナー [x0,y0,x1,y1]。crop区間=bbox有り＆触れた、no_crop=bbox無し＆触れた。

    ``trust_final=True`` は log の touch 判定を省き **最終EDLの全 static bbox を人手GTとみなす**
    （白紙から手で全 crop を引いた等、最終状態を全面的に信頼できる場合。merge/境界移動で log に
    直接の編集が残らない区間も拾える）。no_crop は誤収集を避けるため常に log の明示クリアのみ。
    """
    W = edl.source.width
    H = edl.source.height
    crop_iv = [iv for iv in intervals if iv["has_crop"]]
    nocrop_iv = [iv for iv in intervals if not iv["has_crop"]]
    statics = [r for r in (edl.framing or []) if r.kind == "static"]
    items: list[dict] = []
    for i, r in enumerate(statics):
        touched_crop = trust_final or any(
            _overlaps(r.start, r.end, iv["start"], iv["end"]) for iv in crop_iv
        )
        touched_nocrop = any(
            _overlaps(r.start, r.end, iv["start"], iv["end"]) for iv in nocrop_iv
        )
        t = (r.start + r.end) / 2
        base = {"id": f"{id_prefix}-{i:04d}", "timeline": group,
                "corrected": True, "rejected": False, "_t": float(t),
                "start": float(r.start), "end": float(r.end)}
        if r.bbox is not None and touched_crop:
            nb = px_to_norm_box(r.bbox, W, H)
            if nb[2] - nb[0] <= 1e-3 or nb[3] - nb[1] <= 1e-3:
                continue  # 退化
            if nb[2] - nb[0] >= 1.0 - 1e-3 and nb[3] - nb[1] >= 1.0 - 1e-3:
                continue  # 実質フルフレーム=crop無し扱い
            items.append({**base, "no_crop": False, "bbox": nb})
        elif r.bbox is None and touched_nocrop:
            # 人手で全画面(no_crop)と判断＝将来の no_crop ヘッド用に保存（crop回帰は除外）
            items.append({**base, "no_crop": True, "bbox": [0.0, 0.0, 1.0, 1.0]})
    return items


def harvest_corrections(
    edl_path: str | Path,
    out_root: str | Path = "data/framing_corrections",
    *,
    log_path: str | Path | None = None,
    group: str | None = None,
    trust_final: bool = False,
) -> dict:
    """最終EDL＋correction_log から手修正crop を収穫し corrections データセットへ追加する。

    フレームは out_root/frames/ に抽出、dataset.json は id でマージ（再収穫は上書き更新）。
    ``trust_final=True`` で最終EDLの全 static bbox を信頼（log touch 判定を省く）。
    返り値 = {"added": n_crop, "no_crop": n_nocrop, "skipped": ..., "dataset": path}。
    """
    from wwedit.edl.schema import load_edl
    from wwedit.framing.dataset import _extract_frame

    edl_path = Path(edl_path)
    edl = load_edl(edl_path)
    log = Path(log_path) if log_path else (edl_path.parent / "correction_log.jsonl")
    intervals: list[dict] = []
    if log.exists():
        intervals = parse_touched_intervals(log.read_text(encoding="utf-8").splitlines())
    elif not trust_final:
        return {"added": 0, "no_crop": 0, "skipped": 0, "error": f"log無し: {log}"}

    rec = group or f"corr:{edl.recording_dir or edl_path.stem}"
    id_prefix = f"corr-{edl_path.stem}"
    items = build_correction_items(
        edl, intervals, group=rec, id_prefix=id_prefix, trust_final=trust_final
    )

    out_root = Path(out_root)
    frames_dir = out_root / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    ds_path = out_root / "dataset.json"
    existing = {}
    if ds_path.exists():
        for x in json.loads(ds_path.read_text(encoding="utf-8")):
            existing[x["id"]] = x

    n_crop = n_nocrop = skipped = 0
    for it in items:
        png_rel = f"frames/{it['id']}.png"
        png_abs = out_root / png_rel
        if not png_abs.exists():
            if not _extract_frame(edl.source.video_path, it["_t"], png_abs):
                skipped += 1
                continue
        rec_item = {
            "id": it["id"], "image": png_rel, "timeline": it["timeline"],
            "bbox": it["bbox"], "no_crop": it["no_crop"],
            "corrected": True, "rejected": False,
            "source": "correction", "edl": str(edl_path), "t": it["_t"],
        }
        existing[it["id"]] = rec_item
        if it["no_crop"]:
            n_nocrop += 1
        else:
            n_crop += 1

    ds_path.write_text(
        json.dumps(list(existing.values()), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"added": n_crop, "no_crop": n_nocrop, "skipped": skipped,
            "total_in_dataset": len(existing), "dataset": str(ds_path)}
