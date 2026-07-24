"""自前手修正エディタ（字幕スライス）のAPIテスト。"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from wwedit.edl.schema import (
    Chapter,
    Edl,
    FramingRegion,
    PostUnit,
    Segment,
    SourceMedia,
    Subtitle,
    TimeRange,
    Utterance,
    Word,
    load_edl,
)


def _edl(tmp_path: Path) -> Path:
    edl = Edl(
        recording_dir="2026-06-04",
        source=SourceMedia(
            video_path=str(tmp_path / "v.mp4"), fps=25, width=1920, height=1080, duration_s=100.0
        ),
        segments=[
            Segment(id="s0", start=4.0, end=8.0, invalid=False),
            Segment(id="s1", start=8.0, end=12.0, invalid=True, reason="silence"),
            Segment(id="s2", start=12.0, end=20.0, invalid=False),
        ],
        subtitles=[
            Subtitle(start=4.5, end=7.0, text="あいさつ", style="main", speaker="mossan-hoshi"),
            Subtitle(start=13.0, end=16.0, text="テスト発話", style="main", speaker="Taniguchi"),
        ],
        framing=[
            FramingRegion(start=4.0, end=10.0, kind="static"),
            FramingRegion(start=10.0, end=20.0, kind="loading", loading_label="準備"),
        ],
        chapters=[
            Chapter(start_at=4.0, chapter_title="導入", section_title="A"),
            Chapter(start_at=12.0, chapter_title="本題", section_title="B"),
        ],
        utterances=[
            Utterance(speaker="mossan-hoshi", text="やあ どうも", start=4.5, end=5.5,
                      words=[Word(text="やあ", start=4.5, end=4.9),
                             Word(text="どうも", start=5.0, end=5.5)]),
        ],
        post_units=[
            PostUnit(id="post00", title="第1回",
                     ranges=[TimeRange(start=4.0, end=8.0), TimeRange(start=12.0, end=20.0)],
                     chapter_ids=[0, 1]),
        ],
    )
    p = tmp_path / "edl.json"
    from wwedit.edl.schema import save_edl

    save_edl(edl, p)
    return p


def _client(edl_path: Path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wwedit.webapp.editor import create_editor_app

    return TestClient(create_editor_app(edl_path))


def test_get_edl_maps_output_time(tmp_path: Path):
    c = _client(_edl(tmp_path))
    d = c.get("/api/edl").json()
    assert len(d["subtitles"]) == 2
    # source4.5 → out0.5、source13 → out (8-4)+(13-12)=5.0
    assert d["subtitles"][0]["out_start"] == pytest.approx(0.5, abs=1e-6)
    assert d["subtitles"][1]["out_start"] == pytest.approx(5.0, abs=1e-6)


def test_edit_subtitle_persists_and_logs(tmp_path: Path):
    edl_path = _edl(tmp_path)
    c = _client(edl_path)
    r = c.post("/api/subtitle/1", json={"text": "タニグチの発話"})
    assert r.status_code == 200 and r.json()["after"]["text"] == "タニグチの発話"
    # EDLに非破壊保存された
    edl = load_edl(edl_path)
    assert any(s.text == "タニグチの発話" for s in edl.subtitles)
    # 修正ログが追記された
    log_txt = (edl_path.parent / "correction_log.jsonl").read_text(encoding="utf-8")
    entry = json.loads(log_txt.strip().splitlines()[-1])
    assert entry["type"] == "subtitle_edit" and entry["after"]["text"] == "タニグチの発話"


def test_speaker_color_override_and_auto(tmp_path: Path):
    edl_path = _edl(tmp_path)
    c = _client(edl_path)
    c.post("/api/speaker-color", json={"speaker": "Taniguchi", "color": "red"})
    assert load_edl(edl_path).subtitle_speaker_colors.get("Taniguchi") == "red"
    # auto で上書き解除
    c.post("/api/speaker-color", json={"speaker": "Taniguchi", "color": "auto"})
    assert "Taniguchi" not in load_edl(edl_path).subtitle_speaker_colors


def test_edit_rejects_bad_time(tmp_path: Path):
    c = _client(_edl(tmp_path))
    r = c.post("/api/subtitle/0", json={"start": 7.0, "end": 5.0})
    assert r.status_code == 400


def test_timeline_has_all_tracks(tmp_path: Path):
    c = _client(_edl(tmp_path))
    d = c.get("/api/timeline").json()
    assert d["source"]["duration"] == pytest.approx(100.0)
    assert d["source"]["url"] == "/media/source"
    assert len(d["segments"]) == 3  # keep/silence/keep
    assert len(d["subtitles"]) == 2
    assert d["preview"]["available"] is False  # preview未指定
    # kept_ranges で out へ写像できる
    assert d["kept_ranges"][0]["start"] == pytest.approx(4.0)


def test_segment_toggle_persists_and_logs(tmp_path: Path):
    edl_path = _edl(tmp_path)
    c = _client(edl_path)
    # keep区間 s0 をカット(無効化)
    r = c.post("/api/segment/s0", json={"invalid": True})
    assert r.status_code == 200 and r.json()["invalid"] is True
    assert next(s for s in load_edl(edl_path).segments if s.id == "s0").invalid is True
    log = (edl_path.parent / "correction_log.jsonl").read_text(encoding="utf-8")
    assert '"type": "segment_edit"' in log
    # 復活
    c.post("/api/segment/s0", json={"invalid": False})
    assert next(s for s in load_edl(edl_path).segments if s.id == "s0").invalid is False


def test_timeline_includes_framing_and_chapters(tmp_path: Path):
    c = _client(_edl(tmp_path))
    d = c.get("/api/timeline").json()
    assert len(d["framing"]) == 2 and d["framing"][0]["kind"] == "static"
    assert len(d["chapters"]) == 2 and d["chapters"][0]["chapter_title"] == "導入"


def test_edit_framing_range_and_kind(tmp_path: Path):
    edl_path = _edl(tmp_path)
    c = _client(edl_path)
    r = c.post("/api/framing/0", json={"start": 5.0, "end": 9.0, "kind": "pending",
                                       "loading_label": "準備中"})
    assert r.status_code == 200
    f = sorted(load_edl(edl_path).framing, key=lambda x: x.start)[0]
    assert f.kind == "pending" and f.loading_label == "準備中"
    assert f.start == 5.0 and f.end == 9.0
    assert '"type": "framing_edit"' in (edl_path.parent / "correction_log.jsonl").read_text("utf-8")


def test_edit_chapter_title_and_move(tmp_path: Path):
    edl_path = _edl(tmp_path)
    c = _client(edl_path)
    r = c.post("/api/chapter/0", json={"chapter_title": "オープニング", "start_at": 6.0,
                                       "is_required": True})
    assert r.status_code == 200
    ch = sorted(load_edl(edl_path).chapters, key=lambda x: x.start_at)[0]
    assert ch.chapter_title == "オープニング" and ch.start_at == 6.0 and ch.is_required is True


def test_edit_postunit_title(tmp_path: Path):
    edl_path = _edl(tmp_path)
    c = _client(edl_path)
    r = c.post("/api/postunit/0", json={"title": "改題"})
    assert r.status_code == 200
    assert load_edl(edl_path).post_units[0].title == "改題"


def test_edit_postunit_span_recomputes_ranges(tmp_path: Path):
    # 始端ドラッグ: start=12 → kept∩[12,20] = s2(12-20) のみ（s0 4-8 は範囲外）
    edl_path = _edl(tmp_path)
    c = _client(edl_path)
    r = c.post("/api/postunit/0", json={"start": 12.0})
    assert r.status_code == 200
    rg = load_edl(edl_path).post_units[0].ranges
    assert len(rg) == 1
    assert rg[0].start == pytest.approx(12.0) and rg[0].end == pytest.approx(20.0)
    # 始端を 4 に戻す → kept∩[4,20] = s0(4-8)+s2(12-20) の2区間に復活
    c.post("/api/postunit/0", json={"start": 4.0})
    rg2 = load_edl(edl_path).post_units[0].ranges
    assert len(rg2) == 2
    assert rg2[0].start == pytest.approx(4.0)


def test_edit_framing_clamps_inverted_range(tmp_path: Path):
    # 反転入力(start>end)は segment と同様クランプで最小区間に補正（区間が消えない）
    edl_path = _edl(tmp_path)
    c = _client(edl_path)
    r = c.post("/api/framing/0", json={"start": 9.0, "end": 5.0})
    assert r.status_code == 200
    f = sorted(load_edl(edl_path).framing, key=lambda x: x.start)[0]
    assert f.start == pytest.approx(9.0) and f.end == pytest.approx(9.05)


def test_edit_framing_sets_and_clears_bbox(tmp_path: Path):
    edl_path = _edl(tmp_path)
    c = _client(edl_path)
    # crop bbox (x,y,w,h) を設定
    r = c.post("/api/framing/0", json={"bbox": [320, 180, 1280, 720]})
    assert r.status_code == 200
    f = sorted(load_edl(edl_path).framing, key=lambda x: x.start)[0]
    assert tuple(f.bbox) == (320, 180, 1280, 720)
    # clear_crop で全画面(None)へ戻す
    c.post("/api/framing/0", json={"clear_crop": True})
    assert sorted(load_edl(edl_path).framing, key=lambda x: x.start)[0].bbox is None
    assert '"type": "framing_edit"' in (edl_path.parent / "correction_log.jsonl").read_text("utf-8")


def test_edit_framing_rejects_bad_bbox(tmp_path: Path):
    c = _client(_edl(tmp_path))
    assert c.post("/api/framing/0", json={"bbox": [1, 2, 3]}).status_code == 400


def test_add_subtitle_and_chapter(tmp_path: Path):
    edl_path = _edl(tmp_path)
    c = _client(edl_path)
    r = c.post("/api/subtitle", json={"start": 14.5, "end": 16.5, "text": "追加字幕"})
    assert r.status_code == 200
    edl = load_edl(edl_path)
    assert len(edl.subtitles) == 3 and any(s.text == "追加字幕" for s in edl.subtitles)
    # 追加idxでそのまま編集できる
    idx = r.json()["idx"]
    assert c.post(f"/api/subtitle/{idx}", json={"text": "改名"}).json()["after"]["text"] == "改名"
    rc = c.post("/api/chapter", json={"start_at": 15.0})
    assert rc.status_code == 200 and len(load_edl(edl_path).chapters) == 3


def test_add_subtitle_rejects_bad_range(tmp_path: Path):
    c = _client(_edl(tmp_path))
    assert c.post("/api/subtitle", json={"start": 5.0, "end": 5.0}).status_code == 400


def test_segment_boundary_move_adjusts_neighbor(tmp_path: Path):
    edl_path = _edl(tmp_path)
    c = _client(edl_path)
    # s0[4,8] の右端を 6 へ → 隣 s1 の start も 6 に追従（連続性維持）
    r = c.post("/api/segment/s0", json={"end": 6.0})
    assert r.status_code == 200
    segs = {s.id: s for s in load_edl(edl_path).segments}
    assert segs["s0"].end == 6.0 and segs["s1"].start == 6.0


def test_segment_split_creates_two(tmp_path: Path):
    edl_path = _edl(tmp_path)
    c = _client(edl_path)
    before = len(load_edl(edl_path).segments)
    r = c.post("/api/segment/split", json={"at": 6.0})  # s0[4,8] を6で分割
    assert r.status_code == 200
    segs = sorted(load_edl(edl_path).segments, key=lambda s: s.start)
    assert len(segs) == before + 1
    # 6.0 で連続する2区間に割れている
    pair = [s for s in segs if s.start in (4.0, 6.0) and s.end in (6.0, 8.0)]
    assert any(s.start == 4.0 and s.end == 6.0 for s in pair)
    assert any(s.start == 6.0 and s.end == 8.0 for s in pair)


def test_segment_split_on_boundary_rejected(tmp_path: Path):
    c = _client(_edl(tmp_path))
    assert c.post("/api/segment/split", json={"at": 8.0}).status_code == 400  # 境界上


def test_cut_range_splits_edges_and_invalidates(tmp_path: Path):
    # segments: s0[4,8]keep, s1[8,12]cut, s2[12,20]keep。[6,14]を範囲カット
    edl_path = _edl(tmp_path)
    c = _client(edl_path)
    r = c.post("/api/segment/cut-range", json={"start": 6.0, "end": 14.0})
    assert r.status_code == 200
    segs = sorted(load_edl(edl_path).segments, key=lambda s: s.start)
    # 端が分割され [4,6]keep / [6,8]cut / [8,12]cut / [12,14]cut / [14,20]keep
    def inv(a, b):
        return next(s.invalid for s in segs if s.start == a and s.end == b)
    assert inv(4.0, 6.0) is False and inv(14.0, 20.0) is False  # 範囲外は維持
    assert inv(6.0, 8.0) and inv(8.0, 12.0) and inv(12.0, 14.0)  # 範囲内は全てカット
    assert '"type": "cut_range"' in (edl_path.parent / "correction_log.jsonl").read_text("utf-8")


def test_cut_range_rejects_too_short(tmp_path: Path):
    c = _client(_edl(tmp_path))
    assert c.post("/api/segment/cut-range", json={"start": 6.0, "end": 6.02}).status_code == 400


def test_transcript_returns_words_in_time_order(tmp_path: Path):
    c = _client(_edl(tmp_path))
    d = c.get("/api/transcript").json()
    assert [w["text"] for w in d["words"]] == ["やあ", "どうも"]
    assert d["words"][0]["t"] == 4.5 and d["words"][0]["speaker"] == "mossan-hoshi"


def test_timeline_includes_post_units(tmp_path: Path):
    d = _client(_edl(tmp_path)).get("/api/timeline").json()
    assert len(d["post_units"]) == 1
    pu = d["post_units"][0]
    # スパンは全レンジの最小start〜最大end
    assert pu["start"] == 4.0 and pu["end"] == 20.0 and pu["n_ranges"] == 2


def test_split_framing_creates_two(tmp_path: Path):
    edl_path = _edl(tmp_path)
    c = _client(edl_path)
    before = len(load_edl(edl_path).framing)
    r = c.post("/api/framing/split", json={"at": 7.0})  # static[4,10] を7で分割
    assert r.status_code == 200
    fr = sorted(load_edl(edl_path).framing, key=lambda x: x.start)
    assert len(fr) == before + 1
    assert any(x.start == 4.0 and x.end == 7.0 and x.kind == "static" for x in fr)
    assert any(x.start == 7.0 and x.end == 10.0 and x.kind == "static" for x in fr)


def test_split_framing_on_boundary_rejected(tmp_path: Path):
    c = _client(_edl(tmp_path))
    assert c.post("/api/framing/split", json={"at": 10.0}).status_code == 400  # 境界上


def test_merge_framing_unifies_to_neighbor(tmp_path: Path):
    edl_path = _edl(tmp_path)
    c = _client(edl_path)
    # framing[0]=[4,10]static, framing[1]=[10,20]loading(準備) は接している → 右と結合
    r = c.post("/api/framing/0/merge", json={"dir": 1})
    assert r.status_code == 200
    fr = sorted(load_edl(edl_path).framing, key=lambda x: x.start)
    assert len(fr) == 1
    assert fr[0].start == 4.0 and fr[0].end == 20.0  # 範囲は両者を覆う
    assert fr[0].kind == "loading" and fr[0].loading_label == "準備"  # 内容=隣接側
    log = (edl_path.parent / "correction_log.jsonl").read_text("utf-8")
    assert '"type": "framing_merge"' in log


def test_merge_subtitle_rejects_gap(tmp_path: Path):
    c = _client(_edl(tmp_path))
    # 字幕[4.5,7.0]と[13,16]は隙間あり → 結合不可
    assert c.post("/api/subtitle/0/merge", json={"dir": 1}).status_code == 400


def test_merge_framing_rejects_no_neighbor(tmp_path: Path):
    c = _client(_edl(tmp_path))
    assert c.post("/api/framing/0/merge", json={"dir": -1}).status_code == 400  # 左に隣接なし


def test_edit_post_unit_title(tmp_path: Path):
    edl_path = _edl(tmp_path)
    c = _client(edl_path)
    assert c.post("/api/postunit/0", json={"title": "第1回・改"}).status_code == 200
    assert load_edl(edl_path).post_units[0].title == "第1回・改"


# ── ユーザー配置オーバーレイ（最上位レイヤー） ──────────────────────────
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def test_add_text_overlay_and_timeline_exposes_it(tmp_path: Path):
    edl_path = _edl(tmp_path)
    c = _client(edl_path)
    r = c.post("/api/overlay", json={"kind": "text", "start": 5.0, "end": 7.0,
                                     "x": 0.25, "y": 0.1, "text": "重ね文字",
                                     "color": "purple", "size": 72})
    assert r.status_code == 200 and r.json()["ok"]
    ov = load_edl(edl_path).overlays[0]
    assert (ov.kind, ov.text, ov.color, ov.size) == ("text", "重ね文字", "purple", 72)
    assert ov.double_border is True          # 既定で字幕と同じ二重縁取り
    t = c.get("/api/timeline").json()["overlays"][0]
    assert t["source_start"] == 5.0 and t["out_start"] == 1.0   # keep先頭4.0基準
    assert t["css_color"] == "#783cb4"       # purple の CSS プレビュー色


def test_overlay_zero_length_gets_default_span(tmp_path: Path):
    edl_path = _edl(tmp_path)
    c = _client(edl_path)
    c.post("/api/overlay", json={"kind": "text", "start": 5.0, "end": 5.0})
    ov = load_edl(edl_path).overlays[0]
    assert ov.end - ov.start == pytest.approx(3.0)


def test_edit_overlay_updates_and_clamps_position(tmp_path: Path):
    edl_path = _edl(tmp_path)
    c = _client(edl_path)
    c.post("/api/overlay", json={"kind": "text", "start": 5.0, "end": 7.0})
    assert c.post("/api/overlay/0", json={"x": 1.9, "y": -0.4,
                                          "double_border": False}).status_code == 200
    ov = load_edl(edl_path).overlays[0]
    assert (ov.x, ov.y) == (1.0, 0.0)        # 0..1 にクランプ
    assert ov.double_border is False


def test_delete_overlay(tmp_path: Path):
    edl_path = _edl(tmp_path)
    c = _client(edl_path)
    c.post("/api/overlay", json={"kind": "text", "start": 5.0, "end": 7.0})
    assert c.delete("/api/overlay/0").status_code == 200
    assert load_edl(edl_path).overlays == []
    assert c.delete("/api/overlay/0").status_code == 404


def test_upload_image_saves_and_dedups_name(tmp_path: Path):
    edl_path = _edl(tmp_path)
    c = _client(edl_path)
    r1 = c.post("/api/overlay/upload", files={"file": ("a.png", _PNG, "image/png")})
    r2 = c.post("/api/overlay/upload", files={"file": ("a.png", _PNG, "image/png")})
    assert r1.status_code == 200 and r2.status_code == 200
    assert Path(r1.json()["path"]).exists()
    assert r1.json()["name"] == "a.png" and r2.json()["name"] == "a_1.png"  # 上書きしない
    assert c.get("/media/overlay/a.png").status_code == 200


def test_sequential_image_overlays_5s_each(tmp_path: Path):
    """複数画像は再生ヘッドから5秒ずつ連続で並ぶ（UIの並べ方をAPIレベルで再現）。"""
    edl_path = _edl(tmp_path)
    c = _client(edl_path)
    t = 4.0
    for name in ("a.png", "b.png", "c.png"):
        up = c.post("/api/overlay/upload", files={"file": (name, _PNG, "image/png")}).json()
        c.post("/api/overlay", json={"kind": "image", "start": t, "end": t + 5.0,
                                     "path": up["path"]})
        t += 5.0
    ovs = load_edl(edl_path).overlays
    assert [(o.start, o.end) for o in ovs] == [(4.0, 9.0), (9.0, 14.0), (14.0, 19.0)]
    assert all(o.kind == "image" for o in ovs)


def test_overlay_align_and_line_spacing_roundtrip(tmp_path: Path):
    edl_path = _edl(tmp_path)
    c = _client(edl_path)
    c.post("/api/overlay", json={"kind": "text", "start": 5.0, "end": 7.0,
                                 "align": "center", "line_spacing": 1.4})
    o = load_edl(edl_path).overlays[0]
    assert o.align == "center" and o.line_spacing == pytest.approx(1.4)
    c.post("/api/overlay/0", json={"align": "right", "line_spacing": 0.8})
    o = load_edl(edl_path).overlays[0]
    assert o.align == "right" and o.line_spacing == pytest.approx(0.8)


def test_overlay_invalid_align_falls_back_to_left(tmp_path: Path):
    edl_path = _edl(tmp_path)
    c = _client(edl_path)
    c.post("/api/overlay", json={"kind": "text", "start": 5.0, "end": 7.0, "align": "bogus"})
    assert load_edl(edl_path).overlays[0].align == "left"


def test_delete_overlay_returns_snapshot_for_undo(tmp_path: Path):
    """削除は復元用の完全ペイロードを返し、同一IDで再作成できる（Ctrl+Z の裏付け）。"""
    edl_path = _edl(tmp_path)
    c = _client(edl_path)
    c.post("/api/overlay", json={"kind": "text", "start": 5.0, "end": 7.0,
                                 "text": "消して戻す", "color": "green", "id": "keepme01"})
    snap = c.delete("/api/overlay/0").json()["overlay"]
    assert snap["id"] == "keepme01" and load_edl(edl_path).overlays == []
    # スナップショットをそのまま再POST＝Undo。同じIDで復活する
    c.post("/api/overlay", json=snap)
    back = load_edl(edl_path).overlays
    assert len(back) == 1 and back[0].id == "keepme01" and back[0].text == "消して戻す"


def test_framing_edge_drag_links_neighbor(tmp_path: Path):
    """シーン(framing)区間の端を動かすと隣接区間の端も追従して連続性を保つ。"""
    edl_path = _edl(tmp_path)
    c = _client(edl_path)
    # framing: [4,10] static, [10,20] loading。区間0の右端を10→13へ
    r = c.post("/api/framing/0", json={"end": 13.0})
    assert r.status_code == 200
    regs = sorted(load_edl(edl_path).framing, key=lambda x: x.start)
    assert regs[0].end == pytest.approx(13.0)
    assert regs[1].start == pytest.approx(13.0)   # 隣接が追従＝隙間/重なり無し
    assert r.json()["neighbors"] == [{"idx": 1, "start": 10.0, "end": 20.0}]
    # 区間1の左端を戻す（13→11）→ 区間0の右端も追従
    c.post("/api/framing/1", json={"start": 11.0})
    regs = sorted(load_edl(edl_path).framing, key=lambda x: x.start)
    assert regs[0].end == pytest.approx(11.0) and regs[1].start == pytest.approx(11.0)


def test_framing_edge_clamped_to_neighbor(tmp_path: Path):
    """端を隣接の外まで動かそうとしてもクランプされ、区間が消えない。"""
    edl_path = _edl(tmp_path)
    c = _client(edl_path)
    # 区間0の右端を100へ（区間1の終端20を超える）→ 20-0.05 にクランプ
    c.post("/api/framing/0", json={"end": 100.0})
    regs = sorted(load_edl(edl_path).framing, key=lambda x: x.start)
    assert regs[0].end == pytest.approx(19.95)
    assert regs[1].start == pytest.approx(19.95) and regs[1].end == pytest.approx(20.0)


def test_mosaic_overlay_create_and_edit(tmp_path: Path):
    edl_path = _edl(tmp_path)
    c = _client(edl_path)
    r = c.post("/api/overlay", json={"kind": "mosaic", "start": 5.0, "end": 7.0,
                                     "x": 0.2, "y": 0.3, "w": 0.4, "h": 0.25,
                                     "mosaic_type": "gaussian", "shape": "ellipse",
                                     "strength": 22})
    assert r.status_code == 200
    o = load_edl(edl_path).overlays[0]
    assert o.kind == "mosaic" and o.mosaic_type == "gaussian" and o.shape == "ellipse"
    assert o.w == pytest.approx(0.4) and o.h == pytest.approx(0.25) and o.strength == 22
    # timeline に mosaic 情報が出る
    t = [x for x in c.get("/api/timeline").json()["overlays"] if x["kind"] == "mosaic"][0]
    assert t["shape"] == "ellipse" and t["mosaic_type"] == "gaussian"
    # 編集: 矩形pixelateへ変更・強さ更新
    c.post("/api/overlay/0", json={"mosaic_type": "pixelate", "shape": "rect",
                                   "strength": 30, "w": 0.5})
    o = load_edl(edl_path).overlays[0]
    assert o.mosaic_type == "pixelate" and o.shape == "rect"
    assert o.strength == 30 and o.w == pytest.approx(0.5)


def test_mosaic_invalid_enums_fall_back(tmp_path: Path):
    edl_path = _edl(tmp_path)
    c = _client(edl_path)
    c.post("/api/overlay", json={"kind": "mosaic", "start": 5.0, "end": 7.0,
                                 "mosaic_type": "zzz", "shape": "star"})
    o = load_edl(edl_path).overlays[0]
    assert o.mosaic_type == "pixelate" and o.shape == "rect"


def test_timeline_exposes_path_for_copy_paste(tmp_path: Path):
    """コピー&ペーストで画像を複製できるよう timeline が path を返す。"""
    edl_path = _edl(tmp_path)
    c = _client(edl_path)
    up = c.post("/api/overlay/upload", files={"file": ("pic.png", _PNG, "image/png")}).json()
    c.post("/api/overlay", json={"kind": "image", "start": 5.0, "end": 7.0, "path": up["path"]})
    t = c.get("/api/timeline").json()["overlays"][0]
    assert t["path"] == up["path"] and t["name"] == "pic.png"


def test_paste_creates_independent_copy_with_given_id(tmp_path: Path):
    """貼り付け＝同内容・別IDで新規作成（元を消してもコピーは残る）。"""
    edl_path = _edl(tmp_path)
    c = _client(edl_path)
    c.post("/api/overlay", json={"kind": "mosaic", "start": 5.0, "end": 8.0,
                                 "w": 0.4, "h": 0.3, "shape": "ellipse",
                                 "mosaic_type": "gaussian", "strength": 25, "id": "src00001"})
    src = load_edl(edl_path).overlays[0]
    # クライアントの貼り付け相当: 同フィールド・別ID・再生ヘッド位置へ
    c.post("/api/overlay", json={"kind": src.kind, "id": "pasted01", "start": 12.0, "end": 15.0,
                                 "x": src.x, "y": src.y, "w": src.w, "h": src.h,
                                 "shape": src.shape, "mosaic_type": src.mosaic_type,
                                 "strength": src.strength})
    ovs = load_edl(edl_path).overlays
    assert [o.id for o in ovs] == ["src00001", "pasted01"]
    p = ovs[1]
    assert (p.w, p.h, p.shape, p.mosaic_type, p.strength) == \
           (src.w, src.h, src.shape, src.mosaic_type, src.strength)
    assert p.start == 12.0 and p.end == 15.0        # 位置だけ再生ヘッドへ
    # 元を削除してもコピーは独立して残る
    c.delete("/api/overlay/0")
    assert [o.id for o in load_edl(edl_path).overlays] == ["pasted01"]
