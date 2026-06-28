"""[H] 合成済み本編へ、**各チャプター冒頭に2秒アイキャッチを挿入**する後段パス。

本編の巨大 filtergraph には手を入れず、`compose_kept` で作った mp4 を章境界で分割し、
チャプターごとに生成した generative-art アイキャッチ（`publish.eyecatch`）を割り込ませて
再連結する。挿入で出力タイムラインが後ろにずれるため、**概要欄チャプター時刻も
`shifted_chapter_lines` で同じ量だけ補正**する（純関数・テスト可能）。

タイムライン: ``[EC0][ch0本編][EC1][ch1本編]…``。アイキャッチ i 自体が章 i の頭出しマーカー
なので、章 i の YouTube 時刻 = 元出力時刻 + i×duration（先頭章は 00:00 のまま）。
"""

from __future__ import annotations

import random
import subprocess
import tempfile
from pathlib import Path

from wwedit.common.media import ffmpeg_path
from wwedit.compose.ffmpeg_compose import _src_to_out
from wwedit.edl.schema import Edl, TimeRange

__all__ = [
    "eyecatch_boundaries",
    "shifted_chapter_lines",
    "insert_eyecatches",
]

_AUDIO_EXT = (".wav", ".mp3", ".m4a", ".flac", ".ogg")


def eyecatch_boundaries(
    edl: Edl, ranges: list[TimeRange] | None = None, *, duration: float = 2.0
) -> tuple[list[dict], float]:
    """アイキャッチを挿す章境界を**出力タイムライン秒**で返す（挿入前の時刻）。

    返り値 ``(boundaries, total)``。各 boundary は ``{"out_at","title","index"}``。
    先頭章は 00:00 にスナップ。カットで同一出力時刻へ潰れた章・範囲外の章は除外。
    ``ranges`` 未指定なら収録まるごと（kept_ranges）。投稿単位[K]はその単位の区間を渡す。
    """
    rgs = ranges if ranges is not None else edl.kept_ranges()
    total = sum(r.duration for r in rgs)
    chs = sorted(edl.chapters, key=lambda c: c.start_at)
    out: list[dict] = []
    seen: set[float] = set()
    for i, c in enumerate(chs):
        ot = 0.0 if i == 0 else _src_to_out(rgs, c.start_at)
        if ot < -1e-6 or ot >= total - 1e-6:
            continue  # 範囲外 / 末尾（後ろに本編が無い）はアイキャッチ不要
        key = round(ot, 2)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "out_at": ot,
            "title": c.chapter_title or f"チャプター{len(out) + 1}",
            "index": len(out),
        })
    return out, total


def _fmt_ts(t: float) -> str:
    h, rem = divmod(int(t + 1e-6), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def shifted_chapter_lines(
    edl: Edl, ranges: list[TimeRange] | None = None, *, duration: float = 2.0
) -> list[str]:
    """アイキャッチ挿入後の出力時刻に補正したチャプター行（``MM:SS タイトル``）。

    i 番目の章は前に i 個のアイキャッチ（各 ``duration`` 秒）が入るので ``out_at + i×duration``。
    アイキャッチ自体が章マーカーなので時刻はその開始＝補正後の章頭を指す。先頭は 00:00。
    """
    bounds, _ = eyecatch_boundaries(edl, ranges, duration=duration)
    return [f"{_fmt_ts(b['out_at'] + i * duration)} {b['title']}"
            for i, b in enumerate(bounds)]


def _pick_jingle(jingle_dir: Path, seed: int) -> Path | None:
    cands = sorted(p for p in jingle_dir.rglob("*") if p.suffix.lower() in _AUDIO_EXT)
    return random.Random(seed).choice(cands) if cands else None


def insert_eyecatches(
    main_mp4: str | Path,
    edl: Edl,
    out_path: str | Path,
    *,
    ranges: list[TimeRange] | None = None,
    jingle_dir: str | Path | None = None,
    duration: float = 2.0,
    seed_base: int = 0,
    out_w: int = 1920,
    out_h: int = 1080,
    crf: int = 20,
    preset: str = "medium",
) -> tuple[Path, list[str]]:
    """本編 mp4 の各章境界へアイキャッチを挿入した mp4 を書き出す。

    章ごとに ``publish.eyecatch.generate_eyecatch``（seed=seed_base+i・``jingle_dir`` から
    seed でランダム選曲）でクリップを作り、本編を境界で trim 分割して concat フィルタで再連結。
    fps/音声フォーマットを揃えて全体を1回再エンコードする（クリップ間のドリフト無し）。
    返り値 ``(out_path, chapter_lines)``（chapter_lines=補正済み概要欄用）。
    """
    from wwedit.publish.eyecatch import generate_eyecatch

    main_mp4 = Path(main_mp4).resolve()
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    bounds, total = eyecatch_boundaries(edl, ranges, duration=duration)
    if not bounds:
        raise ValueError("挿入対象の章境界が無い（chapters を確認）")

    fps = int(round(edl.source.fps or 30)) or 30
    work = Path(tempfile.mkdtemp())
    jdir = Path(jingle_dir) if jingle_dir else None

    # 章ごとにアイキャッチ生成（seed で見た目が変わる・ジングルも seed で選曲）
    ec_paths: list[Path] = []
    for b in bounds:
        seed = seed_base + b["index"]
        jingle = _pick_jingle(jdir, seed) if jdir and jdir.exists() else None
        ec = generate_eyecatch(
            b["title"], work / f"ec_{b['index']:02d}.mp4",
            seed=seed, jingle=str(jingle) if jingle else None,
            duration=duration, out_w=out_w, out_h=out_h, fps=fps,
        )
        ec_paths.append(ec)

    # 入力: 0=本編 / 1..N=アイキャッチ
    cmd = [ffmpeg_path(), "-y", "-i", str(main_mp4)]
    for ec in ec_paths:
        cmd += ["-i", str(ec)]

    af = ("aresample=48000,aformat=sample_fmts=fltp:"
          "channel_layouts=stereo,asetpts=PTS-STARTPTS")
    vf = f"scale={out_w}:{out_h},fps={fps},setpts=PTS-STARTPTS,format=yuv420p"
    chains: list[str] = []
    order: list[str] = []
    edges = [b["out_at"] for b in bounds] + [total]  # 章頭…末尾
    for i in range(len(bounds)):
        lo, hi = edges[i], edges[i + 1]
        # アイキャッチ i（入力 i+1）
        chains.append(f"[{i + 1}:v]{vf}[ecv{i}]")
        chains.append(f"[{i + 1}:a]{af}[eca{i}]")
        # 本編セグメント i = [lo,hi)
        chains.append(f"[0:v]trim={lo:.3f}:{hi:.3f},{vf}[sv{i}]")
        chains.append(f"[0:a]atrim={lo:.3f}:{hi:.3f},{af}[sa{i}]")
        order += [f"[ecv{i}][eca{i}]", f"[sv{i}][sa{i}]"]
    n = len(order)
    chains.append(f"{''.join(order)}concat=n={n}:v=1:a=1[outv][outa]")
    script = ";\n".join(chains)

    sp = work / "ec_insert.ffscript"
    sp.write_text(script, encoding="utf-8")
    cmd += [
        "-filter_complex_script", str(sp),
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-c:a", "aac", "-b:a", "192k", str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").splitlines()[-20:])
        raise RuntimeError(f"アイキャッチ挿入失敗:\n{tail}")
    return out_path, shifted_chapter_lines(edl, ranges, duration=duration)
