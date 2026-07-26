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
    # 各章の出力開始秒（0..total にクランプ）。i==0 の特別扱いはしない。
    pts = [(min(max(_src_to_out(rgs, c.start_at), 0.0), total), c) for c in chs]
    out: list[dict] = []
    for j, (ot, c) in enumerate(pts):
        oe = pts[j + 1][0] if j + 1 < len(pts) else total
        # 尺ゼロ（カットで潰れた章／末尾に本編が無い章）は捨て、その位置で実際に流れる
        # 後続の章を残す（例: 冒頭カット時は intro 章でなく実映の次章がそこを担当）。
        if oe - ot <= 1e-3:
            continue
        out.append({
            "out_at": ot,
            "title": c.chapter_title or f"チャプター{len(out) + 1}",
            "index": len(out),
        })
    if out and out[0]["out_at"] > 1e-6:
        out[0]["out_at"] = 0.0  # 先頭章は 00:00 にスナップ
    return out, total


def _fmt_ts(t: float) -> str:
    h, rem = divmod(int(t + 1e-6), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def shifted_chapter_lines(
    edl: Edl, ranges: list[TimeRange] | None = None, *, duration: float = 2.0,
    skip_first: bool = True,
) -> list[str]:
    """アイキャッチ挿入後の出力時刻に補正したチャプター行（``MM:SS タイトル``）。

    各章マーカー = その章のアイキャッチ開始（無い章は本編開始）＝
    ``out_at + (その章より前に入ったアイキャッチ数)×duration``。
    ``skip_first`` の時は先頭章にアイキャッチを入れない（イントロが頭に来るため）ので、
    先頭は 00:00、以降は手前のアイキャッチ数だけずれる。
    """
    bounds, _ = eyecatch_boundaries(edl, ranges, duration=duration)
    lines: list[str] = []
    n_ec_before = 0
    for i, b in enumerate(bounds):
        lines.append(f"{_fmt_ts(b['out_at'] + n_ec_before * duration)} {b['title']}")
        if not (skip_first and i == 0):
            n_ec_before += 1
    return lines


def _pick_jingle(jingle_dir: Path, seed: int) -> Path | None:
    cands = sorted(p for p in jingle_dir.rglob("*") if p.suffix.lower() in _AUDIO_EXT)
    return random.Random(seed).choice(cands) if cands else None


def _synth_voice(out_wav: Path, seed: int) -> tuple[Path | None, str]:
    """章 seed でキャラ＋一言を選び合成する。失敗したら ``(None, "")``（音楽へフォールバック）。"""
    from wwedit.publish.character import full_name
    from wwedit.publish.eyecatch_voice import synth_eyecatch_voice

    try:
        wav, char, disp, _dur = synth_eyecatch_voice(out_wav, seed=seed)
    except Exception as e:  # SBV2サーバ未起動など。アイキャッチ自体は出す。
        print(f"  [warn] アイキャッチ音声の合成に失敗（音楽へ退避）: {e}")
        return (None, "")
    print(f"  アイキャッチ音声: {full_name(char)}「{disp}」")
    return (wav, full_name(char))


def insert_eyecatches(
    main_mp4: str | Path,
    edl: Edl,
    out_path: str | Path,
    *,
    ranges: list[TimeRange] | None = None,
    jingle_dir: str | Path | None = None,
    voice: bool = True,
    duration: float = 2.0,
    seed_base: int = 0,
    out_w: int = 1920,
    out_h: int = 1080,
    crf: int = 20,
    preset: str = "medium",
    skip_first: bool = True,
) -> tuple[Path, list[str]]:
    """本編 mp4 の各章境界へアイキャッチを挿入した mp4 を書き出す。

    章ごとに ``publish.eyecatch.generate_eyecatch``（seed=seed_base+i）でクリップを作り、
    本編を境界で trim 分割して concat フィルタで再連結。fps/音声フォーマットを揃えて全体を
    1回再エンコードする（クリップ間のドリフト無し）。

    **音は既定でのべつべ！キャラの一言ボイス**（``voice=True``・章ごとにキャラと台詞が変わり、
    右上にロゴ＋キャラ名が出る）。SBV2サーバが無いなど合成に失敗したら ``jingle_dir`` の
    音楽へフォールバックする（アイキャッチ自体は必ず出す）。
    返り値 ``(out_path, chapter_lines)``（chapter_lines=補正済み概要欄用）。
    """
    from wwedit.publish.eyecatch import generate_eyecatch

    main_mp4 = Path(main_mp4).resolve()
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    bounds, total = eyecatch_boundaries(edl, ranges, duration=duration)
    if not bounds:
        raise ValueError("挿入対象の章境界が無い（chapters を確認）")

    # skip_first: 先頭章(out_at≈0)はアイキャッチ無し（イントロが頭に来るため）
    ec_chapters = [i for i in range(len(bounds))
                   if not (skip_first and i == 0 and bounds[i]["out_at"] <= 1e-6)]
    if not ec_chapters:
        raise ValueError("挿入対象のアイキャッチが無い（先頭以外の章が必要）")

    fps = int(round(edl.source.fps or 30)) or 30
    work = Path(tempfile.mkdtemp())
    jdir = Path(jingle_dir) if jingle_dir else None

    # アイキャッチ生成（対象章のみ）。ffmpeg入力番号 = 生成順に 1..M（0=本編）
    cmd = [ffmpeg_path(), "-y", "-i", str(main_mp4)]
    ec_input_of: dict[int, int] = {}
    for n_ec, i in enumerate(ec_chapters):
        b = bounds[i]
        seed = seed_base + i
        vwav, vname = (None, "")
        if voice:
            vwav, vname = _synth_voice(work / f"ec_{i:02d}.wav", seed)
        jingle = None
        if vwav is None and jdir and jdir.exists():
            jingle = _pick_jingle(jdir, seed)
        ec = generate_eyecatch(
            b["title"], work / f"ec_{i:02d}.mp4",
            seed=seed, jingle=str(jingle) if jingle else None,
            voice=str(vwav) if vwav else None, voice_name=vname,
            duration=duration, out_w=out_w, out_h=out_h, fps=fps,
        )
        cmd += ["-i", str(ec)]
        ec_input_of[i] = n_ec + 1

    af = ("aresample=48000,aformat=sample_fmts=fltp:"
          "channel_layouts=stereo,asetpts=PTS-STARTPTS")
    vf = f"scale={out_w}:{out_h},fps={fps},setpts=PTS-STARTPTS,format=yuv420p"
    chains: list[str] = []
    order: list[str] = []
    edges = [b["out_at"] for b in bounds] + [total]  # 章頭…末尾
    for i in range(len(bounds)):
        lo, hi = edges[i], edges[i + 1]
        # 章 i のアイキャッチ（先頭スキップ章には無い）→ 本編セグメント i = [lo,hi)
        if i in ec_input_of:
            inp = ec_input_of[i]
            chains.append(f"[{inp}:v]{vf}[ecv{i}]")
            chains.append(f"[{inp}:a]{af}[eca{i}]")
            order.append(f"[ecv{i}][eca{i}]")
        chains.append(f"[0:v]trim={lo:.3f}:{hi:.3f},{vf}[sv{i}]")
        chains.append(f"[0:a]atrim={lo:.3f}:{hi:.3f},{af}[sa{i}]")
        order.append(f"[sv{i}][sa{i}]")
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
    return out_path, shifted_chapter_lines(edl, ranges, duration=duration,
                                           skip_first=skip_first)
