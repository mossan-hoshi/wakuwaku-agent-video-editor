"""[E] 感情エフェクトの合成（ちびキャラの頭の斜め上に漫符を出す）。

ちびキャラと**同じ ffconcat 方式**で左右1本ずつトラックを足す。エフェクトが出ていない
区間は完全に透明なフレームを ``duration`` で置くだけなので、実デコード量はほとんど増えない。

図解（``infographic_overlay``）のように ``-loop 1 -i png`` ＋ ``enable='between(t,…)'``
にしない理由: 感情の切り替わりは1本の動画で数十〜数百回あり、その数だけ入力と overlay 段が
増えてフィルタグラフが破綻する。

⚠️ **エフェクトに ``hflip`` を掛けてはいけない**（「!」「?」が鏡文字になる）。ちび側だけを
反転している現行実装とは**トラックが別**なので自然に分離されるが、ちびのチェーンへ継ぎ足す
実装ミスだけは避けること。既存テストの「``hflip`` はスクリプト全体で1回だけ」がその回帰ガード。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = ["FxSpec", "fx_placement", "fx_side_specs"]


@dataclass(frozen=True)
class FxSpec:
    """1体分のエフェクト合成仕様。"""

    side: str
    ffconcat_path: Path
    x: int
    y: int
    size_px: int
    #: エフェクトが**実際に出ている**出力秒の区間。合成側はここだけ overlay する。
    #: 全長に対して1%未満（実測: 25分の動画で約10秒）なので、これを渡さないと
    #: 何も映っていないフレームまで毎回アルファ合成することになる。
    spans: tuple[tuple[float, float], ...] = ()


def fx_placement(
    side: str, *, out_w: int, out_h: int, chibi_h: int,
    margin: tuple[int, int], scale_frac: float = 0.35,
    offset_x_frac: float = 0.62, offset_y_frac: float = 1.02,
) -> tuple[int, int, int]:
    """エフェクトの ``(x, y, size)`` を**整数で**返す（``W``/``h`` 式を使わずテストしやすく）。

    スプライトのキャンバスは正方形で ``scale=-1:{chibi_h}`` を掛けるので、画面上のちびの箱は
    どのキャラでも ``chibi_h × chibi_h``。それを基準に「頭の斜め上・画面中央寄り」へ置く。
    """
    mx, my = margin
    size = max(8, int(round(chibi_h * scale_frac)))
    y = out_h - my - int(round(chibi_h * offset_y_frac))
    if side == "left":
        x = mx + int(round(chibi_h * offset_x_frac))
    else:
        x = out_w - mx - int(round(chibi_h * offset_x_frac)) - size
    return x, y, size


def fx_side_specs(
    specs, *, tmp_dir: Path, chibi_h: int, total: float,
    out_w: int = 1920, out_h: int = 1080,
    margin: tuple[int, int] = (24, 24), cfg=None,
) -> list[FxSpec]:
    """ちびの ``SideSpec`` 列からエフェクトの ffconcat を作る。

    切替時刻は **``SideSpec.emotions``（出力時刻系の感情トラック）** から取る。
    ``EDL.emotion_cues`` を直接読むと方式Bで位置がずれる。
    """
    from wwedit.chibi.fx import FX_EMOTIONS, FX_FRAMES, fx_frames, fx_idle_frame
    from wwedit.chibi.timeline import _ffconcat_path, emotion_change_times

    scale_frac = getattr(cfg, "fx_scale_frac", 0.35)
    off_x = getattr(cfg, "fx_offset_x_frac", 0.62)
    off_y = getattr(cfg, "fx_offset_y_frac", 1.02)
    duration = float(getattr(cfg, "fx_duration_s", 0.0)) or None

    out: list[FxSpec] = []
    for sp in specs:
        x, y, size = fx_placement(
            sp.side, out_w=out_w, out_h=out_h, chibi_h=chibi_h, margin=margin,
            scale_frac=scale_frac, offset_x_frac=off_x, offset_y_frac=off_y)
        events = [(t, e) for t, e in emotion_change_times(list(sp.emotions))
                  if e in FX_EMOTIONS]
        idle = fx_idle_frame(size)
        step = (duration or 0.0) / FX_FRAMES if duration else None

        lines = ["ffconcat version 1.0"]
        pos = 0.0
        last: Path | None = None
        spans: list[tuple[float, float]] = []
        for t, emo in events:
            if t < pos:
                continue
            if t > pos:
                lines.append(f"file '{_ffconcat_path(idle)}'")
                lines.append(f"duration {t - pos:.5f}")
                last = idle
            frames = fx_frames(emo, size)
            from wwedit.chibi.fx import FX_FPS

            d = step or (1.0 / FX_FPS)
            for fp in frames:
                lines.append(f"file '{_ffconcat_path(fp)}'")
                lines.append(f"duration {d:.5f}")
                last = fp
            spans.append((t, t + d * len(frames)))
            pos = t + d * len(frames)
        if total > pos:
            lines.append(f"file '{_ffconcat_path(idle)}'")
            lines.append(f"duration {total - pos:.5f}")
            last = idle
        if last is not None:
            lines.append(f"file '{_ffconcat_path(last)}'")   # 末尾重複（duration無視対策）

        p = Path(tmp_dir) / f"chibi_fx_{sp.side}.ffconcat"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        out.append(FxSpec(sp.side, p, x, y, size, tuple(spans)))
    return out
