"""VAD無音検出を3ゴールデン日で検証（sileroロードは1回、各トラックforward1回）。

未取り込みの日は EDL を生成してから評価。確定パラメータ thr0.5/min_silence200/pad80。
出力は数値サマリのみ。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wwedit.common.media import probe  # noqa: E402
from wwedit.cut.autocut import segments_from_keep  # noqa: E402
from wwedit.cut.vad import SILERO_SR, _load_audio_16k, _load_silero, _union  # noqa: E402
from wwedit.edl.schema import Edl, SourceMedia, load_edl, save_edl  # noqa: E402
from wwedit.eval.golden import (  # noqa: E402
    GOLDEN_DIRS,
    interval_total,
    removed_silence_from_fcpxml,
    score_cuts,
)
from wwedit.ingest.normalize import normalize_folder_name  # noqa: E402
from wwedit.ingest.tracks import detect_tracks  # noqa: E402

THR, MINSIL, PAD, BRIDGE = 0.5, 200, 80, 0.2
DATA_ROOT = Path("data")


def ensure_edl(folder: str) -> Path:
    canonical = normalize_folder_name(Path(folder).name)
    out = DATA_ROOT / canonical / "edl.json"
    if out.exists():
        return out
    tracks = detect_tracks(folder)
    info = probe(tracks.video_path)
    edl = Edl(
        recording_dir=str(folder),
        source=SourceMedia(
            video_path=tracks.video_path, fps=info.fps or 30,
            width=info.width or 1920, height=info.height or 1080,
            duration_s=info.duration_s, audio_tracks=tracks.speaker_tracks,
        ),
        meta={"canonical_date": canonical, "video_id": tracks.video_id},
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    save_edl(edl, out)
    return out


def vad_keep(vad, wavs):
    all_iv = []
    for w in wavs:
        ts = vad.get_ts(
            w, vad.model, sampling_rate=SILERO_SR, threshold=THR,
            min_speech_duration_ms=150, min_silence_duration_ms=MINSIL, speech_pad_ms=PAD,
        )
        all_iv += [(t["start"] / SILERO_SR, t["end"] / SILERO_SR) for t in ts]
    return _union(all_iv, bridge_s=BRIDGE)


def main() -> None:
    vad = _load_silero()
    print(f"# params thr={THR} min_silence={MINSIL}ms pad={PAD}ms bridge={BRIDGE}s", flush=True)
    print(f"{'day':>12} {'dur':>6} {'gt_s':>6} {'cut_s':>6} {'recall':>7} {'prec':>6} {'iou':>6}",
          flush=True)
    for folder in GOLDEN_DIRS:
        if not Path(folder).exists():
            print(f"{Path(folder).name:>12}  (フォルダ無し)", flush=True)
            continue
        edl_path = ensure_edl(folder)
        edl = load_edl(edl_path)
        dur = edl.source.duration_s
        tracks = [t.path for t in edl.source.audio_tracks if not t.is_desktop_audio]
        wavs = [_load_audio_16k(p) for p in tracks]
        keep = vad_keep(vad, wavs)
        segs = segments_from_keep(keep, dur)
        edl.segments = segs
        save_edl(edl, edl_path)  # 確定セグメントを書き戻す
        cut = [(s.start, s.end) for s in segs if s.invalid]

        rec = Path(edl.recording_dir)
        fcps = sorted(rec.glob("video*.fcpxml")) or sorted(rec.glob("*.fcpxml"))
        if not fcps:
            print(f"{Path(folder).name:>12} {dur:>6.0f}  (fcpxml無し)", flush=True)
            continue
        gt = removed_silence_from_fcpxml(fcps[0], dur)
        m = score_cuts(cut, gt)
        print(
            f"{Path(folder).name:>12} {dur:>6.0f} {interval_total(gt):>6.0f} {m['pred_s']:>6.0f} "
            f"{m['recall']:>6.1%} {m['precision']:>5.1%} {m['iou']:>5.1%}",
            flush=True,
        )


if __name__ == "__main__":
    main()
