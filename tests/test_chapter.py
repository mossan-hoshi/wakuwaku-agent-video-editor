from wwedit.chapter.detect import source_to_output, youtube_chapter_lines
from wwedit.edl.schema import Chapter, Edl, Segment, SourceMedia


def _edl():
    # keep [0,10) cut [10,20) keep [20,30)
    edl = Edl(recording_dir=".", source=SourceMedia(video_path="v.mp4", duration_s=30.0))
    edl.segments = [
        Segment(id="s0", start=0.0, end=10.0),
        Segment(id="s1", start=10.0, end=20.0, invalid=True, reason="silence"),
        Segment(id="s2", start=20.0, end=30.0),
    ]
    return edl


def test_source_to_output_maps_across_cut():
    edl = _edl()
    assert source_to_output(edl, 5.0) == 5.0       # 最初のkeep内
    assert source_to_output(edl, 15.0) == 10.0     # カット内→次keep先頭(累積10)
    assert source_to_output(edl, 25.0) == 15.0     # 2つ目keep内: 10 + (25-20)


def test_youtube_lines_force_first_zero():
    edl = _edl()
    edl.chapters = [
        Chapter(start_at=2.0, chapter_title="導入"),
        Chapter(start_at=25.0, chapter_title="本編"),
    ]
    lines = youtube_chapter_lines(edl)
    assert lines[0] == "00:00 導入"      # 先頭は必ず00:00
    assert lines[1] == "00:15 本編"      # 出力TLで 10+(25-20)=15s
