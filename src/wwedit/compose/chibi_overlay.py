"""compose へのちびキャラ統合（左下・右下の2体・話者側だけ口パク）。

**ffconcat PNG プレイリスト方式**: スプライトPNGを並べたテキストを ``-f concat`` の動画入力
として渡す。PNGデコーダが RGBA を返すのでアルファ付きコーデック・事前レンダは不要。
compose_kept へは「入力2本＋overlay 2段」を足すだけ（モザイク後・字幕前＝UIレイヤー）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from wwedit.compose.ffmpeg_compose import out_total
from wwedit.edl.schema import Edl, TimeRange

__all__ = ["SideSpec", "chibi_sides", "chibi_side_specs"]


@dataclass(frozen=True)
class SideSpec:
    """1体分の合成仕様。``x_expr``/``y_expr`` は overlay のピクセル式。"""

    side: str            # "left" | "right"
    speaker: str
    char: str
    ffconcat_path: Path
    x_expr: str
    y_expr: str
    flip: bool = False   # 左右反転（2体を対面させるため）


def chibi_sides(edl: Edl) -> list[tuple[str, str, str]]:
    """(side, speaker, char) の並び。EDL.chibi.sides 優先、無ければ話者名ソートで左→右。"""
    cast = edl.character_cast or {}
    speakers = sorted(cast)
    sides_cfg = (edl.chibi.sides if edl.chibi else {}) or {}
    left = sides_cfg.get("left") or (speakers[0] if speakers else None)
    right = sides_cfg.get("right") or next(
        (s for s in speakers if s != left), None)
    out: list[tuple[str, str, str]] = []
    if left and left in cast:
        out.append(("left", left, cast[left]))
    if right and right in cast:
        out.append(("right", right, cast[right]))
    return out


def chibi_side_specs(
    edl: Edl, ranges: list[TimeRange], *,
    tmp_dir: Path, margin: tuple[int, int] | None = None,
    mouth_step: float | None = None, data_dir: Path | None = None,
) -> list[SideSpec]:
    """左右2体のちびキャラの ffconcat と overlay 式を作る（compose_kept から呼ばれる）。

    方式B（meta.voice.method=="tts"）で ``data_dir/voice_tts_report.json`` があれば
    クリップ実尺から発話スパンを取り、無ければ word タイミングにフォールバックする。

    ちび素材はどれもほぼ正面向きだが、体の傾き・小物の位置に左右差がある。2体をそのまま
    並べると同じ側を向いて見えるので、``EDL.chibi.flip_sides``（既定 ``["left"]``）の側を
    左右反転して**対面**させる。素材に文字が入ると反転で読めなくなるので、その時は空にする。
    """
    from wwedit.chibi.assets import char_dir, mouth_pair_paths
    from wwedit.chibi.timeline import (
        MOUTH_STEP_S,
        build_side_timeline,
        emotion_track_from_report,
        speaking_spans_from_report,
        write_ffconcat,
    )

    frz = tuple(edl.freezes or ())
    total = out_total(ranges, frz)
    mx, my = margin or (edl.chibi.margin_px if edl.chibi else (24, 24))
    step = mouth_step or MOUTH_STEP_S
    flip_sides = set(edl.chibi.flip_sides if edl.chibi else ["left"])

    report_rows: list[dict] | None = None
    voice_meta = edl.meta.get("voice") or {}
    if voice_meta.get("method") == "tts" and data_dir:
        # [S2] 時間ワープ後は読み上げの出力位置が変わっている。**ワープ後のレポート**を
        # 見ないと口パクだけ元の位置に残る（字幕は EDL 側なので合っているのに口だけずれる）。
        names = (["warped_voice_tts_report.json"] if voice_meta.get("warped") else []) \
            + ["voice_tts_report.json"]
        for name in names:
            rp = Path(data_dir) / name
            if rp.exists():
                report_rows = json.loads(rp.read_text(encoding="utf-8"))["rows"]
                break

    specs: list[SideSpec] = []
    for side, speaker, char in chibi_sides(edl):
        # アセット実在チェック（無い感情は normal へ落とす）
        available = {
            d.name for d in char_dir(char).glob("*/")
            if all(p.exists() for p in mouth_pair_paths(char, d.name))
        }
        if "normal" not in available:
            raise FileNotFoundError(
                f"{char} の normal スプライトが無い（wwedit chibi ensure を先に）")
        # 方式Bは口パクも感情も**読み上げクリップの位置**に合わせる（元発話位置ではない）
        spans = emotions = None
        if report_rows is not None:
            spans = speaking_spans_from_report(report_rows, ranges, speaker, freezes=frz)
            emotions = emotion_track_from_report(
                edl, report_rows, ranges, speaker, total, freezes=frz)
        intervals = build_side_timeline(
            edl, ranges, speaker, total=total, freezes=frz, spans=spans,
            emotions=emotions, step=step)
        ffc = write_ffconcat(
            intervals, char, Path(tmp_dir) / f"chibi_{side}.ffconcat",
            available_emotions=available)
        x = f"{mx}" if side == "left" else f"W-w-{mx}"
        specs.append(SideSpec(side, speaker, char, ffc, x, f"H-h-{my}",
                              flip=side in flip_sides))
    return specs
