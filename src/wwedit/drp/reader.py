"""`.drp`（ZIP+XML）から最終編集タイムラインを読み込む。"""

from __future__ import annotations

import re
import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Clip", "Timeline", "read_timelines", "keep_ranges_for_day", "remap_path"]

# 既定の正解 .drp（複数日混在モノプロジェクト）
DEFAULT_DRP = "D:/Users/sackn/Videos/wakuwaku/wakuwaku_from_202510.20260608234725 (Copy).drp"

# プロジェクト内パスの旧ドライブ -> 実ドライブ
PATH_REMAP = {"K:": "D:"}

_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})|(\d{4})(\d{2})(\d{2})")
_CLIP_RE = re.compile(r"<(Sm2TiVideoClip|Sm2TiAudioClip)\b.*?</\1>", re.S)


def remap_path(p: str) -> str:
    for old, new in PATH_REMAP.items():
        if p.startswith(old):
            return new + p[len(old) :]
    return p


def _field(block: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", block, re.S)
    return m.group(1) if m else None


def _hex_double(h: str | None) -> float:
    """先頭8バイト(little-endian double)をデコード（MediaFrameRate 等）。"""
    if not h or len(h) < 16:
        return 0.0
    try:
        return struct.unpack("<d", bytes.fromhex(h[:16]))[0]
    except (ValueError, struct.error):
        return 0.0


def _day_from_path(path: str) -> str | None:
    """MediaFilePath から収録日 YYYY-MM-DD を取り出す。"""
    m = _DATE_RE.search(path.replace("\\", "/"))
    if not m:
        return None
    if m.group(1):
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return f"{m.group(4)}-{m.group(5)}-{m.group(6)}"


@dataclass
class Clip:
    track_type: str  # "video" / "audio"
    timeline_start_f: int  # 出力タイムライン上の開始フレーム（= Recut 出力 offset と一致）
    duration_f: int  # 尺（フレーム）
    media_start_f: int  # 注: .drp では常に 0。ソース内イン点は MediaTimemapBA blob 側（未デコード）
    fps: float
    media_path: str  # 実ドライブへ再マップ済み
    day: str | None

    @property
    def out_start_s(self) -> float:
        """出力タイムライン上の開始秒。"""
        return self.timeline_start_f / self.fps if self.fps else 0.0

    @property
    def out_end_s(self) -> float:
        return (self.timeline_start_f + self.duration_f) / self.fps if self.fps else 0.0

    @property
    def duration_s(self) -> float:
        return self.duration_f / self.fps if self.fps else 0.0


@dataclass
class Timeline:
    uuid: str
    clips: list[Clip] = field(default_factory=list)

    @property
    def days(self) -> list[str]:
        return sorted({c.day for c in self.clips if c.day})

    @property
    def primary_day(self) -> str | None:
        days = [c.day for c in self.clips if c.day]
        return max(set(days), key=days.count) if days else None

    @property
    def video_clips(self) -> list[Clip]:
        return [c for c in self.clips if c.track_type == "video"]


def _parse_seqcontainer(uuid: str, xml: str) -> Timeline:
    tl = Timeline(uuid=uuid)
    for m in _CLIP_RE.finditer(xml):
        kind = m.group(1)
        blk = m.group(0)
        path = _field(blk, "MediaFilePath") or ""
        path = remap_path(path)
        fps = _hex_double(_field(blk, "MediaFrameRate")) or 25.0
        try:
            start = int(_field(blk, "Start") or 0)
            dur = int(_field(blk, "Duration") or 0)
            mstart = int(_field(blk, "MediaStartTime") or 0)
        except ValueError:
            continue
        tl.clips.append(
            Clip(
                track_type="video" if kind == "Sm2TiVideoClip" else "audio",
                timeline_start_f=start,
                duration_f=dur,
                media_start_f=mstart,
                fps=fps,
                media_path=path,
                day=_day_from_path(path),
            )
        )
    return tl


def read_timelines(drp_path: str | Path = DEFAULT_DRP) -> list[Timeline]:
    """`.drp` 内の全 SeqContainer タイムラインを読み込む。"""
    timelines: list[Timeline] = []
    with zipfile.ZipFile(drp_path) as z:
        for name in z.namelist():
            if not (name.startswith("SeqContainer/") and name.endswith(".xml")):
                continue
            uuid = Path(name).stem
            xml = z.read(name).decode("utf-8", errors="replace")
            tl = _parse_seqcontainer(uuid, xml)
            if tl.clips:
                timelines.append(tl)
    return timelines


def final_timeline_for_day(
    day: str, drp_path: str | Path = DEFAULT_DRP
) -> Timeline | None:
    """指定日の最終編集タイムライン（その日を主に含み映像クリップが最多のもの）。"""
    timelines = read_timelines(drp_path)
    cands = [t for t in timelines if t.primary_day == day and t.video_clips]
    if not cands:
        return None
    return max(cands, key=lambda t: len(t.video_clips))


def keep_ranges_for_day(
    day: str, drp_path: str | Path = DEFAULT_DRP
) -> list[tuple[float, float]]:
    """指定日の最終編集の **出力タイムライン上**の映像クリップ区間（秒）を返す。

    注: これは出力（カット後）タイムラインの区間であって、ソース上のイン点ではない
    （ソースイン点は MediaTimemapBA blob 側で未デコード）。クリップ数・尺の比較に使う。
    """
    tl = final_timeline_for_day(day, drp_path)
    if tl is None:
        return []
    ranges = [(c.out_start_s, c.out_end_s) for c in tl.video_clips if c.duration_f > 0]
    ranges.sort()
    return ranges
