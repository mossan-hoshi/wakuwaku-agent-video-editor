"""左上チャプターリボン: 区間算出（潰れた章の扱い）・話者色解決・日付整形のテスト。"""
from wwedit.compose.chapter_ribbon import (
    RIBBON_SCHEMES,
    chapter_ribbon_intervals,
    format_rec_date,
    resolve_speaker_schemes,
)
from wwedit.edl.schema import Chapter, Edl, Segment, SourceMedia


def _edl(segments, chapters, colors=None):
    return Edl(
        recording_dir="2026-07-16",
        source=SourceMedia(video_path="v.mp4", fps=30, width=1920, height=1080,
                           duration_s=600.0),
        segments=segments,
        chapters=chapters,
        subtitle_speaker_colors=colors or {},
    )


def test_intervals_contiguous_cover_and_speaker():
    # 全採用 [0,600)。章 0/200/400。区間は連続して total まで埋まる。
    edl = _edl(
        [Segment(id="s0", start=0.0, end=600.0, invalid=False)],
        [
            Chapter(start_at=0.0, chapter_title="A", speaker="mossan-hoshi"),
            Chapter(start_at=200.0, chapter_title="B", speaker="Taniguchi"),
            Chapter(start_at=400.0, chapter_title="C", speaker="mossan-hoshi"),
        ],
    )
    ivs, total = chapter_ribbon_intervals(edl)
    assert total == 600.0
    assert [(round(i["out_start"]), round(i["out_end"])) for i in ivs] == [
        (0, 200), (200, 400), (400, 600)]
    assert [i["title"] for i in ivs] == ["A", "B", "C"]
    assert [i["speaker"] for i in ivs] == ["mossan-hoshi", "Taniguchi", "mossan-hoshi"]


def test_collapsed_chapter_dropped_next_owns_interval():
    # 冒頭 [0,50) をカット。章0(10s)・章1(30s) は両方カット域→出力0に潰れる。
    # 潰れた intro章0 は捨て、実際に流れる章1(Taniguchi) が先頭 [0, ...] を担当する。
    edl = _edl(
        [
            Segment(id="s0", start=0.0, end=50.0, invalid=True),
            Segment(id="s1", start=50.0, end=600.0, invalid=False),
        ],
        [
            Chapter(start_at=10.0, chapter_title="intro", speaker="mossan-hoshi"),
            Chapter(start_at=30.0, chapter_title="ComfyUI", speaker="Taniguchi"),
            Chapter(start_at=300.0, chapter_title="news", speaker="mossan-hoshi"),
        ],
    )
    ivs, total = chapter_ribbon_intervals(edl)
    assert total == 550.0
    assert [i["title"] for i in ivs] == ["ComfyUI", "news"]      # intro は除外
    assert ivs[0]["out_start"] == 0.0                            # 先頭は 00:00
    assert ivs[0]["speaker"] == "Taniguchi"


def test_speaker_empty_inherits_previous():
    edl = _edl(
        [Segment(id="s0", start=0.0, end=600.0, invalid=False)],
        [
            Chapter(start_at=0.0, chapter_title="A", speaker="Taniguchi"),
            Chapter(start_at=300.0, chapter_title="B", speaker=""),  # 空→直前を継承
        ],
    )
    ivs, _ = chapter_ribbon_intervals(edl)
    assert [i["speaker"] for i in ivs] == ["Taniguchi", "Taniguchi"]


def test_resolve_speaker_schemes_matches_subtitle_keys():
    # 字幕色を明示指定→リボン配色がその色キーに一致（mossan=blue=現行/taniguchi=purple）
    edl = _edl(
        [Segment(id="s0", start=0.0, end=600.0, invalid=False)],
        [
            Chapter(start_at=0.0, chapter_title="A", speaker="mossan-hoshi"),
            Chapter(start_at=300.0, chapter_title="B", speaker="Taniguchi"),
        ],
        colors={"mossan-hoshi": "blue", "Taniguchi": "purple"},
    )
    schemes = resolve_speaker_schemes(edl)
    assert schemes["mossan-hoshi"] == RIBBON_SCHEMES["blue"]
    assert schemes["Taniguchi"] == RIBBON_SCHEMES["purple"]


def test_format_rec_date():
    assert format_rec_date("2026-07-16") == "7/16収録"
    # 日付を含むフルパス（絵文字入り）でも抽出できる
    assert format_rec_date("D:/x/2026-07-16 18.00.21 [x] y") == "7/16収録"
    assert format_rec_date("no-date") == "収録"
