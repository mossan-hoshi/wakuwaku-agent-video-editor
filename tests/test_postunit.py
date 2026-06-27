from wwedit.edl.postunit import n_post_units, post_unit_chapter_lines, post_unit_ranges
from wwedit.edl.schema import Chapter, Edl, PostUnit, Segment, SourceMedia, TimeRange


def _edl():
    # kept = [0,60)+[60,120)（全採用・連続）。投稿2本に分割: A=[0,60), B=[60,120)
    return Edl(
        recording_dir="2026-06-04",
        source=SourceMedia(video_path="v.mp4", fps=30, width=1920, height=1080, duration_s=120.0),
        segments=[
            Segment(id="s0", start=0.0, end=60.0, invalid=False),
            Segment(id="s1", start=60.0, end=120.0, invalid=False),
        ],
        chapters=[
            Chapter(start_at=0.0, chapter_title="A開始"),
            Chapter(start_at=30.0, chapter_title="A後半"),
            Chapter(start_at=60.0, chapter_title="B開始"),
            Chapter(start_at=90.0, chapter_title="B後半"),
        ],
        post_units=[
            PostUnit(id="A", title="前半", ranges=[TimeRange(start=0.0, end=60.0)]),
            PostUnit(id="B", title="後半", ranges=[TimeRange(start=60.0, end=120.0)]),
        ],
    )


def test_n_post_units():
    assert n_post_units(_edl()) == 2
    assert n_post_units(Edl(recording_dir="x",
                            source=SourceMedia(video_path="v", fps=30, width=1, height=1,
                                               duration_s=1.0))) == 0


def test_post_unit_ranges_splits():
    edl = _edl()
    a = post_unit_ranges(edl, 0)
    b = post_unit_ranges(edl, 1)
    assert [(r.start, r.end) for r in a] == [(0.0, 60.0)]
    assert [(r.start, r.end) for r in b] == [(60.0, 120.0)]
    # 範囲外 index は kept 全体にフォールバック
    assert len(post_unit_ranges(edl, 5)) == 2


def test_post_unit_chapter_lines_unit_relative():
    edl = _edl()
    # 投稿Aは A開始(00:00)・A後半(00:30)。Bの章は含めない
    a = post_unit_chapter_lines(edl, 0)
    assert a == ["00:00 A開始", "00:30 A後半"]
    # 投稿Bは **単位内出力時刻**で先頭00:00（source60→out0）・B後半=source90→out30
    b = post_unit_chapter_lines(edl, 1)
    assert b == ["00:00 B開始", "00:30 B後半"]
