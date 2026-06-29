from wwedit.compose.eyecatch_insert import (
    eyecatch_boundaries,
    shifted_chapter_lines,
)
from wwedit.edl.schema import Chapter, Edl, Segment, SourceMedia


def _edl():
    # 全採用・連続 [0,600)。章: 0/120/300（出力時刻=ソース時刻）
    return Edl(
        recording_dir="2026-06-04",
        source=SourceMedia(video_path="v.mp4", fps=30, width=1920, height=1080,
                           duration_s=600.0),
        segments=[Segment(id="s0", start=0.0, end=600.0, invalid=False)],
        chapters=[
            Chapter(start_at=0.0, chapter_title="開会"),
            Chapter(start_at=120.0, chapter_title="本題"),
            Chapter(start_at=300.0, chapter_title="まとめ"),
        ],
    )


def test_boundaries_basic():
    bounds, total = eyecatch_boundaries(_edl(), duration=2.0)
    assert total == 600.0
    assert [b["out_at"] for b in bounds] == [0.0, 120.0, 300.0]
    assert [b["index"] for b in bounds] == [0, 1, 2]
    assert [b["title"] for b in bounds] == ["開会", "本題", "まとめ"]


def test_boundaries_drop_tail_chapter():
    # 末尾(=total)に章がある場合は後ろに本編が無いので除外
    edl = _edl()
    edl.chapters.append(Chapter(start_at=600.0, chapter_title="終端"))
    bounds, _ = eyecatch_boundaries(edl, duration=2.0)
    assert "終端" not in [b["title"] for b in bounds]


def test_shifted_chapter_lines_offsets_skip_first():
    # 既定 skip_first=True: 先頭章はアイキャッチ無し→手前のEC数だけずれる
    lines = shifted_chapter_lines(_edl(), duration=2.0)
    assert lines[0] == "00:00 開会"          # EC無し
    assert lines[1] == "02:00 本題"          # 120 + 0*2（手前のEC 0個）
    assert lines[2] == "05:02 まとめ"        # 300 + 1*2 = 302（手前のEC 1個）


def test_shifted_chapter_lines_no_skip():
    # skip_first=False: 全章にEC→ out_at + i*2（従来挙動）
    lines = shifted_chapter_lines(_edl(), duration=2.0, skip_first=False)
    assert lines[1] == "02:02 本題"          # 120 + 1*2 = 122
    assert lines[2] == "05:04 まとめ"        # 300 + 2*2 = 304


def test_shifted_chapter_lines_hms():
    edl = _edl()
    edl.chapters = [
        Chapter(start_at=0.0, chapter_title="A"),
        Chapter(start_at=3600.0, chapter_title="B"),
    ]
    edl.segments = [Segment(id="s0", start=0.0, end=7200.0, invalid=False)]
    edl.source.duration_s = 7200.0
    lines = shifted_chapter_lines(edl, duration=2.0)  # skip_first 既定
    assert lines[0] == "00:00 A"
    assert lines[1] == "1:00:00 B"  # 3600 + 0*2（先頭EC無し）→ H:MM:SS
