"""話者トラック判別のテスト。

**同じ人が最大2本のトラックを持ちうる**（マイク＝発話 ／ PC音声＝画面共有で流した音）。
Zoom は PC 音声を「その人の表示名の別枠」として書き出すので、同名の2本目以降が PC 音声。
ここを取り違えると、① 音楽をSTTにかけた幻聴の語がその話者の発話として入り、
② 本当の発話が丸ごと落ちる（2026-08-03 で実際に踏んだ）。
"""

from __future__ import annotations

from pathlib import Path

from wwedit.compose.ffmpeg_compose import build_speaker_mix_filter
from wwedit.ingest.tracks import detect_tracks


def _make_recording(tmp_path: Path, names: list[str], vid: str = "1082559635") -> Path:
    folder = tmp_path / "rec"
    (folder / "Audio Record").mkdir(parents=True)
    (folder / f"video{vid}.mp4").write_bytes(b"")
    (folder / f"audio{vid}.m4a").write_bytes(b"")
    for n in names:
        (folder / "Audio Record" / f"audio{n}{vid}.m4a").write_bytes(b"")
    return folder


def test_duplicate_speaker_second_track_is_desktop_audio(tmp_path: Path) -> None:
    """同名の2本目＝PC音声。1本目（発話）は文字起こし対象のまま。"""
    folder = _make_recording(tmp_path, ["mossan-hoshi1", "Taniguchi2", "Taniguchi3"])
    tracks = detect_tracks(folder).speaker_tracks
    got = [(t.speaker, t.is_desktop_audio) for t in tracks]
    assert got == [
        ("mossan-hoshi", False),
        ("Taniguchi", False),
        ("Taniguchi", True),
    ]


def test_two_speakers_can_each_have_pc_audio(tmp_path: Path) -> None:
    """**1話者あたり最大2本**＝各自のマイクと各自のPC音声（両方が同時に起こりうる）。"""
    folder = _make_recording(
        tmp_path, ["Taniguchi1", "Taniguchi2", "sakamoto3", "sakamoto4"]
    )
    tracks = detect_tracks(folder).speaker_tracks
    # 並び順は OS のファイル名ソート（Windows は大文字小文字を無視する）に依存するので、
    # 順序ではなく「連番の小さい方が発話・大きい方がPC音声」を検査する。
    got = {Path(t.path).stem.split("audio")[-1][:-10]: t.is_desktop_audio for t in tracks}
    assert got == {
        "Taniguchi1": False,
        "Taniguchi2": True,
        "sakamoto3": False,
        "sakamoto4": True,
    }


def test_desktop_hint_in_name_still_detected(tmp_path: Path) -> None:
    folder = _make_recording(tmp_path, ["desktop1", "Taniguchi2"])
    tracks = detect_tracks(folder).speaker_tracks
    assert [t.is_desktop_audio for t in tracks] == [True, False]


def test_raw_idx_is_mixed_without_window_normalization() -> None:
    """``raw_idx`` の入力は何も掛けずに混ぜる（互換経路。本番の PC音声は desktop_idx を使う）。"""
    f = build_speaker_mix_filter(3, windowed=True, raw_idx=(2,))
    assert "[0:a]" in f and "[1:a]" in f  # 発話2本は前処理へ
    assert "[d0]" in f and "[d1]" in f
    assert "[d2]" not in f  # PC音声に dynaudnorm は掛けない
    assert "[d0][d1][2:a]amix=inputs=3" in f  # が、ミックスには入る


def test_pc_audio_is_normalized_like_voice_but_quieter() -> None:
    """PC音声も**声と同じ窓ノーマライズ**を掛け、声より少し下げて混ぜる。

    2026-09-13 ユーザー指摘「システム音うるさすぎた」「話し声と同じようにノーマライズ
    （話し声よりはやや小さめ）」。素のまま混ぜるとピークが声を上回る。
    """
    from wwedit.compose.ffmpeg_compose import DESKTOP_GAIN_DB, DYNNORM

    assert DESKTOP_GAIN_DB < 0
    f = build_speaker_mix_filter(3, windowed=True, desktop_idx=(2,))
    assert f.count(DYNNORM) == 3                      # 声2本＋PC音声1本
    assert f"[2:a]{DYNNORM},volume={DESKTOP_GAIN_DB:.1f}dB[d2]" in f
    assert f.count("volume=") == 1                    # 下げるのは PC音声だけ
    assert "[d0][d1][d2]amix=inputs=3" in f


def test_render_speaker_mix_treats_pc_audio_as_desktop(tmp_path: Path, monkeypatch) -> None:
    """本番の経路（`render_speaker_mix`）が PC音声を `desktop_idx` として渡す。"""
    import subprocess

    from wwedit.compose import ffmpeg_compose
    from wwedit.edl.schema import Edl, SourceMedia, SpeakerTrack

    edl = Edl(
        recording_dir="2026-09-10",
        source=SourceMedia(
            video_path="v.mp4", fps=25, width=1920, height=1080, duration_s=10.0,
            audio_tracks=[
                SpeakerTrack(path="a.m4a", speaker="mossan-hoshi"),
                SpeakerTrack(path="b.m4a", speaker="mossan-hoshi", is_desktop_audio=True),
                SpeakerTrack(path="c.m4a", speaker="Taniguchi"),
            ],
        ),
    )
    seen: dict = {}

    def fake_run(cmd, **kw):
        seen["filter"] = cmd[cmd.index("-filter_complex") + 1]
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ffmpeg_compose.subprocess, "run", fake_run)
    ffmpeg_compose.render_speaker_mix(edl, tmp_path / "mix.wav")
    assert f"[1:a]{ffmpeg_compose.DYNNORM},volume=" in seen["filter"]
    assert "[0:a]" + ffmpeg_compose.DYNNORM + "[d0]" in seen["filter"]
