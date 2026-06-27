from wwedit.compose.ffmpeg_compose import build_filter_script
from wwedit.edl.schema import TimeRange


def _ranges():
    return [TimeRange(start=1.0, end=2.5), TimeRange(start=10.0, end=12.0)]


def test_build_filter_script_trims_and_concats():
    s = build_filter_script(_ranges())
    # 各区間に v/a の trim、最後に concat=n=2
    assert "[0:v]trim=start=1.000:end=2.500" in s
    assert "[0:a]atrim=start=10.000:end=12.000" in s
    assert "concat=n=2:v=1:a=1[outv][outa]" in s
    # PTS振り直しが入る
    assert "setpts=PTS-STARTPTS" in s
    assert "asetpts=PTS-STARTPTS" in s


def test_build_filter_script_custom_audio_source():
    # 整音済み音声を別入力(1:a)から取る場合
    s = build_filter_script(_ranges(), vsrc="0:v", asrc="1:a")
    assert "[1:a]atrim=start=1.000:end=2.500" in s
    assert "[0:v]trim=start=1.000:end=2.500" in s
