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


def test_pc_audio_is_mixed_but_not_window_normalized() -> None:
    """PC音声は**合成には混ぜる**が dynaudnorm は掛けない（音楽が潰れ無音が持ち上がる）。"""
    f = build_speaker_mix_filter(3, windowed=True, raw_idx=(2,))
    assert "[0:a]" in f and "[1:a]" in f  # 発話2本は前処理へ
    assert "[d0]" in f and "[d1]" in f
    assert "[d2]" not in f  # PC音声に dynaudnorm は掛けない
    assert "[d0][d1][2:a]amix=inputs=3" in f  # が、ミックスには入る
