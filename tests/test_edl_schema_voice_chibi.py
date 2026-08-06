from wwedit.edl.schema import ChibiConfig, Edl, Freeze, SourceMedia, SpeakerTrack, Utterance


def _minimal_edl_dict():
    """新フィールドを一切含まない旧形式のEDL dict。"""
    return {
        "recording_dir": "2026-01-01",
        "source": {
            "video_path": "v.mp4",
            "audio_tracks": [
                {"speaker": "mossan-hoshi", "path": "a.m4a"},
                {"speaker": "Taniguchi", "path": "b.m4a", "is_desktop_audio": True},
            ],
        },
        "utterances": [{"speaker": "mossan-hoshi", "text": "こんにちは", "start": 0.0, "end": 1.0}],
    }


def test_old_edl_loads_with_defaults():
    edl = Edl.model_validate(_minimal_edl_dict())
    assert edl.character_cast == {}
    assert edl.freezes == []
    assert edl.chibi is None
    assert edl.source.audio_tracks[0].voice_path is None
    assert edl.utterances[0].emotion is None


def test_voice_chibi_fields_round_trip():
    edl = Edl(
        recording_dir="2026-01-01",
        source=SourceMedia(
            video_path="v.mp4",
            audio_tracks=[
                SpeakerTrack(speaker="mossan-hoshi", path="a.m4a", voice_path="a_vc.wav")
            ],
        ),
        utterances=[
            Utterance(speaker="mossan-hoshi", text="やった", start=0.0, end=1.0, emotion="smile")
        ],
        character_cast={"mossan-hoshi": "noa", "Taniguchi": "suzu"},
        freezes=[Freeze(at=12.3, extra=0.8, note="u0005")],
        chibi=ChibiConfig(enabled=True, sides={"left": "Taniguchi", "right": "mossan-hoshi"}),
    )
    edl2 = Edl.model_validate(edl.model_dump(mode="json"))
    assert edl2.character_cast["mossan-hoshi"] == "noa"
    assert edl2.freezes[0].extra == 0.8
    assert edl2.chibi.enabled and edl2.chibi.height_px == 320
    assert edl2.source.audio_tracks[0].voice_path == "a_vc.wav"
    assert edl2.utterances[0].emotion == "smile"
