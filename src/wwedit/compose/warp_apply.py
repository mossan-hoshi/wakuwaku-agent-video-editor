"""[S2] ``Warp`` を実素材へ適用する — ワープ済みフッテージと**新しい EDL** を作る。

## なぜ「素材を作り直す」のか

合成コア（`ffmpeg_compose`）に可変速を持ち込むと、字幕・章・リボン・ちび・図解・BGM・
アイキャッチが全部そこのタイムライン計算に依存しているので、まとめて壊れる。
そこで**合成の手前**で素材そのものをワープしてしまい、以後は
「カットもフリーズも無い普通の素材」として既存の合成をそのまま通す。

    収録mp4 ──Warp──> footage_warped.mp4（映像＋PC音声だけ・出力座標）
    EDL     ──Warp──> edl.warped.json（segments は全体1本・freezes なし）
                       字幕/章/framing/読み上げ/口パクは**新しい出力秒**に置き直す
    → compose video を普段どおり実行

## 音は伸縮しない

映像だけが可変速で、音は**切って詰める**。PC音声が鳴っている区間は Warp 側で倍率1.0に
固定してあるので、切られるのは無音だけ。``atempo`` を使わないので音程も変わらない。
読み上げ（TTS）は out 座標に置き直すだけで**一切加工しない**。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import wave
from pathlib import Path

from wwedit.common.media import ffmpeg_error, ffmpeg_path, ffprobe_path, probe
from wwedit.compose.ffmpeg_compose import _split_ranges_at_freezes
from wwedit.compose.timewarp import Warp
from wwedit.edl.schema import Edl, Segment, TimeRange

__all__ = [
    "Piece",
    "src_prime_pieces",
    "video_frame_count",
    "warp_pieces",
    "build_warp_video_script",
    "build_warp_audio_script",
    "render_warped_footage",
    "render_warped_audio",
    "render_voice_track",
    "warp_edl",
]

#: ``(素材開始秒, 素材終了秒, 出力秒数, フリーズか)``。素材秒は**収録ファイルの raw 秒**。
Piece = tuple[float, float, float, bool]

EPS = 1e-6


def src_prime_pieces(
    ranges: list[TimeRange], freezes=(),
) -> list[tuple[float, float, float, float, bool]]:
    """``(src'開始, src'終了, raw開始, raw終了, フリーズか)`` の列。

    src'（keep区間を連結した秒）と収録ファイルの raw 秒の対応表。フリーズは
    ``raw開始 == raw終了`` で表す。
    """
    out: list[tuple[float, float, float, float, bool]] = []
    acc = 0.0
    for r, extra in _split_ranges_at_freezes(ranges, freezes):
        out.append((acc, acc + r.duration, r.start, r.end, False))
        acc += r.duration
        if extra > EPS:
            out.append((acc, acc + extra, r.end, r.end, True))
            acc += extra
    return out


def warp_pieces(warp: Warp, ranges: list[TimeRange], freezes=(), *,
                fps: int = 25) -> list[Piece]:
    """``Warp`` の各区間を keep区間の境界で割り、収録ファイルの raw 秒へ落とす。

    Warp は src' 座標なので、そのままでは素材の穴（カットした所）をまたいでしまう。
    ここで割っておかないと ``trim`` が捨てたはずの区間を拾う。

    ⚠️ 割った小片の出力尺は**累積で丸める**。小片ごとに独立して丸めると端数が積み上がり、
    区間の合計が合わなくなる（393区間→604片で+35フレーム＝1.4秒ずれた）。
    """
    table = src_prime_pieces(ranges, freezes)
    out: list[Piece] = []
    for s in warp.segs:
        total_n = max(1, round(s.out_dur * fps))
        if s.src_dur <= EPS:                       # フリーズ区間（素材が尽きた）
            raw = _sp_to_raw(table, s.src_start)
            out.append((raw, raw, total_n / fps, True))
            continue
        cut: list[tuple[float, float, float, bool]] = []
        for sa, sb, ra, _rb, is_freeze in table:
            a, b = max(s.src_start, sa), min(s.src_end, sb)
            if b - a <= EPS:
                continue
            cut.append((ra + (a - sa), ra + (b - sa), (b - sa) - (a - sa), is_freeze))
        acc, done, made = 0.0, 0, 0
        for k, (r0, r1, _w, is_freeze) in enumerate(cut):
            acc += (r1 - r0) / s.src_dur
            n = (total_n - done) if k == len(cut) - 1 else round(acc * total_n) - done
            if n <= 0:
                # 1フレームに満たない小片は**捨てる**。1枚に切り上げると区間の合計が
                # 増えてしまう（604片で+23フレーム）。落ちるのは1/25秒未満の映像だけ。
                continue
            done += n
            made += 1
            out.append((r0, r0 if is_freeze else r1, n / fps, is_freeze))
        if not made:                                   # 全部が1フレーム未満だった
            r0, r1, _w, is_freeze = cut[0]
            out.append((r0, r0 if is_freeze else r1, total_n / fps, is_freeze))
    return out


def _sp_to_raw(table, t: float) -> float:
    for sa, sb, ra, _rb, _f in table:
        if t <= sb + EPS:
            return ra + max(0.0, t - sa)
    return table[-1][3] if table else 0.0


def build_warp_video_script(pieces: list[Piece], *, fps: int, vsrc: str = "0:v",
                            vout: str = "outv", src_frames: int | None = None) -> str:
    """映像だけをワープする filtergraph。

    * 通常区間 … ``trim`` → ``setpts`` で**尺を出力尺へ引き伸ばす/縮める** → ``fps`` で整流。
      ``setpts`` の係数は「出力尺 ÷ 素材尺」（倍率の逆数）。
    * フリーズ … 1フレームだけ取って ``tpad=stop_mode=clone`` で伸ばす。

    ⚠️ **``fps`` フィルタを使ってはいけない**。区間ごとに1枚前後ずれて積み上がる
    （実測: 60片で6〜7フレーム＝0.24〜0.28秒不足。604片なら約2.5秒ずれる）。
    そこで**フレーム番号で扱う**:

        trim=start_frame:end_frame → select で m 枚から n 枚を等間隔に間引く
        → setpts=N/fps/TB → tpad（足りなければ複製）→ trim=end_frame=n

    倍率は必ず 1.0 以上なので ``m >= n``＝間引きだけで済む（``select`` は複製できない）。

    ⚠️ **``select`` は前後1枚ぶれる**。式の上では ``m`` 枚から ``n`` 枚ちょうど残るはずだが、
    実測では区間によって1枚落ちた（select を多く使う区間ほど落ちた: 60片で最大-14枚、
    604片で-22枚＝0.88秒）。なので**全区間**に tpad→trim=end_frame の保険を掛けて
    枚数を ``n`` に確定させる。フリーズも同じ形で書けるので分岐は無い。

    ⚠️ ``src_frames``（素材の総フレーム数）を渡すと**素材の外を指す片をクランプ**する。
    渡さないと、素材の終端を越えた位置のフリーズ片が ``trim`` で**0枚**になり、
    ``tpad`` は複製元が無いので伸ばせず、その片がまるごと消える
    （実測: 末尾のフリーズ1片で-30フレーム＝-1.2秒。素材 1048.12秒＝26203枚に対し
    片が 1048.21秒＝26205枚目を要求していた）。
    """
    last = None if src_frames is None else max(0, src_frames - 1)
    lines: list[str] = []
    labels: list[str] = []
    for i, (ra, rb, out_dur, is_freeze) in enumerate(pieces):
        n = max(1, round(out_dur * fps))
        f0, f1 = round(ra * fps), round(rb * fps)
        if last is not None:
            f0, f1 = min(f0, last), min(f1, last + 1)
        m = 0 if is_freeze else f1 - f0
        # 比は**整数のまま**書く。小数に潰すと端で trunc が1つ落ちる
        # （62枚→56枚のはずが55枚になった。62*0.903225806 が 55.99999… になるため）
        sel = f"select='trunc((n+1)*{n}/{m})-trunc(n*{n}/{m})'," if 0 < n < m else ""
        lines.append(
            f"[{vsrc}]trim=start_frame={f0}:end_frame={max(f0 + 1, f1)},"
            f"{sel}setpts=N/{fps}/TB,"
            f"tpad=stop_mode=clone:stop_duration={out_dur + 1.0:.4f},"
            f"trim=end_frame={n},setpts=N/{fps}/TB[v{i}];"
        )
        labels.append(f"[v{i}]")
    # concat の**継ぎ目**で1枚落ちる（区間ごとに tpad→trim で枚数を固定しても数値が
    # 1つも変わらなかった＝落ちているのは区間の中ではない）。連結後に通しで PTS を
    # 振り直すと、重なった時刻が無くなって CFR 化の際に捨てられなくなる。
    lines.append(f"{''.join(labels)}concat=n={len(pieces)}:v=1:a=0[vcat];")
    lines.append(f"[vcat]setpts=N/{fps}/TB[{vout}]")
    return "\n".join(lines)


def build_warp_audio_script(pieces: list[Piece], *, asrc: str = "0:a",
                            aout: str = "outa") -> str:
    """音は**伸縮せず切って詰める** filtergraph（素材由来の音＝PC音声用）。

    倍率1.0を強制した区間（PC音声が鳴っている所）は素材尺＝出力尺なのでそのまま通る。
    それ以外は無音なので、頭から出力尺ぶんだけ採って残りを捨てる。足りなければ無音で埋める。
    ``atempo`` を使わないので**音程も速さも変わらない**。
    """
    lines: list[str] = []
    labels: list[str] = []
    for i, (ra, rb, out_dur, is_freeze) in enumerate(pieces):
        take = min(out_dur, max(0.0, rb - ra))
        if is_freeze or take <= EPS:
            lines.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{out_dur:.4f},"
                         f"asetpts=PTS-STARTPTS[a{i}];")
        else:
            lines.append(
                f"[{asrc}]atrim=start={ra:.4f}:end={ra + take:.4f},"
                f"asetpts=PTS-STARTPTS,apad=whole_dur={out_dur:.4f},"
                f"atrim=0:{out_dur:.4f},asetpts=PTS-STARTPTS[a{i}];"
            )
        labels.append(f"[a{i}]")
    lines.append(f"{''.join(labels)}concat=n={len(pieces)}:v=0:a=1[{aout}]")
    return "\n".join(lines)


def video_frame_count(src: str | Path) -> int | None:
    """素材の映像フレーム数。取れなければ ``None``（クランプしない）。

    ``nb_frames`` はコンテナによっては入っていないので、無ければパケットを数える
    （デコードしないので 17分の素材でも数秒）。
    """
    cmd = [ffprobe_path(), "-v", "error", "-select_streams", "v:0",
           "-count_packets", "-show_entries", "stream=nb_read_packets,nb_frames",
           "-of", "default=nw=1:nk=0", str(src)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        return None
    vals: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        k, _, v = line.partition("=")
        vals[k.strip()] = v.strip()
    for key in ("nb_read_packets", "nb_frames"):
        try:
            n = int(vals.get(key, ""))
        except ValueError:
            continue
        if n > 0:
            return n
    return None


def _run(cmd: list[str], what: str) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        raise RuntimeError(f"{what} に失敗:\n{ffmpeg_error(proc.stderr)}")


def _script_arg(text: str) -> tuple[str, Path]:
    """filtergraph を一時ファイルへ（``mkstemp`` の fd は**必ず閉じる**。
    開いたままだと Windows では後始末の unlink が PermissionError で落ちる）。"""
    import os

    fd, name = tempfile.mkstemp(suffix=".ffscript")
    os.close(fd)
    p = Path(name)
    p.write_text(text, encoding="utf-8")
    return str(p), p


def _cache_key(src: str | Path, script: str) -> str:
    """素材と filtergraph から作る鍵。素材の実体（大きさ・更新時刻）も混ぜる。"""
    st = Path(src).stat()
    raw = f"{Path(src).resolve()}|{st.st_size}|{int(st.st_mtime)}|{script}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _up_to_date(out: Path, key: str) -> bool:
    """``out`` が同じ鍵で作られていればそのまま使える。

    **相槌の位置を変えるだけの回でも映像とPC音声を毎回作り直していた**（実測: 相槌の配置
    そのものは9秒なのに、映像4.5分＋PC音声3.5分を捨てて作り直していた）。ワープ計画が
    変わらない限りどちらも同じものになるので、鍵が一致したら飛ばす。
    """
    side = out.with_suffix(out.suffix + ".key")
    return (out.exists() and side.exists()
            and side.read_text(encoding="utf-8").strip() == key)


def _stamp(out: Path, key: str) -> None:
    out.with_suffix(out.suffix + ".key").write_text(key, encoding="utf-8")


def render_warped_footage(
    src_video: str | Path, pieces: list[Piece], out_mp4: str | Path, *,
    fps: int, crf: int = 18, preset: str = "veryfast", refresh: bool = False,
) -> Path:
    """収録映像を Warp どおり可変速で書き出す（**映像のみ**・音は入れない）。

    中間素材なので ``crf`` は高画質側（既定18）にする。ここで劣化させると本合成の
    再エンコードと合わせて二重に効く。

    同じ計画で作った結果が残っていれば作り直さない（``refresh=True`` で強制）。
    """
    out_mp4 = Path(out_mp4)
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    script = build_warp_video_script(pieces, fps=fps,
                                     src_frames=video_frame_count(src_video))
    key = _cache_key(src_video, f"v{crf}:{preset}:{fps}:{script}")
    if not refresh and _up_to_date(out_mp4, key):
        return out_mp4
    arg, path = _script_arg(script)
    try:
        _run([ffmpeg_path(), "-y", "-i", str(src_video),
              "-filter_complex_script", arg, "-map", "[outv]", "-an",
              "-c:v", "libx264", "-pix_fmt", "yuv420p",
              "-crf", str(crf), "-preset", preset, str(out_mp4)],
             "映像のワープ")
    finally:
        path.unlink(missing_ok=True)
    _stamp(out_mp4, key)
    return out_mp4


def render_warped_audio(src_audio: str | Path, pieces: list[Piece],
                        out_wav: str | Path, *, refresh: bool = False) -> Path:
    """素材由来の音（PC音声）を Warp に合わせて切り詰めた wav を書き出す。

    同じ計画で作った結果が残っていれば作り直さない（``refresh=True`` で強制）。
    """
    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    script = build_warp_audio_script(pieces)
    key = _cache_key(src_audio, script)
    if not refresh and _up_to_date(out_wav, key):
        return out_wav
    arg, path = _script_arg(script)
    try:
        _run([ffmpeg_path(), "-y", "-i", str(src_audio),
              "-filter_complex_script", arg, "-map", "[outa]",
              "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", str(out_wav)],
             "PC音声のワープ")
    finally:
        path.unlink(missing_ok=True)
    _stamp(out_wav, key)
    return out_wav


def render_voice_track(clips: list[tuple], out_wav: str | Path, *,
                       total: float) -> Path:
    """読み上げ wav を出力座標へ**そのまま置く**だけの全長トラックを作る。

    リサンプルも伸縮もしない（＝声は一切変質しない）。``clips`` は ``(出力秒, wav)``。

    ⚠️ **台詞は重ねない**。以前は「相槌だけ相手の声に重ねる（ゲインを下げて）」を
    ここで受けていたが、**却下済みの設計**。重なりは配置で解く問題ではなく、台本の時点で
    ターンを加味して書くことで起きなくする。ゲイン引数は意図的に**存在しない**。
    """
    import numpy as np

    rate, chans = 0, 1
    for c in clips:
        with wave.open(str(c[1]), "rb") as wf:
            rate, chans = wf.getframerate(), wf.getnchannels()
        break
    if not rate:
        raise ValueError("読み上げクリップが1本もない")
    buf = np.zeros((int(round(total * rate)) + rate, chans), dtype=np.float32)
    for at, p in clips:
        with wave.open(str(p), "rb") as wf:
            if wf.getframerate() != rate:
                raise ValueError(f"サンプルレートが混在: {p}")
            x = np.frombuffer(wf.readframes(wf.getnframes()), "<i2")
        x = x.astype(np.float32).reshape(-1, wf.getnchannels())[:, :chans] / 32768.0
        i = int(round(at * rate))
        buf[i:i + len(x), :x.shape[1]] += x
    buf = np.clip(buf[:int(round(total * rate))], -1.0, 1.0)
    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_wav), "wb") as wf:
        wf.setnchannels(chans)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes((buf * 32767).astype("<i2").tobytes())
    return out_wav


def warp_edl(
    edl: Edl, warp: Warp, warped_video: Path, *,
    ranges: list[TimeRange], freezes=(), voice_paths: dict[str, Path],
    desktop_paths: dict[str, Path], subtitles, report_rows: list[dict],
) -> Edl:
    """ワープ済み素材を指す**新しい EDL** を作る（カットもフリーズも無い1本の素材）。

    時刻はすべて ``raw → src' → out`` で写す。``subtitles`` と ``report_rows`` は
    呼び出し側が**新しい out 座標で**作って渡す（読み上げの位置は Warp の
    ``placements`` が正で、元の out 位置から写像しても合わない）。
    """
    from wwedit.compose.ffmpeg_compose import _src_to_out

    def to_out(t: float) -> float:
        return round(warp.src_to_out(_src_to_out(ranges, t, freezes)), 3)

    new = edl.model_copy(deep=True)
    info = probe(warped_video)
    # **絶対パスで書く**。相対のままだと ffmpeg が「No such file」で落ちる
    # （filtergraph は一時ディレクトリ経由で渡るので cwd 依存にしてはいけない）。
    new.source.video_path = str(Path(warped_video).resolve())
    new.source.duration_s = info.duration_s
    new.segments = [Segment(id="warped", start=0.0, end=round(warp.out_total, 3))]
    new.freezes = []
    new.subtitles = list(subtitles)
    for t in new.source.audio_tracks:
        if t.is_desktop_audio and t.path in desktop_paths:
            t.path = str(Path(desktop_paths[t.path]).resolve())
            t.voice_path = None
        elif t.speaker in voice_paths:
            t.voice_path = str(Path(voice_paths[t.speaker]).resolve())
    for u in new.utterances:
        u.start, u.end = to_out(u.start), to_out(u.end)
        for w in u.words:
            w.start, w.end = to_out(w.start), to_out(w.end)
    for c in new.chapters:
        c.start_at = to_out(c.start_at)
    for f in new.framing:
        f.start, f.end = to_out(f.start), to_out(f.end)
    for o in new.overlays:
        o.start, o.end = to_out(o.start), to_out(o.end)
    meta = dict(new.meta or {})
    voice = dict(meta.get("voice") or {})
    voice["clips"] = [
        {"speaker": r["speaker"], "out_start": round(float(r["out_start"]), 3),
         "out_end": round(float(r["out_start"]) + float(r["tts_s"]), 3)}
        for r in report_rows
    ]
    voice["warped"] = True
    meta["voice"] = voice
    new.meta = meta
    return new


def write_warped_report(rows: list[dict], path: Path, *, out_total: float) -> None:
    """ワープ後の ``voice_tts_report.json``（下流の口パク・感情がこれを見る）。"""
    path.write_text(
        json.dumps({"rows": rows, "out_total": round(out_total, 3),
                    "scheduled_end": round(max((float(r["out_start"]) + float(r["tts_s"])
                                                for r in rows), default=0.0), 3),
                    "warped": True}, ensure_ascii=False, indent=1),
        encoding="utf-8")
