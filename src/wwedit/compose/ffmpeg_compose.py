"""EDL の keep区間を ffmpeg で trim+concat し、1本の mp4 に合成する（[J] 最小実装）。

M1 薄いE2E 用: フレーミング/字幕/BGM/話者整音はまだ載せず、**映像内蔵音声のまま**
無音カットだけを適用した動画を作る。多数区間でもコマンド長制限に当たらないよう
``-filter_complex_script`` でフィルタをファイル渡しする。

フレーム精度は trim/atrim の秒指定＋再エンコードで担保（A/Vを同一フィルタグラフで
concat するためドリフトしない）。
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from wwedit.common.media import ffmpeg_path
from wwedit.edl.schema import Edl, Subtitle, TimeRange

__all__ = [
    "build_filter_script",
    "build_filter_script_framed",
    "framing_crop_filter",
    "bbox_at",
    "loading_overlay_intervals",
    "build_framed_overlay_script",
    "subtitles_to_output",
    "build_audio_filter_script",
    "build_speaker_mix_filter",
    "render_speaker_mix",
    "compose_kept",
    "compose_audio_kept",
]

# YouTube向けラウドネス目標（EBU R128）。割れない範囲でほどほどに大きく（plan [F]）。
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"
# [F] 30秒粒度の窓ノーマライズ（dynaudnorm）。全長一括だと一部が小さすぎるのを防ぐ。
# g=61 フレーム×f=500ms ≒ 30.5s 窓、m=7 で無音/ノイズ床の過増幅を抑制、p=0.9 ピーク目標。
DYNNORM = "dynaudnorm=f=500:g=61:m=7:p=0.9"


def build_speaker_mix_filter(n: int, *, windowed: bool = True) -> str:
    """話者 n トラックの整音 filter_complex を作る。

    ``windowed``: True で各トラックに **30秒粒度の窓ノーマライズ(dynaudnorm)** を先に掛けて
    区間ごとの音量ムラを均し、ミックス後に全体ラウドネスを LOUDNORM で YouTube 目標へ。
    False は従来の全長一括 LOUDNORM のみ（動作確認用）。無音はカットで除外済み・dynaudnorm の
    m 制限で無音の過増幅も抑える。
    """
    if n < 1:
        raise ValueError("トラックが無い")
    pre = (lambda i: f"[{i}:a]{DYNNORM}[d{i}]") if windowed else None
    src = (lambda i: f"[d{i}]") if windowed else (lambda i: f"[{i}:a]")
    lines: list[str] = []
    if windowed:
        lines += [pre(i) for i in range(n)]
    if n == 1:
        lines.append(f"{src(0)}{LOUDNORM}[outa]")
    else:
        mix = "".join(src(i) for i in range(n))
        lines.append(f"{mix}amix=inputs={n}:normalize=0[mix]")
        lines.append(f"[mix]{LOUDNORM}[outa]")
    return ";".join(lines)


def build_filter_script(
    ranges: list[TimeRange], *, vsrc: str = "0:v", asrc: str = "0:a"
) -> str:
    """keep区間列から filter_complex スクリプト本文を作る（v+a を trim→concat）。

    ``vsrc``/``asrc``: 映像/音声の入力ラベル。音声を別ファイル（整音済み）にする場合は
    ``asrc="1:a"`` を渡す。
    """
    lines: list[str] = []
    labels: list[str] = []
    for i, r in enumerate(ranges):
        # set/asetpts でPTSを各区間ローカルに振り直し、concatの連結を正しくする
        lines.append(
            f"[{vsrc}]trim=start={r.start:.3f}:end={r.end:.3f},setpts=PTS-STARTPTS[v{i}];"
        )
        lines.append(
            f"[{asrc}]atrim=start={r.start:.3f}:end={r.end:.3f},asetpts=PTS-STARTPTS[a{i}];"
        )
        labels.append(f"[v{i}][a{i}]")
    n = len(ranges)
    lines.append(f"{''.join(labels)}concat=n={n}:v=1:a=1[outv][outa]")
    return "\n".join(lines)


def framing_crop_filter(
    bbox: tuple[int, int, int, int] | None, out_w: int = 1920, out_h: int = 1080
) -> str:
    """フレーミング bbox (x,y,w,h)px → crop+scale フィルタ文字列。

    bbox None / 退化 / 全画面相当は crop 無しの ``scale`` のみ（no_crop）。crop ありは
    指定矩形に切り出してから出力解像度へ拡大（メイン領域へ寄せる）。
    """
    if bbox is None:
        return f"scale={out_w}:{out_h}"
    x, y, w, h = bbox
    if w <= 0 or h <= 0:
        return f"scale={out_w}:{out_h}"
    return f"crop={w}:{h}:{x}:{y},scale={out_w}:{out_h}"


def bbox_at(edl: Edl, t: float) -> tuple[int, int, int, int] | None:
    """時刻 t を含むフレーミング区間の bbox を返す（無ければ None）。"""
    for r in edl.framing:
        if r.start <= t < r.end:
            return r.bbox
    return None


def build_filter_script_framed(
    edl: Edl,
    ranges: list[TimeRange],
    *,
    vsrc: str = "0:v",
    asrc: str = "0:a",
    out_w: int = 1920,
    out_h: int = 1080,
    vout: str = "outv",
    aout: str = "outa",
) -> str:
    """keep区間ごとに、その区間中点のフレーミング bbox で crop+scale を適用して concat する。

    各区間の映像は trim→crop(メイン領域)→scale(出力解像度) の順。フレーミング未割当
    （bbox None）の区間は scale のみ＝全画面。映像/音声を同一グラフで concat しドリフト無し。
    ``vout``/``aout`` で最終ラベルを変えられる（上位レイヤーのoverlay土台にする時に使う）。
    """
    lines: list[str] = []
    labels: list[str] = []
    for i, r in enumerate(ranges):
        vf = framing_crop_filter(bbox_at(edl, (r.start + r.end) / 2), out_w, out_h)
        lines.append(
            f"[{vsrc}]trim=start={r.start:.3f}:end={r.end:.3f},"
            f"setpts=PTS-STARTPTS,{vf}[v{i}];"
        )
        lines.append(
            f"[{asrc}]atrim=start={r.start:.3f}:end={r.end:.3f},asetpts=PTS-STARTPTS[a{i}];"
        )
        labels.append(f"[v{i}][a{i}]")
    n = len(ranges)
    lines.append(f"{''.join(labels)}concat=n={n}:v=1:a=1[{vout}][{aout}]")
    return "\n".join(lines)


def _src_to_out(ranges: list[TimeRange], t: float) -> float:
    """ソース時刻 t を、keep区間を連結した出力タイムライン秒へ変換する（``ranges`` 基準）。"""
    acc = 0.0
    for r in ranges:
        if t < r.start:
            return acc
        if t <= r.end:
            return acc + (t - r.start)
        acc += r.end - r.start
    return acc


def subtitles_to_output(
    subtitles: list[Subtitle], ranges: list[TimeRange]
) -> list[Subtitle]:
    """字幕(ソース時刻)を出力タイムライン時刻へ変換する（カット連結後の焼き込み用）。

    完全にカット区間内（start と end が同じ出力時刻へ潰れる）字幕は除外。EDL本体はソース時刻
    のまま保持し、レンダリング時にのみ変換する（非破壊）。
    """
    out: list[Subtitle] = []
    for s in subtitles:
        os_, oe = _src_to_out(ranges, s.start), _src_to_out(ranges, s.end)
        if oe - os_ <= 1e-3:
            continue
        out.append(
            Subtitle(start=os_, end=oe, text=s.text, style=s.style, speaker=s.speaker)
        )
    return out


def loading_overlay_intervals(
    edl: Edl, ranges: list[TimeRange], *, default_label: str = "画面を準備"
) -> list[dict]:
    """loading 区間 × keep区間 の重なりを、**出力タイムライン**上の overlay 区間に変換する。

    返す各要素: ``{"out_start","out_end","label"}``（出力秒）。元 footage は EDL に残したまま、
    レンダリング時にこの区間へローディング画面を**最上位レイヤーとして重ねる**（非破壊）。
    loading 区間がカットで分断される場合は keep区間ごとに分けて複数返す。
    """
    loadings = [r for r in edl.framing if r.kind == "loading"]
    out: list[dict] = []
    for lr in loadings:
        label = lr.loading_label or default_label
        for r in ranges:
            a, b = max(lr.start, r.start), min(lr.end, r.end)
            if b <= a:
                continue
            out.append(
                {
                    "out_start": _src_to_out(ranges, a),
                    "out_end": _src_to_out(ranges, b),
                    "label": label,
                }
            )
    out.sort(key=lambda x: x["out_start"])
    return out


def build_framed_overlay_script(
    edl: Edl,
    ranges: list[TimeRange],
    intervals: list[dict],
    loading_vlabels: list[str],
    *,
    vsrc: str = "0:v",
    asrc: str = "0:a",
    out_w: int = 1920,
    out_h: int = 1080,
) -> str:
    """framed concat を土台に、loading クリップを出力タイムラインへ overlay 合成する。

    ``intervals``: loading_overlay_intervals の結果（出力秒）。``loading_vlabels``: 各 interval に
    対応する ffmpeg 入力映像ラベル（例 ``"2:v"``）。各クリップは setpts で out_start へ遅延させ、
    ``enable='between(t,...)'`` でその区間だけ最上位に被せる（元映像は下に残る＝非破壊）。
    """
    base = build_filter_script_framed(
        edl, ranges, vsrc=vsrc, asrc=asrc, out_w=out_w, out_h=out_h,
        vout="base0", aout="outa",
    )
    # 各 filterchain は末尾 ';' を付けず、最後に ";\n" で結合する（区切りを確実にする）。
    chains: list[str] = [base]
    prev = "base0"
    for k, (iv, vl) in enumerate(zip(intervals, loading_vlabels, strict=False)):
        os_, oe = iv["out_start"], iv["out_end"]
        nxt = "outv" if k == len(intervals) - 1 else f"ov{k}"
        chains.append(f"[{vl}]scale={out_w}:{out_h},setpts=PTS+{os_:.3f}/TB[L{k}]")
        chains.append(
            f"[{prev}][L{k}]overlay=eof_action=pass:"
            f"enable='between(t,{os_:.3f},{oe:.3f})'[{nxt}]"
        )
        prev = nxt
    if not intervals:
        chains.append("[base0]null[outv]")
    return ";\n".join(chains)


def build_audio_filter_script(ranges: list[TimeRange], *, asrc: str = "0:a") -> str:
    """音声のみ keep区間を atrim→concat する filter_complex スクリプト本文。"""
    lines: list[str] = []
    labels: list[str] = []
    for i, r in enumerate(ranges):
        lines.append(
            f"[{asrc}]atrim=start={r.start:.3f}:end={r.end:.3f},asetpts=PTS-STARTPTS[a{i}];"
        )
        labels.append(f"[a{i}]")
    lines.append(f"{''.join(labels)}concat=n={len(ranges)}:v=0:a=1[outa]")
    return "\n".join(lines)


def compose_audio_kept(
    edl: Edl,
    out_path: str | Path,
    *,
    source: str = "video",
    overwrite: bool = True,
) -> Path:
    """EDL の keep区間を連結した音声ファイルを出力する（試聴/確認用）。

    ``source``: ``"video"``=映像内蔵音声から / ``"speakers"``=話者別整音音声から。
    """
    out_path = Path(out_path)
    ranges = edl.kept_ranges()
    if not ranges:
        raise ValueError("keep区間が無い")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_files: list[str] = []

    if source == "speakers":
        mix_wav = out_path.with_suffix(".mix.wav")
        render_speaker_mix(edl, mix_wav)
        tmp_files.append(str(mix_wav))
        in_path = str(mix_wav)
    else:
        in_path = edl.source.video_path

    script = build_audio_filter_script(ranges, asrc="0:a")
    with tempfile.NamedTemporaryFile(
        "w", suffix=".ffscript", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(script)
        script_path = fh.name
    tmp_files.append(script_path)

    cmd = [
        ffmpeg_path(),
        "-y" if overwrite else "-n",
        "-i",
        in_path,
        "-filter_complex_script",
        script_path,
        "-map",
        "[outa]",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        str(out_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    finally:
        for f in tmp_files:
            Path(f).unlink(missing_ok=True)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").splitlines()[-15:])
        raise RuntimeError(f"音声合成失敗:\n{tail}")
    return out_path


def render_speaker_mix(edl: Edl, out_wav: str | Path) -> Path:
    """話者別トラックをミックスし、ラウドネス正規化した全長wavを書き出す（[F] 整音）。

    映像内蔵音声は使わず話者別 m4a のみを使う（plan [F]: video音声ミュート）。
    まず全長で整音し、カットは後段の trim/concat で行う（A/Vドリフト無し）。
    無音区間はカットで除かれるので整音対象から自然に外れる。
    """
    out_wav = Path(out_wav)
    tracks = [t.path for t in edl.source.audio_tracks if not t.is_desktop_audio]
    if not tracks:
        raise ValueError("話者トラックが無い")

    cmd = [ffmpeg_path(), "-y"]
    for p in tracks:
        cmd += ["-i", p]
    n = len(tracks)
    afilter = build_speaker_mix_filter(n, windowed=True)
    cmd += [
        "-filter_complex",
        afilter,
        "-map",
        "[outa]",
        "-ar",
        "48000",
        "-c:a",
        "pcm_s16le",
        str(out_wav),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").splitlines()[-15:])
        raise RuntimeError(f"話者ミックス失敗:\n{tail}")
    return out_wav


SPEECH_LUFS = -16.0  # 声(話者ミックス)の正規化ターゲット（LOUDNORM I=-16 と一致）
BGM_LOUDNESS_LUFS = -18.0  # 連結前に各曲を揃える unify 基準。最終BGM音量は別途 target で決める
# 本編BGMの既定の最終ラウドネス＝カフェBGM並みのさりげなさ（声 -16 より ~18 LU 下）。
CAFE_BGM_TARGET_LUFS = -34.0


def render_bgm_playlist(tracks: list[str | Path], out_wav: str | Path) -> Path:
    """複数のBGM曲を**曲ごとにラウドネス統一してから1本に連結**したwavを書き出す。

    パイプライン: **各曲を loudnorm で同一LUFS(``BGM_LOUDNESS_LUFS``)へ正規化 → 共通
    フォーマット(48k/stereo) → 指定順で concat**。曲間の音量差をここで消すので、本編側は
    連結済み1本へ**一括の音量調整(-20dB ダッキング)**を掛けるだけで均一に敷ける。
    並びは ``order_bgms`` のランダム順。本編側は出来た1本を ``-stream_loop`` で尺まで伸ばす。
    """
    out_wav = Path(out_wav)
    tracks = [str(t) for t in tracks]
    if not tracks:
        raise ValueError("BGM曲が無い")
    cmd = [ffmpeg_path(), "-y"]
    for t in tracks:
        cmd += ["-i", t]
    n = len(tracks)
    # 各曲: loudnorm で同一ラウドネスへ統一 → 共通フォーマット(48k/stereo/fltp) → concat
    parts = [
        f"[{i}:a]loudnorm=I={BGM_LOUDNESS_LUFS:g}:TP=-1.5:LRA=11,"
        f"aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]"
        for i in range(n)
    ]
    concat_in = "".join(f"[a{i}]" for i in range(n))
    afilter = ";".join(parts) + f";{concat_in}concat=n={n}:v=0:a=1[outa]"
    cmd += [
        "-filter_complex",
        afilter,
        "-map",
        "[outa]",
        "-ar",
        "48000",
        "-c:a",
        "pcm_s16le",
        str(out_wav),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").splitlines()[-15:])
        raise RuntimeError(f"BGMプレイリスト連結失敗:\n{tail}")
    return out_wav


def compose_kept(
    edl: Edl,
    out_path: str | Path,
    *,
    crf: int = 20,
    preset: str = "medium",
    max_ranges: int | None = None,
    audio: str = "speakers",
    framed: bool = False,
    subtitles: bool = False,
    bgm: str | list[str] | None = None,
    bgm_gain_db: float = -20.0,
    bgm_target_lufs: float | None = None,
    loading_label: str = "画面を準備",
    out_w: int = 1920,
    out_h: int = 1080,
    overwrite: bool = True,
    ranges: list | None = None,
    chapter_ribbon: bool = False,
    ribbon_date: str = "",
    overlays: bool = False,
) -> Path:
    """EDL の keep区間を連結した mp4 を出力する。

    ``audio``: ``"speakers"``=話者別を整音した音声を使う（plan [F]・既定）/
    ``"embedded"``=映像内蔵音声をそのまま使う（動作確認用）。
    ``framed``: True で各区間に EDL.framing の bbox を crop+scale 適用（[E]フレーミング反映）。
      さらに loading 区間には、のべつべ!ローディング画面を**最上位レイヤーとして overlay**
      合成する（元映像・音声は下に残す＝非破壊。音声は下の発話を継続）。
    ``overlays``: True で ``EDL.overlays``（編集ツールでユーザーが置いた画像/テキスト/モザイク）
      を焼き込む。レイヤー順は下から
      **映像/ローディング → 画像 → モザイク → 字幕 → チャプターリボン → テキスト**。
      モザイクが掛かるのは**映像とユーザー画像だけ**で、字幕・リボン・テキスト重ねといった
      文字情報/UI はモザイクより上に置く（最上位に置くと収録日や章名までぼける）。
      テキストは字幕と同一の二重縁取り。
    ``max_ranges``: 先頭N区間だけ合成（動作確認用）。
    ``ranges``: 連結対象区間の明示指定（**投稿単位[K]はその単位の区間を渡す**）。
      指定時は kept_ranges() の代わりに使い、字幕/フレーミング/BGMもこの区間から導出される。
    返り値は出力パス。
    """
    out_path = Path(out_path).resolve()  # 派生tmp/ass参照のため絶対化（cwd変更に耐える）
    ranges = list(ranges) if ranges is not None else edl.kept_ranges()
    if max_ranges is not None:
        ranges = ranges[:max_ranges]
    if not ranges:
        raise ValueError("keep区間が無い（先に cut auto-vad 等で segments を作る）")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_files: list[str] = []
    run_cwd: str | None = None

    cmd = [ffmpeg_path(), "-y" if overwrite else "-n", "-i", edl.source.video_path]
    if audio == "speakers":
        mix_wav = out_path.with_suffix(".mix.wav")
        render_speaker_mix(edl, mix_wav)
        tmp_files.append(str(mix_wav))
        cmd += ["-i", str(mix_wav)]
        asrc = "1:a"
    else:
        asrc = "0:a"
    if framed:
        intervals = loading_overlay_intervals(edl, ranges, default_label=loading_label)
        if intervals:
            from wwedit.framing.loading_screen import loading_loop_clip

            # ラベルごとに1周ループ動画を1本だけ生成(キャッシュ)。各区間は -stream_loop で伸ばす
            loop_by_label: dict[str, str] = {}
            vlabels: list[str] = []
            for iv in intervals:
                label = iv["label"]
                if label not in loop_by_label:
                    loop_by_label[label] = str(
                        loading_loop_clip(label, width=out_w, height=out_h)
                    )
                idx = sum(1 for a in cmd if a == "-i")
                cmd += ["-stream_loop", "-1", "-i", loop_by_label[label]]
                vlabels.append(f"{idx}:v")
            script = build_framed_overlay_script(
                edl, ranges, intervals, vlabels,
                vsrc="0:v", asrc=asrc, out_w=out_w, out_h=out_h,
            )
        else:
            script = build_filter_script_framed(
                edl, ranges, vsrc="0:v", asrc=asrc, out_w=out_w, out_h=out_h
            )
    else:
        script = build_filter_script(ranges, vsrc="0:v", asrc=asrc)

    vmap = "[outv]"

    # ── レイヤー順（下→上）────────────────────────────────────────────────
    #   映像/ローディング → ユーザー画像 → **モザイク** → 字幕 → リボン → テキスト重ね
    # モザイクは「映像とユーザー画像」だけに掛ける。字幕・チャプターリボン・テキスト重ね＝
    # **文字情報/UIはモザイクより上**に置く（最上位に置くと収録日やチャプター名までぼける）。
    # 重ねの座標は**ソースフレーム基準**なので、フレーミング crop 区間ごとに
    # 出力ピクセルへ写像する（同じ重ねでも crop が変われば位置・倍率が変わる）。
    ovs: list = []
    if overlays and edl.overlays:
        from wwedit.compose.overlay import (
            edl_overlays_for_output,
            output_crop_segments,
            place_overlays,
        )

        ovs = place_overlays(
            edl_overlays_for_output(edl, ranges), output_crop_segments(edl, ranges),
            src_w=edl.source.width or out_w, src_h=edl.source.height or out_h,
            out_w=out_w, out_h=out_h,
        )

    # ユーザー画像: 静止画を -loop で無限フレーム化し、拡大率・不透明度を掛けて位置へ重ねる
    if ovs:
        from wwedit.compose.overlay import image_overlays

        imgs = image_overlays(ovs)
        if imgs:
            prev = vmap[1:-1]
            ov_chains: list[str] = []
            for k, p in enumerate(imgs):
                o = p.o
                idx = sum(1 for a in cmd if a == "-i")
                cmd += ["-loop", "1", "-i", str(o.path)]
                sc = max(0.01, float(o.scale or 1.0)) * p.mag   # crop 拡大率を反映
                op = min(1.0, max(0.0, float(o.opacity if o.opacity is not None else 1.0)))
                px, py = int(round(p.x)), int(round(p.y))
                ov_chains.append(
                    f"[{idx}:v]scale=iw*{sc:g}:ih*{sc:g},format=rgba,"
                    f"colorchannelmixer=aa={op:g}[ovi{k}]"
                )
                nxt = f"ovo{k}"
                ov_chains.append(
                    f"[{prev}][ovi{k}]overlay={px}:{py}:eof_action=pass:"
                    f"enable='between(t,{p.start:.3f},{p.end:.3f})'[{nxt}]"
                )
                prev = nxt
            script = f"{script};\n" + ";\n".join(ov_chains)
            vmap = f"[{prev}]"

    # モザイク: 映像＋ユーザー画像に適用（字幕/リボン/テキストより**下**）。
    # 楕円形状は PIL で白楕円のグレースケールPNGを作り、マスク入力として渡す。
    if ovs:
        from wwedit.compose.overlay import (
            build_mosaic_chains,
            mosaic_overlays,
            mosaic_region_px,
        )

        mosaics = mosaic_overlays(ovs)
        if mosaics:
            from PIL import Image, ImageDraw

            mo_dir = Path(tempfile.mkdtemp())
            # マスクは**配置ごと**（crop 区間ごとに領域サイズが変わる）に作るので添字で引く
            mask_input_of: dict[int, int] = {}
            for k, p in enumerate(mosaics):
                if p.o.shape == "ellipse":
                    _, _, rw, rh = mosaic_region_px(p, out_w, out_h)
                    m = Image.new("L", (rw, rh), 0)
                    ImageDraw.Draw(m).ellipse([0, 0, rw - 1, rh - 1], fill=255)
                    mp = mo_dir / f"mask_{k:03d}_{p.o.id}.png"
                    m.save(mp)
                    tmp_files.append(str(mp))
                    idx = sum(1 for a in cmd if a == "-i")
                    cmd += ["-loop", "1", "-i", str(mp)]
                    mask_input_of[k] = idx
            prev = vmap[1:-1]
            mo_chains, last = build_mosaic_chains(
                mosaics, prev, out_w, out_h, mask_input_of=mask_input_of)
            if mo_chains:
                script = f"{script};\n" + ";\n".join(mo_chains)
                vmap = f"[{last}]"

    # 字幕（モザイクより上）: 出力時刻へ変換した EDL.subtitles を ASS で焼き込む
    if subtitles and edl.subtitles:
        from wwedit.subtitle.ass import MAIN_PALETTE, assign_speaker_colors, build_ass

        subs_out = subtitles_to_output(edl.subtitles, ranges)
        if subs_out:
            # 本編字幕色は話者ごと（喋っている人の色）。自動=寒色/暖色割当、EDL指定で上書き可
            speakers = [s.speaker for s in subs_out if s.speaker]
            color_map = assign_speaker_colors(speakers, edl.recording_dir or "main")
            for sp, key in (edl.subtitle_speaker_colors or {}).items():
                if key in MAIN_PALETTE:
                    color_map[sp] = MAIN_PALETTE[key]
            ass_dir = Path(tempfile.mkdtemp())
            ass_file = ass_dir / "subs.ass"
            ass_file.write_text(
                build_ass(subs_out, color_map=color_map, play_w=out_w, play_h=out_h),
                encoding="utf-8",
            )
            tmp_files.append(str(ass_file))
            # Windowsパスの ':'/'\\' を filtergraph でエスケープせず済むよう、cwd を ass_dir に
            # 置いて相対ファイル名で参照する（他パスは絶対化済み）。
            run_cwd = str(ass_dir)
            script = f"{script};\n[{vmap[1:-1]}]ass={ass_file.name}[outvs]"
            vmap = "[outvs]"

    # チャプターリボン（最上位・左上に張り付く2段リボン）: 章ごとに話者色で色分け。
    # 各章のPNG(フルフレーム透過)を出力タイムラインの区間に enable で被せる（非破壊）。
    if chapter_ribbon and edl.chapters:
        from wwedit.compose.chapter_ribbon import (
            chapter_ribbon_intervals,
            render_ribbon_png,
            resolve_speaker_schemes,
        )

        ivs, _tot = chapter_ribbon_intervals(edl, ranges)
        if ivs:
            schemes = resolve_speaker_schemes(edl)
            rib_dir = Path(tempfile.mkdtemp())
            prev = vmap[1:-1]  # 角括弧を外す
            rib_chains: list[str] = []
            for k, iv in enumerate(ivs):
                png = rib_dir / f"rib_{k:02d}.png"
                render_ribbon_png(
                    ribbon_date, iv["title"], png,
                    scheme=schemes.get(iv["speaker"]), out_w=out_w, out_h=out_h,
                )
                tmp_files.append(str(png))
                idx = sum(1 for a in cmd if a == "-i")
                cmd += ["-loop", "1", "-i", str(png)]  # 静止PNGを無限フレーム化して区間で被せる
                nxt = "outvr" if k == len(ivs) - 1 else f"rb{k}"
                rib_chains.append(
                    f"[{prev}][{idx}:v]overlay=0:0:eof_action=pass:"
                    f"enable='between(t,{iv['out_start']:.3f},{iv['out_end']:.3f})'[{nxt}]"
                )
                prev = nxt
            script = f"{script};\n" + ";\n".join(rib_chains)
            vmap = f"[{prev}]"

    # テキスト重ね（**最上位**＝リボン・字幕より上／モザイクは掛からない）。
    # 位置指定(\an7+\pos)の ASS を字幕とは別ファイルで最後に重ねる。
    if ovs:
        from wwedit.compose.overlay import build_overlay_ass

        ov_ass = build_overlay_ass(ovs, play_w=out_w, play_h=out_h)
        if "Dialogue:" in ov_ass:
            ov_dir = Path(run_cwd) if run_cwd else Path(tempfile.mkdtemp())
            ov_file = ov_dir / "overlays.ass"
            ov_file.write_text(ov_ass, encoding="utf-8")
            tmp_files.append(str(ov_file))
            run_cwd = str(ov_dir)  # filtergraph は相対名で参照（Windowsパスのエスケープ回避）
            prev = vmap[1:-1]
            script = f"{script};\n[{prev}]ass={ov_file.name}[outvo]"
            vmap = "[outvo]"

    # BGM（本編下に -20dB 目安でダッキング・最後に薄く敷く）。元音声 [outa] と amix。
    # bgm が複数曲なら同ジャンル連続再生のため1本のプレイリストwavへ連結してから敷く。
    # 本編BGMの最終音量は bgm_target_lufs（カフェBGM並みのさりげなさ）で指定するのが基本。
    # 指定時は各曲を unify(-18 LUFS)へ揃えた連結wavへ「目標-unify」dBを掛けて目標LUFSへ落とす。
    amap = "[outa]"
    tracks: list[str] = [bgm] if isinstance(bgm, str) else list(bgm or [])
    bgm_file: str | None = None
    gain_db = bgm_gain_db
    if tracks:
        if bgm_target_lufs is not None:
            # 目標LUFS指定: 単曲でも loudnorm で unify 基準へ揃えてから目標まで減衰（数値が正確）
            playlist_wav = out_path.with_suffix(".bgm.wav")
            render_bgm_playlist(tracks, playlist_wav)
            tmp_files.append(str(playlist_wav))
            bgm_file = str(playlist_wav)
            gain_db = bgm_target_lufs - BGM_LOUDNESS_LUFS
        elif len(tracks) == 1:
            bgm_file = tracks[0]  # 単一ファイル・目標未指定は素材そのままを bgm_gain_db で敷く
        else:
            playlist_wav = out_path.with_suffix(".bgm.wav")
            render_bgm_playlist(tracks, playlist_wav)
            tmp_files.append(str(playlist_wav))
            bgm_file = str(playlist_wav)
    if bgm_file:
        bgm_idx = sum(1 for a in cmd if a == "-i")
        cmd += ["-stream_loop", "-1", "-i", bgm_file]  # 出力尺まで自動ループ（全曲1巡後に頭から）
        script = (
            f"{script};\n[{bgm_idx}:a]volume={gain_db:g}dB[bg];"
            f"[outa][bg]amix=inputs=2:duration=first:normalize=0[outaB]"
        )
        amap = "[outaB]"

    with tempfile.NamedTemporaryFile(
        "w", suffix=".ffscript", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(script)
        script_path = fh.name
    tmp_files.append(script_path)

    cmd += [
        "-filter_complex_script",
        script_path,
        "-map",
        vmap,
        "-map",
        amap,
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        str(out_path),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", cwd=run_cwd
        )
    finally:
        for f in tmp_files:
            Path(f).unlink(missing_ok=True)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").splitlines()[-15:])
        raise RuntimeError(f"ffmpeg 合成失敗:\n{tail}")
    return out_path
