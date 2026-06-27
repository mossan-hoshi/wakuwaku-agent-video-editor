"""`.drp` のクリップ/調整クリップから**フレーミング（reframe transform）**を抽出する。

最近(2026)の編集はフレーミング皆無だが、2025の編集は調整クリップ＋メディアクリップに
共通エフェクト GUID ``8128b52ffd20…`` を乗せて reframe している。そのパラメータ（zoom/pan）は
``EffectFiltersBA`` の protobuf 風バイナリに fixed64 double として入っている。

これは「画面のどの領域に寄せたか」のフレーミング正解＝[E]フレーミングモデルの教師データ。

protobuf風 fixed64 として格納（2026-05-10 video1655105088 を Resolve実測で校正）:
- **field2 = Zoom**（生値。例 2.21 / 1.32 が厳密一致）。crop と共用のため最大値を採る。
- **field5 = Position Y**（高さ正規化。値×1080=px。例 0.0991×1080=107px）
- field4/6 = Position X（校正クリップでは0で未出現）
- Crop は本収録では固定（top 86px / bottom 38.7px ＝レターボックス除去）でフレーミング判断ではない。
"""

from __future__ import annotations

import re
import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from wwedit.drp.reader import (
    DEFAULT_DRP,
    final_timeline_for_day,
    remap_path,
)

__all__ = [
    "FRAMING_EFFECT_GUID",
    "FIELD_ZOOM",
    "FIELD_POS_Y",
    "NORM_H",
    "FramingClip",
    "parse_effect_params",
    "framing_clips_for_timeline",
    "framing_clips_for_day",
]

FRAMING_EFFECT_GUID = bytes.fromhex("8128b52ffd20")
FIELD_ZOOM = 2  # protobuf field2 fixed64 = Zoom（生値）
FIELD_POS_Y = 5  # field5 fixed64 = Position Y（高さ正規化）
FIELD_POS_X = 4  # 推定（校正クリップで0のため未確定）
NORM_H = 1080.0  # Position/Crop の正規化基準（フレーム高さ）

_CLIP_RE = re.compile(r"<Sm2TiVideoClip\b.*?</Sm2TiVideoClip>", re.S)


def _varint(b: bytes, i: int) -> tuple[int, int]:
    r = s = 0
    while i < len(b):
        v = b[i]
        r |= (v & 0x7F) << s
        i += 1
        if not v & 0x80:
            break
        s += 7
    return r, i


def parse_effect_params(effect_hex: str) -> dict[int, list[float]]:
    """EffectFiltersBA(hex) から {protobuf field番号: [fixed64 double,...]} を抽出する。

    GUID以降のバイト列を走査し、fixed64ワイヤ(タグ下位3bit=1)の double をフィールド番号別に集める。
    field2=Zoom, field5=Position Y（[[resolve-drp-ground-truth]]の校正値）。
    """
    try:
        bb = bytes.fromhex(effect_hex.strip())
    except ValueError:
        return {}
    gi = bb.find(FRAMING_EFFECT_GUID)
    if gi < 0:
        return {}
    # GUID以降を全offset走査し、「前バイトが fixed64 ワイヤタグ(下位3bit=1)」の double を採用。
    # 木構造は復元せず、フィールド番号(=タグ>>3)別に集める（厳密パースは同期を崩すため避ける）。
    out: dict[int, list[float]] = {}
    start = gi + len(FRAMING_EFFECT_GUID)
    for o in range(start + 1, len(bb) - 7):
        tagb = bb[o - 1]
        if tagb & 7 != 1:  # fixed64 ワイヤのみ
            continue
        val = struct.unpack_from("<d", bb, o)[0]
        if val != val or abs(val) >= 1e6:
            continue
        out.setdefault(tagb >> 3, []).append(val)
    return out


@dataclass
class FramingClip:
    """1クリップ（メディア or 調整）に乗ったフレーミング transform。"""

    timeline_start_f: int
    duration_f: int
    fps: float
    has_media: bool  # False=調整クリップ
    media_path: str | None
    params: dict[int, list[float]] = field(default_factory=dict)

    def _first(self, field_num: int) -> float | None:
        v = self.params.get(field_num)
        return v[0] if v else None

    @property
    def zoom(self) -> float | None:
        """拡大率（生値）。field2 は Zoom と crop で共用されるため、寄せを表す最大値を採る。"""
        v = self.params.get(FIELD_ZOOM)
        return max(v) if v else None

    @property
    def pos_y_px(self) -> float | None:
        """Position Y（ピクセル換算。高さ1080正規化値×1080）。"""
        v = self._first(FIELD_POS_Y)
        return v * NORM_H if v is not None else None

    @property
    def pos_x_px(self) -> float | None:
        v = self._first(FIELD_POS_X)
        return v * NORM_H if v is not None else None

    @property
    def out_start_s(self) -> float:
        return self.timeline_start_f / self.fps if self.fps else 0.0

    @property
    def out_end_s(self) -> float:
        return (self.timeline_start_f + self.duration_f) / self.fps if self.fps else 0.0

    @property
    def is_reframed(self) -> bool:
        """実際に寄せ（ズームイン）しているか。crop固定のみのクリップは除外。"""
        z = self.zoom
        return z is not None and z > 1.05


def _field(block: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", block, re.S)
    return m.group(1) if m else None


def _hex_double(h: str | None) -> float:
    if not h or len(h) < 16:
        return 0.0
    try:
        return struct.unpack("<d", bytes.fromhex(h[:16]))[0]
    except (ValueError, struct.error):
        return 0.0


def framing_clips_for_timeline(
    uuid: str, drp_path: str | Path = DEFAULT_DRP
) -> list[FramingClip]:
    """指定タイムラインの、フレーミングエフェクトが乗った全クリップを返す。"""
    with zipfile.ZipFile(drp_path) as z:
        name = next(
            (n for n in z.namelist() if n.startswith(f"SeqContainer/{uuid}")), None
        )
        if not name:
            return []
        xml = z.read(name).decode("utf-8", errors="replace")

    clips: list[FramingClip] = []
    for m in _CLIP_RE.finditer(xml):
        blk = m.group(0)
        eff = _field(blk, "EffectFiltersBA") or ""
        params = parse_effect_params(eff)
        if not params:
            continue
        path = (_field(blk, "MediaFilePath") or "").strip()
        try:
            start = int(_field(blk, "Start") or 0)
            dur = int(_field(blk, "Duration") or 0)
        except ValueError:
            continue
        clips.append(
            FramingClip(
                timeline_start_f=start,
                duration_f=dur,
                fps=_hex_double(_field(blk, "MediaFrameRate")) or 25.0,
                has_media=bool(path),
                media_path=remap_path(path) if path else None,
                params=params,
            )
        )
    clips.sort(key=lambda c: c.timeline_start_f)
    return clips


def framing_clips_for_day(
    day: str, drp_path: str | Path = DEFAULT_DRP
) -> list[FramingClip]:
    """指定日の最終タイムラインのフレーミングクリップ群。"""
    tl = final_timeline_for_day(day, drp_path)
    if tl is None:
        return []
    return framing_clips_for_timeline(tl.uuid, drp_path)
