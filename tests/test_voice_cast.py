"""publish.voice_cast（[V] キャラ割当）のテスト。"""

from __future__ import annotations

import random

import pytest

from wwedit.edl.schema import Edl, Freeze, SourceMedia, SpeakerTrack
from wwedit.publish.voice_cast import apply_cast, mic_speakers, pick_cast, revert_voice


def _edl() -> Edl:
    return Edl(
        recording_dir="2026-01-01",
        source=SourceMedia(
            video_path="v.mp4",
            audio_tracks=[
                SpeakerTrack(speaker="mossan-hoshi", path="a.m4a"),
                SpeakerTrack(speaker="Taniguchi", path="b.m4a"),
                SpeakerTrack(speaker="Taniguchi", path="c.m4a", is_desktop_audio=True),
            ],
        ),
        subtitle_speaker_colors={"Taniguchi": "red"},
    )


def test_mic_speakers_excludes_desktop():
    assert mic_speakers(_edl()) == ["Taniguchi", "mossan-hoshi"]


def test_pick_cast_random_unique():
    cast = pick_cast(_edl(), rng=random.Random(42))
    assert set(cast) == {"Taniguchi", "mossan-hoshi"}
    assert len(set(cast.values())) == 2  # 重複なし


def test_pick_cast_named_chars_in_speaker_order():
    cast = pick_cast(_edl(), chars=["noa", "suzu"])
    assert cast == {"Taniguchi": "noa", "mossan-hoshi": "suzu"}  # ソート順に割当


def test_pick_cast_rejects_unknown_char():
    with pytest.raises(ValueError, match="未知のキャラID"):
        pick_cast(_edl(), chars=["noa", "unknown-chan"])


def test_pick_cast_excludes_non_original_characters():
    """つくよみちゃんは外部のフリー素材キャラなので指名でもランダムでも使わない。"""
    from wwedit.publish.voice_cast import NON_ORIGINAL_CHARS

    with pytest.raises(ValueError, match="オリジナルではない"):
        pick_cast(_edl(), chars=["tsukuyomi", "noa"])
    for seed in range(30):
        cast = pick_cast(_edl(), rng=random.Random(seed))
        assert not (set(cast.values()) & NON_ORIGINAL_CHARS)
    # pool 指定で紛れ込んでも除外される
    cast = pick_cast(_edl(), pool=["tsukuyomi", "noa", "suzu"], rng=random.Random(0))
    assert not (set(cast.values()) & NON_ORIGINAL_CHARS)


def test_apply_cast_writes_all_and_snapshots_colors():
    edl = _edl()
    cast = {"mossan-hoshi": "noa", "Taniguchi": "suzu"}
    apply_cast(edl, cast, method="seedvc")
    assert edl.character_cast == cast
    assert edl.subtitle_speaker_colors["mossan-hoshi"] == "noa"
    assert edl.subtitle_speaker_colors["Taniguchi"] == "suzu"
    assert edl.chibi is not None and edl.chibi.enabled
    assert edl.meta["voice"]["method"] == "seedvc"
    assert edl.meta["voice"]["prev_colors"] == {"Taniguchi": "red"}  # 元の色を退避
    # 再キャストしても最初のスナップショットを保持
    apply_cast(edl, {"mossan-hoshi": "yume", "Taniguchi": "ritsu"}, method="tts")
    assert edl.meta["voice"]["prev_colors"] == {"Taniguchi": "red"}


def test_apply_cast_rejects_bad_method():
    with pytest.raises(ValueError, match="method"):
        apply_cast(_edl(), {"mossan-hoshi": "noa"}, method="magic")


def test_revert_voice_restores_everything():
    edl = _edl()
    apply_cast(edl, {"mossan-hoshi": "noa", "Taniguchi": "suzu"}, method="tts")
    edl.source.audio_tracks[0].voice_path = "a_vc.wav"
    edl.freezes = [Freeze(at=1.0, extra=0.5)]
    done = revert_voice(edl)
    assert done  # 何かしら戻した
    assert edl.character_cast == {}
    assert edl.subtitle_speaker_colors == {"Taniguchi": "red"}  # 元の色に復元
    assert edl.freezes == []
    assert edl.source.audio_tracks[0].voice_path is None
    assert not edl.chibi.enabled
    assert "voice" not in edl.meta


def test_revert_voice_noop_when_not_cast():
    assert revert_voice(_edl()) == []
