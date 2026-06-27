"""フレーミングGTデータセット抽出（[E]フレーミングモデルの教師データ）。

`.drp` の各フレーミングクリップ（調整クリップ＋媒体クリップ）について:
1. タイムライン時刻 → ソース動画フレームへ対応（``source_frame = In + (T - Start)``）。
2. そのフレームを ffmpeg で抽出（PNG）。
3. zoom/position を **元フレーム上のクロップ bbox**（正規化 0..1）へ変換。

最終動画ではスケールは外接フィットなので、教師は「元フレームのどの矩形に寄せたか」= bbox で表す。
初期bboxは zoom/pos からの近似で、ユーザーがアノテータ(別途)で補正する前提。
"""

from __future__ import annotations

import json
import re
import struct
import subprocess
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

from wwedit.common.media import ffmpeg_path, probe
from wwedit.drp.framing import parse_effect_params
from wwedit.drp.reader import DEFAULT_DRP, remap_path

__all__ = ["bbox_from_framing", "DatasetItem", "build_dataset"]

_CLIP_RE = re.compile(r"<Sm2TiVideoClip\b.*?</Sm2TiVideoClip>", re.S)


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, v))


def bbox_from_framing(
    zoom: float, pos_x_px: float, pos_y_px: float, width: int, height: int
) -> list[float]:
    """zoom(アスペクト固定)＋position(px) から元フレーム上の正規化bbox [x0,y0,x1,y1]。

    可視窓の各辺は 1/Z。**Position は zoom 後に効く**ため、ソース座標では posX/Z・posY/Z で換算。
    Resolve は +X右/+Y上＝コンテンツがその向きに動く→可視窓は逆向きに動く:
    可視窓中心 = (0.5 - posX/Z/W, 0.5 + posY/Z/H)。近似（crop無視・補正前提）。
    """
    z = zoom or 1.0
    fw = 1.0 / z
    cx = 0.5 - (pos_x_px / z / width if width else 0.0)
    cy = 0.5 + (pos_y_px / z / height if height else 0.0)
    return [
        _clamp(cx - fw / 2),
        _clamp(cy - fw / 2),
        _clamp(cx + fw / 2),
        _clamp(cy + fw / 2),
    ]


@dataclass
class DatasetItem:
    id: str
    timeline: str
    image: str
    source_path: str
    source_time_s: float
    out_start_s: float
    out_end_s: float
    has_media: bool  # False=調整クリップ（下の媒体から抽出）
    zoom: float
    pos_y_px: float
    animated: bool  # zoom/posにキーフレームが複数=動的フレーミング（単一bboxは近似）
    bbox: list[float]  # [x0,y0,x1,y1] 正規化（初期値・要補正）
    corrected: bool = False


def _clip_fields(blk: str) -> dict:
    def g(t: str):
        m = re.search(rf"<{t}>(.*?)</{t}>", blk, re.S)
        return m.group(1).strip() if m else None

    return {
        "start": g("Start"),
        "dur": g("Duration"),
        "in": g("In"),
        "path": (g("MediaFilePath") or "").strip(),
        "eff": g("EffectFiltersBA") or "",
        "fps": g("MediaFrameRate"),
    }


def _int(s, default=0):
    try:
        return int(re.split(r"[|<]", s)[0]) if s else default
    except (ValueError, TypeError):
        return default


def _hex_double(h, default=25.0):
    if not h or len(h) < 16:
        return default
    try:
        return struct.unpack("<d", bytes.fromhex(h[:16]))[0]
    except (ValueError, struct.error):
        return default


def _extract_frame(video: str, t_s: float, out_png: Path) -> bool:
    out_png.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_path(), "-y", "-ss", f"{t_s:.3f}", "-i", video,
        "-frames:v", "1", "-q:v", "2", str(out_png),
    ]
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode == 0 and out_png.exists()


def build_dataset(
    timeline_uuids: list[str],
    out_dir: str | Path,
    *,
    drp_path: str | Path = DEFAULT_DRP,
    zoom_min: float = 1.05,
) -> list[DatasetItem]:
    """指定タイムライン群からフレーミングGTデータセットを構築する。

    各フレーミングクリップの代表フレーム(中点)を抽出し、初期bbox付きで dataset.json に保存。
    調整クリップは、その区間に重なる媒体クリップ（最大重なり）からソースフレームを得る。
    """
    out_dir = Path(out_dir)
    img_dir = out_dir / "frames"
    items: list[DatasetItem] = []
    dims: dict[str, tuple[int, int]] = {}

    with zipfile.ZipFile(drp_path) as z:
        for uuid in timeline_uuids:
            name = next(
                (n for n in z.namelist() if n.startswith(f"SeqContainer/{uuid}")), None
            )
            if not name:
                continue
            xml = z.read(name).decode("utf-8", errors="replace")
            clips = [_clip_fields(m.group(0)) for m in _CLIP_RE.finditer(xml)]
            media = [c for c in clips if c["path"] and _int(c["dur"]) > 0]

            for c in clips:
                params = parse_effect_params(c["eff"])
                z2 = params.get(2)
                zoom = max(z2) if z2 else None
                if zoom is None or zoom <= zoom_min:
                    continue
                py = params.get(5)
                pos_y = py[0] * 1080.0 if py else 0.0
                # zoom/posY のキーフレームが複数 = 動的フレーミング（パン/ズーム移動）
                animated = len(z2) > 1 or (py is not None and len(py) > 1)

                fps = _hex_double(c["fps"])
                start, dur = _int(c["start"]), _int(c["dur"])
                # クリップ先頭寄り（先頭キーフレーム値に対応）。動きの途中フレームを避ける。
                offset = min(dur * 0.15, fps * 0.4) if fps else 0
                tl_t = start + offset

                if c["path"]:
                    src = c
                else:
                    src, bestov = None, 0
                    for m in media:
                        ms, md = _int(m["start"]), _int(m["dur"])
                        ov = min(start + dur, ms + md) - max(start, ms)
                        if ov > bestov:
                            src, bestov = m, ov
                if not src:
                    continue
                src_path = remap_path(src["path"])
                src_frame = _int(src["in"]) + (tl_t - _int(src["start"]))
                t_s = max(0.0, src_frame / fps) if fps else 0.0

                cid = f"{uuid[:8]}_{start}"
                png = img_dir / f"{cid}.png"
                if not _extract_frame(src_path, t_s, png):
                    continue
                if src_path not in dims:
                    info = probe(src_path)
                    dims[src_path] = (info.width or 1920, info.height or 1080)
                w, h = dims[src_path]
                items.append(
                    DatasetItem(
                        id=cid,
                        timeline=uuid,
                        image=str(png.relative_to(out_dir)).replace("\\", "/"),
                        source_path=src_path,
                        source_time_s=round(t_s, 3),
                        out_start_s=round(start / fps, 2) if fps else 0.0,
                        out_end_s=round((start + dur) / fps, 2) if fps else 0.0,
                        has_media=bool(c["path"]),
                        zoom=round(zoom, 4),
                        pos_y_px=round(pos_y, 1),
                        animated=animated,
                        bbox=bbox_from_framing(zoom, 0.0, pos_y, w, h),
                    )
                )

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "dataset.json").write_text(
        json.dumps([asdict(i) for i in items], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return items
