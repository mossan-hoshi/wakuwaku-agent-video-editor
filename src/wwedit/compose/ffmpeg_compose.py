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

from wwedit.common.media import ffmpeg_error, ffmpeg_path
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
    "src_to_out",
    "stretch_time",
    "out_total",
]


def src_to_out(ranges: list[TimeRange], t: float, freezes=()) -> float:
    """公開版 ``_src_to_out``（ちびキャラのタイムライン構築などレンダ系モジュールが使う）。"""
    return _src_to_out(ranges, t, freezes)

# YouTube向けラウドネス目標（EBU R128）。割れない範囲でほどほどに大きく（plan [F]）。
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"
# [F] 30秒粒度の窓ノーマライズ（dynaudnorm）。全長一括だと一部が小さすぎるのを防ぐ。
# g=61 フレーム×f=500ms ≒ 30.5s 窓、m=7 で無音/ノイズ床の過増幅を抑制、p=0.9 ピーク目標。
DYNNORM = "dynaudnorm=f=500:g=61:m=7:p=0.9"


def build_speaker_mix_filter(
    n: int, *, windowed: bool = True, raw_idx: tuple[int, ...] = ()
) -> str:
    """話者 n トラックの整音 filter_complex を作る。

    ``windowed``: True で各トラックに **30秒粒度の窓ノーマライズ(dynaudnorm)** を先に掛けて
    区間ごとの音量ムラを均し、ミックス後に全体ラウドネスを LOUDNORM で YouTube 目標へ。
    False は従来の全長一括 LOUDNORM のみ（動作確認用）。無音はカットで除外済み・dynaudnorm の
    m 制限で無音の過増幅も抑える。

    ``raw_idx``: **dynaudnorm を掛けない入力**（PC オーディオ＝共有された音楽など）。
    音楽に窓ノーマライズを掛けると強弱が潰れ、曲間の無音まで持ち上がる。素のまま混ぜ、
    全体ラウドネスだけ最後に揃える。
    """
    if n < 1:
        raise ValueError("トラックが無い")
    raw = set(raw_idx)
    pre = (lambda i: f"[{i}:a]{DYNNORM}[d{i}]") if windowed else None
    src = (
        (lambda i: f"[{i}:a]" if i in raw else f"[d{i}]")
        if windowed
        else (lambda i: f"[{i}:a]")
    )
    lines: list[str] = []
    if windowed:
        lines += [pre(i) for i in range(n) if i not in raw]
    if n == 1:
        lines.append(f"{src(0)}{LOUDNORM}[outa]")
    else:
        mix = "".join(src(i) for i in range(n))
        lines.append(f"{mix}amix=inputs={n}:normalize=0[mix]")
        lines.append(f"[mix]{LOUDNORM}[outa]")
    return ";".join(lines)


def build_filter_script(
    ranges: list[TimeRange], *, vsrc: str = "0:v", asrc: str = "0:a", freezes=()
) -> str:
    """keep区間列から filter_complex スクリプト本文を作る（v+a を trim→concat）。

    ``vsrc``/``asrc``: 映像/音声の入力ラベル。音声を別ファイル（整音済み）にする場合は
    ``asrc="1:a"`` を渡す。
    ``freezes``: フリーズ位置で区間を分割し、映像は ``tpad`` で最終フレームを複製、
    音声は stretched 座標（``asrc`` が σ タイムラインの全長 wav である前提）で atrim する。
    空なら従来と完全同一の出力。
    """
    pieces = _split_ranges_at_freezes(ranges, freezes)
    lines: list[str] = []
    labels: list[str] = []
    cum = 0.0
    for i, (r, extra) in enumerate(pieces):
        vpad = f",tpad=stop_mode=clone:stop_duration={extra:.3f}" if extra > 0 else ""
        # set/asetpts でPTSを各区間ローカルに振り直し、concatの連結を正しくする
        lines.append(
            f"[{vsrc}]trim=start={r.start:.3f}:end={r.end:.3f},setpts=PTS-STARTPTS{vpad}[v{i}];"
        )
        a_s, a_e = r.start + cum, r.end + cum + extra
        cum += extra
        lines.append(
            f"[{asrc}]atrim=start={a_s:.3f}:end={a_e:.3f},asetpts=PTS-STARTPTS[a{i}];"
        )
        labels.append(f"[v{i}][a{i}]")
    n = len(pieces)
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
    freezes=(),
) -> str:
    """keep区間を**フレーミング境界でも割り**、各小片の中点の bbox で crop+scale して concat する。

    各区間の映像は trim→crop(メイン領域)→scale(出力解像度) の順。フレーミング未割当
    （bbox None）の区間は scale のみ＝全画面。映像/音声を同一グラフで concat しドリフト無し。
    ``vout``/``aout`` で最終ラベルを変えられる（上位レイヤーのoverlay土台にする時に使う）。
    ``freezes``: build_filter_script と同じフリーズ対応（映像 tpad / 音声 σ 座標）。
    """
    pieces = framed_pieces(edl, ranges, freezes)
    lines: list[str] = []
    labels: list[str] = []
    cum = 0.0
    for i, (r, extra) in enumerate(pieces):
        vf = framing_crop_filter(bbox_at(edl, (r.start + r.end) / 2), out_w, out_h)
        vpad = f",tpad=stop_mode=clone:stop_duration={extra:.3f}" if extra > 0 else ""
        lines.append(
            f"[{vsrc}]trim=start={r.start:.3f}:end={r.end:.3f},"
            f"setpts=PTS-STARTPTS,{vf}{vpad}[v{i}];"
        )
        a_s, a_e = r.start + cum, r.end + cum + extra
        cum += extra
        lines.append(
            f"[{asrc}]atrim=start={a_s:.3f}:end={a_e:.3f},asetpts=PTS-STARTPTS[a{i}];"
        )
        labels.append(f"[v{i}][a{i}]")
    n = len(pieces)
    lines.append(f"{''.join(labels)}concat=n={n}:v=1:a=1[{vout}][{aout}]")
    return "\n".join(lines)


def _freezes_in_ranges(freezes, ranges: list[TimeRange]):
    """keep区間の**内側**（境界を除く）にあるフリーズだけを at 昇順で返す。"""
    return sorted(
        (f for f in (freezes or ())
         if any(r.start < f.at < r.end for r in ranges)),
        key=lambda f: f.at,
    )


def stretch_time(t: float, freezes) -> float:
    """ソース秒 t を、フリーズ挿入後の stretched ソース秒 σ(t)=t+Σextra へ写像する。

    方式B（TTS読み上げ）の voice-tts-finalize が全長トラックを組むときの座標系。
    ``at`` ちょうどの時刻はシフトしない（フリーズは at の**直後**に挿入される）。
    """
    return t + sum(f.extra for f in (freezes or ()) if f.at < t)


def out_total(ranges: list[TimeRange], freezes=()) -> float:
    """出力タイムラインの総尺（keep区間の合計＋区間内フリーズの延長分）。"""
    return sum(r.duration for r in ranges) + sum(
        f.extra for f in _freezes_in_ranges(freezes, ranges))


def _src_to_out(ranges: list[TimeRange], t: float, freezes=()) -> float:
    """ソース時刻 t を、keep区間を連結した出力タイムライン秒へ変換する（``ranges`` 基準）。

    ``freezes``: EDL.freezes（方式Bのフリーズフレーム）。t より前（at < t）の
    keep区間内フリーズの延長分だけ後ろへずれる。空なら従来と同一。
    """
    acc = 0.0
    for r in ranges:
        if t < r.start:
            break
        if t <= r.end:
            acc += t - r.start
            break
        acc += r.end - r.start
    if freezes:
        acc += sum(f.extra for f in _freezes_in_ranges(freezes, ranges) if f.at < t)
    return acc


def out_to_src(ranges: list[TimeRange], t: float, freezes=()) -> float:
    """出力タイムライン秒 t を**ソース秒へ逆写像**する（``_src_to_out`` の逆）。

    方式B（TTS読み上げ）で「読み上げクリップの終わり」をソース時刻の字幕へ戻すのに使う。
    フリーズの延長中（映像が止まっている間）はソース時刻が進まないので、その区間の t は
    すべてフリーズ位置 ``at`` に落ちる。範囲外は端にクランプする。
    """
    if not ranges:
        return 0.0
    acc = 0.0
    for r, extra in _split_ranges_at_freezes(ranges, freezes):
        dur = r.duration
        if t <= acc + dur:
            return r.start + max(0.0, t - acc)
        acc += dur
        if extra:
            if t <= acc + extra:
                return r.end          # フリーズ中はソース時刻が進まない
            acc += extra
    return ranges[-1].end


def _split_ranges_at_freezes(
    ranges: list[TimeRange], freezes=()
) -> list[tuple[TimeRange, float]]:
    """各keep区間をフリーズ位置で分割し ``(小区間, 直後に挿入するフリーズ秒)`` を返す。

    フリーズ無しなら ``[(r, 0.0), ...]``（従来と同じ区間列）。映像はフリーズ付き小区間の
    末尾に ``tpad=stop_mode=clone`` を足し、音声は stretched 座標で atrim する。
    """
    out: list[tuple[TimeRange, float]] = []
    for r in ranges:
        cur = r.start
        for f in _freezes_in_ranges(freezes, [r]):
            out.append((TimeRange(start=cur, end=f.at), f.extra))
            cur = f.at
        out.append((TimeRange(start=cur, end=r.end), 0.0))
    return out


# フレーミング境界で割ってできる小片の最小尺(秒)。これ未満になる境界は無視する
# （1フレーム未満の片は concat で丸ごと消え、端でフレームが落ちる）。
FRAMING_MIN_PIECE_S = 0.2


def framing_bounds(edl: Edl) -> list[float]:
    """フレーミング区間の境界時刻（昇順・重複除去）。"""
    bs = {round(float(f.start), 3) for f in (edl.framing or ())}
    bs |= {round(float(f.end), 3) for f in (edl.framing or ())}
    return sorted(bs)


def split_range_at_bounds(
    r: TimeRange, bounds: list[float], *, min_piece: float = FRAMING_MIN_PIECE_S
) -> list[TimeRange]:
    """keep区間 ``r`` を ``bounds`` の境界で割る（``min_piece`` 未満の小片は作らない）。"""
    cuts: list[float] = []
    for b in bounds:
        if b <= r.start + min_piece:
            continue
        if b >= r.end - min_piece:
            break
        if cuts and b - cuts[-1] < min_piece:
            continue
        cuts.append(b)
    if not cuts:
        return [r]
    out: list[TimeRange] = []
    prev = r.start
    for b in cuts:
        out.append(TimeRange(start=prev, end=b))
        prev = b
    out.append(TimeRange(start=prev, end=r.end))
    return out


def framed_pieces(
    edl: Edl, ranges: list[TimeRange], freezes=()
) -> list[tuple[TimeRange, float]]:
    """keep区間を**フリーズ位置とフレーミング境界の両方で**割った ``(小区間, フリーズ秒)``。

    bbox は小片の中点で引くので、**フレーミングで割らないと 1 keep区間につき bbox が1つ**
    しか当たらない。ワープ後(方式B)の EDL は keep区間が**全長で1個**なので、割らないと
    crop も 画面内NGワードのモザイクも丸ごと効かなくなる（2026-08-06 実測・STATUS §17.9）。
    フリーズ秒は分割後の**最後の小片**に付ける（フリーズは区間の直後に入るため）。
    """
    bounds = framing_bounds(edl)
    out: list[tuple[TimeRange, float]] = []
    for r, extra in _split_ranges_at_freezes(ranges, freezes):
        subs = split_range_at_bounds(r, bounds)
        for j, sub in enumerate(subs):
            out.append((sub, extra if j == len(subs) - 1 else 0.0))
    return out


def subtitles_to_output(
    subtitles: list[Subtitle], ranges: list[TimeRange], freezes=()
) -> list[Subtitle]:
    """字幕(ソース時刻)を出力タイムライン時刻へ変換する（カット連結後の焼き込み用）。

    完全にカット区間内（start と end が同じ出力時刻へ潰れる）字幕は除外。EDL本体はソース時刻
    のまま保持し、レンダリング時にのみ変換する（非破壊）。フリーズ（at=発話end直前）を
    区間内に含む字幕は end 側だけ延長される＝フリーズ中も字幕が持続する。
    """
    out: list[Subtitle] = []
    for s in subtitles:
        os_, oe = _src_to_out(ranges, s.start, freezes), _src_to_out(ranges, s.end, freezes)
        if oe - os_ <= 1e-3:
            continue
        out.append(
            Subtitle(start=os_, end=oe, text=s.text, style=s.style, speaker=s.speaker)
        )
    return out


def loading_overlay_intervals(
    edl: Edl, ranges: list[TimeRange], *, default_label: str = "画面を準備", freezes=()
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
                    "out_start": _src_to_out(ranges, a, freezes),
                    "out_end": _src_to_out(ranges, b, freezes),
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
    freezes=(),
) -> str:
    """framed concat を土台に、loading クリップを出力タイムラインへ overlay 合成する。

    ``intervals``: loading_overlay_intervals の結果（出力秒）。``loading_vlabels``: 各 interval に
    対応する ffmpeg 入力映像ラベル（例 ``"2:v"``）。各クリップは setpts で out_start へ遅延させ、
    ``enable='between(t,...)'`` でその区間だけ最上位に被せる（元映像は下に残る＝非破壊）。
    """
    base = build_filter_script_framed(
        edl, ranges, vsrc=vsrc, asrc=asrc, out_w=out_w, out_h=out_h,
        vout="base0", aout="outa", freezes=freezes,
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


def build_audio_filter_script(
    ranges: list[TimeRange], *, asrc: str = "0:a", freezes=()
) -> str:
    """音声のみ keep区間を atrim→concat する filter_complex スクリプト本文。

    ``freezes``: 入力が σ タイムラインの全長 wav（voice-tts-finalize 済み）である前提で
    stretched 座標の atrim にする。空なら従来と同一。
    """
    pieces = _split_ranges_at_freezes(ranges, freezes)
    lines: list[str] = []
    labels: list[str] = []
    cum = 0.0
    for i, (r, extra) in enumerate(pieces):
        a_s, a_e = r.start + cum, r.end + cum + extra
        cum += extra
        lines.append(
            f"[{asrc}]atrim=start={a_s:.3f}:end={a_e:.3f},asetpts=PTS-STARTPTS[a{i}];"
        )
        labels.append(f"[a{i}]")
    lines.append(f"{''.join(labels)}concat=n={len(pieces)}:v=0:a=1[outa]")
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

    frz = tuple(edl.freezes or ())
    if source == "speakers":
        mix_wav = out_path.with_suffix(".mix.wav")
        render_speaker_mix(edl, mix_wav)
        tmp_files.append(str(mix_wav))
        in_path = str(mix_wav)
    else:
        in_path = edl.source.video_path
        if frz:
            raise ValueError("freezes がある EDL は source='video' 不可（内蔵音声は伸びない）")

    script = build_audio_filter_script(ranges, asrc="0:a", freezes=frz)
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
        tail = ffmpeg_error(proc.stderr)
        raise RuntimeError(f"音声合成失敗:\n{tail}")
    return out_path


def render_speaker_mix(edl: Edl, out_wav: str | Path) -> Path:
    """話者別トラックをミックスし、ラウドネス正規化した全長wavを書き出す（[F] 整音）。

    映像内蔵音声は使わず話者別 m4a のみを使う（plan [F]: video音声ミュート）。
    まず全長で整音し、カットは後段の trim/concat で行う（A/Vドリフト無し）。
    無音区間はカットで除かれるので整音対象から自然に外れる。
    """
    out_wav = Path(out_wav)
    # **PC オーディオ（共有された音楽など）も混ぜる。**文字起こしはしないが、本編で
    # 実際に鳴っていた音なので落とすと内容が欠ける（音楽生成AIの試聴回など）。
    # ただし窓ノーマライズは掛けない（``raw_idx``）。
    if not [t for t in edl.source.audio_tracks if not t.is_desktop_audio]:
        raise ValueError("話者トラックが無い")
    # [V] キャラ声差し替え済みなら voice_path を使う（None=元の path＝非破壊）
    tracks = [t.voice_path or t.path for t in edl.source.audio_tracks]
    raw_idx = tuple(i for i, t in enumerate(edl.source.audio_tracks) if t.is_desktop_audio)

    cmd = [ffmpeg_path(), "-y"]
    for p in tracks:
        cmd += ["-i", p]
    n = len(tracks)
    afilter = build_speaker_mix_filter(n, windowed=True, raw_idx=raw_idx)
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
        tail = ffmpeg_error(proc.stderr)
        raise RuntimeError(f"話者ミックス失敗:\n{tail}")
    return out_wav


SPEECH_LUFS = -16.0  # 声(話者ミックス)の正規化ターゲット（LOUDNORM I=-16 と一致）
BGM_LOUDNESS_LUFS = -18.0  # 連結前に各曲を揃える unify 基準。最終BGM音量は別途 target で決める
# 本編BGMの既定の最終ラウドネス＝カフェBGM並みのさりげなさ（声 -16 より ~18 LU 下）。
CAFE_BGM_TARGET_LUFS = -34.0


#: BGM を止める区間の前後に足す余白（秒）。切り替わりを発話やデモ音にぶつけない。
BGM_MUTE_PAD_S = 0.3

#: 止める前後のフェード（秒）。**いきなり消えると不快**（ユーザー指示・2026-08-06）。
BGM_MUTE_FADE_S = 0.6


def bgm_mute_spans_merged(
    spans, *, pad: float = BGM_MUTE_PAD_S, total: float | None = None,
) -> list[tuple[float, float]]:
    """BGM を止める区間に余白を足し、重なったものを1つにまとめる。"""
    merged: list[list[float]] = []
    for a, b in sorted((float(a), float(b)) for a, b in (spans or ())):
        lo, hi = max(0.0, a - pad), b + pad
        if total is not None:
            hi = min(hi, float(total))
        if hi - lo <= 1e-3:
            continue
        if merged and lo <= merged[-1][1] + 1e-6:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return [(a, b) for a, b in merged]


def bgm_mute_expr(
    spans, *, pad: float = BGM_MUTE_PAD_S, fade: float = BGM_MUTE_FADE_S,
    total: float | None = None,
) -> str:
    """BGM の音量エンベロープ式（**フェード付き**）を返す。空なら ``""``。

    PCシステム音が鳴っている間だけ BGM を 0 にする。**その回の音そのものを聴かせる**
    ときに使う（音楽生成の聴き比べ等・`--bgm-avoid-desktop`）。

    ⚠️ **いきなり切らない**。区間の ``fade`` 秒手前から線形に下げ、終わってから
    ``fade`` 秒かけて戻す（ユーザー指示・2026-08-06「いきなり消えると不快」）。

    各区間の「区間からの距離 ÷ fade」を 0〜1 に丸め、全区間の**最小**を音量にする
    （区間内=0・十分離れていれば1）。``volume='…':eval=frame`` に渡す。
    """
    merged = bgm_mute_spans_merged(spans, pad=pad, total=total)
    if not merged:
        return ""
    f = max(1e-3, float(fade))
    terms = [f"clip(max({a:.3f}-t\\,t-{b:.3f})/{f:g}\\,0\\,1)" for a, b in merged]
    expr = terms[0]
    for term in terms[1:]:
        expr = f"min({expr}\\,{term})"
    return expr


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
        tail = ffmpeg_error(proc.stderr)
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
    bgm_mute_spans: list[tuple[float, float]] | None = None,
    loading_label: str = "画面を準備",
    out_w: int = 1920,
    out_h: int = 1080,
    overwrite: bool = True,
    ranges: list | None = None,
    chapter_ribbon: bool = False,
    ribbon_date: str = "",
    overlays: bool = False,
    chibi: bool = False,
    chibi_height: int = 0,
    chibi_margin: tuple[int, int] | None = None,
    chibi_mouth_step: float | None = None,
    infographic: bool = False,
    data_dir: str | Path | None = None,
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
    ``infographic``: True で ``EDL.infographic``（本編冒頭の要約図解）を
      **上部UI/ちびキャラ/字幕に被らない安全枠**へ重ねる（モザイクより上・ちびより下）。
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

    # [V] 方式Bのフリーズフレーム。映像は tpad で静止し、音声は σ タイムラインの
    # 全長 wav（voice-tts-finalize 済み voice_path）を stretched 座標で切る。
    frz = tuple(edl.freezes or ())
    if frz and audio != "speakers":
        raise ValueError("freezes がある EDL は audio='speakers' 必須（内蔵音声は伸びない）")

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
        intervals = loading_overlay_intervals(
            edl, ranges, default_label=loading_label, freezes=frz)
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
                vsrc="0:v", asrc=asrc, out_w=out_w, out_h=out_h, freezes=frz,
            )
        else:
            script = build_filter_script_framed(
                edl, ranges, vsrc="0:v", asrc=asrc, out_w=out_w, out_h=out_h,
                freezes=frz,
            )
    else:
        script = build_filter_script(ranges, vsrc="0:v", asrc=asrc, freezes=frz)

    vmap = "[outv]"

    # ── レイヤー順（下→上）────────────────────────────────────────────────
    #   映像/ローディング → ユーザー画像 → **モザイク** → 図解 → ちび → 字幕 → リボン → テキスト
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

    # 要約インフォグラフィック（[I]・本編冒頭N秒）。モザイクより上・ちびキャラより下。
    # 安全枠に contain 収めしてあるので本来ぶつからないが、万一はみ出しても
    # ちび/字幕/リボンが上から描かれる並びにしておく。
    if infographic and edl.infographic and edl.infographic.enabled:
        from wwedit.compose.infographic_overlay import (
            build_infographic_chain,
            infographic_placement,
        )

        placement = infographic_placement(edl, out_w=out_w, out_h=out_h)
        if placement:
            idx = sum(1 for a in cmd if a == "-i")
            cmd += ["-loop", "1", "-i", str(Path(edl.infographic.path).resolve())]
            prev = vmap[1:-1]
            script = f"{script};\n" + ";\n".join(
                build_infographic_chain(edl.infographic, placement, idx, prev, "igo"))
            vmap = "[igo]"

    # ちびキャラ（[V]・モザイクより上/字幕より下＝UIレイヤー、モザイクは掛けない）。
    # スプライトPNGの ffconcat プレイリストを -f concat の動画入力にして左右2体を重ねる。
    if chibi and edl.chibi and edl.chibi.enabled and edl.character_cast:
        from wwedit.compose.chibi_overlay import chibi_side_specs

        ch_dir = Path(tempfile.mkdtemp())
        specs = chibi_side_specs(
            edl, ranges, tmp_dir=ch_dir, margin=chibi_margin,
            mouth_step=chibi_mouth_step,
            data_dir=Path(data_dir) if data_dir else None,
        )
        if specs:
            ch_h = chibi_height or (edl.chibi.height_px if edl.chibi else 320)
            prev = vmap[1:-1]
            ch_chains: list[str] = []
            for k, sp in enumerate(specs):
                idx = sum(1 for a in cmd if a == "-i")
                cmd += ["-f", "concat", "-safe", "0", "-i", str(sp.ffconcat_path)]
                tmp_files.append(str(sp.ffconcat_path))
                nxt = f"cvb{k}"
                flip = ",hflip" if sp.flip else ""
                ch_chains.append(
                    f"[{idx}:v]fps=30,scale=-1:{ch_h}{flip},format=rgba[chb{k}]")
                ch_chains.append(
                    f"[{prev}][chb{k}]overlay={sp.x_expr}:{sp.y_expr}:"
                    f"eof_action=pass[{nxt}]"
                )
                prev = nxt
            script = f"{script};\n" + ";\n".join(ch_chains)
            vmap = f"[{prev}]"

    # 字幕（モザイクより上）: 出力時刻へ変換した EDL.subtitles を ASS で焼き込む
    if subtitles and edl.subtitles:
        from wwedit.subtitle.ass import assign_speaker_colors, build_ass, resolve_color_key

        subs_out = subtitles_to_output(edl.subtitles, ranges, freezes=frz)
        if subs_out:
            # 本編字幕色は話者ごと（喋っている人の色）。自動=寒色/暖色割当、EDL指定で上書き可
            # （パレットキー / キャラid / #RRGGBB を受ける）
            speakers = [s.speaker for s in subs_out if s.speaker]
            color_map = assign_speaker_colors(speakers, edl.recording_dir or "main")
            for sp, key in (edl.subtitle_speaker_colors or {}).items():
                c = resolve_color_key(key)
                if c:
                    color_map[sp] = c
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
        bgm_filter = f"volume={gain_db:g}dB"
        mute_expr = bgm_mute_expr(bgm_mute_spans or ())
        if mute_expr:
            # フェード付きのエンベロープ（毎フレーム評価）。いきなり切らない。
            bgm_filter += f",volume=volume='{mute_expr}':eval=frame"
        script = (
            f"{script};\n[{bgm_idx}:a]{bgm_filter}[bg];"
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
        tail = ffmpeg_error(proc.stderr)
        raise RuntimeError(f"ffmpeg 合成失敗:\n{tail}")
    return out_path
