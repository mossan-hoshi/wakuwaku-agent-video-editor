"""[S3] 本編音声のフェード（本編の頭と尻／各チャプターの前後）。

**後段パスの一番最後に掛ける。** アイキャッチ挿入 → 高速化 → ここ、の順。
高速化は発話の間を最大8倍に詰めるので、その前にフェードを置くと 0.35 秒が 0.04 秒へ
潰れて聞こえなくなる（2026-09-13 の設計判断）。

掛ける場所（`duration` はアイキャッチの尺）:

- 本編の頭 ``[0, body_in)`` で立ち上げ、末尾 ``(total-body_out, total]`` で落とす
- チャプター境界 ``T``（= **そのチャプターのアイキャッチ開始時刻**）ごとに
  ``[T-edge, T)`` で落とし、アイキャッチが終わる ``T+duration`` から ``edge`` 秒で戻す

⚠️ ``afade`` は使えない。``afade=t=in:st=X`` は **X より前を全部無音にする**ので、
1本のトラックへ複数の立ち上がりを重ねられない。``volume`` の式で包絡線を直接書く。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from wwedit.common.media import ffmpeg_error, ffmpeg_path

__all__ = ["fade_volume_expr", "apply_audio_fades", "parse_chapter_seconds"]

#: チャプター境界の前後に掛けるフェードの長さ（秒）。
EDGE_FADE_S = 0.35
#: 本編の頭の立ち上がり／末尾の落とし（秒）。頭は短く、終わりは余韻を残す。
BODY_IN_S = 0.8
BODY_OUT_S = 1.2


def parse_chapter_seconds(lines: list[str]) -> list[float]:
    """``MM:SS タイトル`` / ``H:MM:SS タイトル`` の行から秒を取り出す。"""
    out: list[float] = []
    for ln in lines:
        head = (ln or "").strip().split(" ", 1)[0]
        parts = head.split(":")
        if not (2 <= len(parts) <= 3) or not all(p.isdigit() for p in parts):
            continue
        v = [int(p) for p in parts]
        out.append(v[0] * 60 + v[1] if len(v) == 2 else v[0] * 3600 + v[1] * 60 + v[2])
    return out


def fade_volume_expr(
    boundaries: list[float], total: float, *, eyecatch_dur: float = 2.0,
    edge: float = EDGE_FADE_S, body_in: float = BODY_IN_S,
    body_out: float = BODY_OUT_S,
) -> str:
    """``volume`` フィルタへ渡す包絡線の式を組む（0〜1 の積）。

    ``boundaries`` は**アイキャッチ開始時刻**（秒）。0 付近の先頭章は
    アイキャッチが無いので呼び出し側で除いておくこと。
    """
    terms: list[str] = []
    if body_in > 0:
        terms.append(f"min(1,t/{body_in:.3f})")
    if body_out > 0 and total > body_out:
        terms.append(f"min(1,({total:.3f}-t)/{body_out:.3f})")
    for t0 in boundaries:
        a, b = t0 - edge, t0 + eyecatch_dur
        if a <= 0 or b + edge >= total:
            continue                      # 端に寄りすぎた境界は本編フェードに任せる
        terms.append(
            f"if(between(t,{a:.3f},{t0:.3f}),({t0:.3f}-t)/{edge:.3f},"
            f"if(between(t,{t0:.3f},{b:.3f}),0,"
            f"if(between(t,{b:.3f},{b + edge:.3f}),(t-{b:.3f})/{edge:.3f},1)))"
        )
    return "*".join(terms) if terms else "1"


def apply_audio_fades(
    src_mp4: str | Path, out_path: str | Path, *, boundaries: list[float],
    total: float, eyecatch_dur: float = 2.0, edge: float = EDGE_FADE_S,
    body_in: float = BODY_IN_S, body_out: float = BODY_OUT_S,
) -> Path:
    """音声にだけフェードを掛けた mp4 を書き出す（**映像は再エンコードしない**）。

    映像は ``-c:v copy`` なので画質は1ビットも落ちず、処理も数十秒で終わる。
    """
    src_mp4, out_path = Path(src_mp4).resolve(), Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    expr = fade_volume_expr(boundaries, total, eyecatch_dur=eyecatch_dur,
                            edge=edge, body_in=body_in, body_out=body_out)
    cmd = [
        ffmpeg_path(), "-y", "-i", str(src_mp4),
        "-af", f"volume=volume='{expr}':eval=frame",
        "-map", "0:v", "-map", "0:a", "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k", str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"音声フェード失敗:\n{ffmpeg_error(proc.stderr)}")
    return out_path
