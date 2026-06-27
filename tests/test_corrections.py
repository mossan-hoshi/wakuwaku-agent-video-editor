import json
from pathlib import Path

from wwedit.edl.schema import Edl, FramingRegion, SourceMedia
from wwedit.framing.corrections import (
    build_correction_items,
    parse_touched_intervals,
    px_to_norm_box,
)
from wwedit.framing.croptrain import load_crop_items_multi


def test_parse_touched_intervals():
    lines = [
        json.dumps({"type": "framing_edit", "idx": 0,
                    "before": {"start": 0, "end": 10, "bbox": None},
                    "after": {"start": 0, "end": 10, "bbox": [10, 10, 100, 100]}}),
        # bbox 不変（範囲だけ）は無視
        json.dumps({"type": "framing_edit", "idx": 1,
                    "before": {"start": 10, "end": 20, "bbox": [1, 2, 3, 4]},
                    "after": {"start": 10, "end": 22, "bbox": [1, 2, 3, 4]}}),
        # クリア（no_crop）
        json.dumps({"type": "framing_edit", "idx": 2,
                    "before": {"start": 20, "end": 30, "bbox": [1, 2, 3, 4]},
                    "after": {"start": 20, "end": 30, "bbox": None}}),
        json.dumps({"type": "segment_edit", "id": "x"}),  # 別種は無視
        "",  # 空行
    ]
    iv = parse_touched_intervals(lines)
    assert len(iv) == 2
    assert iv[0] == {"start": 0.0, "end": 10.0, "has_crop": True}
    assert iv[1] == {"start": 20.0, "end": 30.0, "has_crop": False}


def test_px_to_norm_box():
    nb = px_to_norm_box([480, 270, 960, 540], 1920, 1080)
    assert nb == [0.25, 0.25, 0.75, 0.75]
    # クランプ（はみ出し）
    nb2 = px_to_norm_box([-10, -10, 2000, 1200], 1920, 1080)
    assert nb2 == [0.0, 0.0, 1.0, 1.0]


def _edl():
    return Edl(
        recording_dir="rec",
        source=SourceMedia(video_path="v.mp4", fps=30, width=1920, height=1080, duration_s=100.0),
        framing=[
            FramingRegion(start=0, end=10, kind="static", bbox=(480, 270, 960, 540)),   # crop触
            FramingRegion(start=10, end=20, kind="static", bbox=(0, 0, 1920, 1080)),    # 触but全
            FramingRegion(start=20, end=30, kind="static", bbox=None),                  # no_crop触
            FramingRegion(start=30, end=40, kind="static", bbox=(100, 100, 800, 450)),  # 未触=除外
            FramingRegion(start=40, end=50, kind="pending", bbox=(1, 1, 2, 2)),         # 非static
        ],
    )


def test_build_correction_items_filters_by_touch():
    edl = _edl()
    intervals = [
        {"start": 0, "end": 10, "has_crop": True},
        {"start": 10, "end": 20, "has_crop": True},   # フル → スキップ
        {"start": 20, "end": 30, "has_crop": False},  # no_crop
    ]
    items = build_correction_items(edl, intervals, group="corr:t", id_prefix="c")
    # crop区間(0-10)のみ crop項目、(20-30)は no_crop、未touch(30-40)とフル(10-20)は除外
    crops = [it for it in items if not it["no_crop"]]
    ncs = [it for it in items if it["no_crop"]]
    assert len(crops) == 1
    assert crops[0]["bbox"] == [0.25, 0.25, 0.75, 0.75]
    assert crops[0]["timeline"] == "corr:t"
    assert len(ncs) == 1 and ncs[0]["bbox"] == [0.0, 0.0, 1.0, 1.0]


def test_build_correction_items_trust_final():
    edl = _edl()
    # log 無し（intervals空）でも trust_final なら全 static crop を採用、フルフレームは除外
    items = build_correction_items(edl, [], group="g", id_prefix="c", trust_final=True)
    crops = [it for it in items if not it["no_crop"]]
    # static crop = (0-10)=(480,270,960,540) と (30-40)=(100,100,800,450)、(10-20)はフル除外
    assert len(crops) == 2
    assert all(not it["no_crop"] for it in items)  # no_crop は touch必須＝今回0


def test_load_crop_items_multi(tmp_path: Path):
    def mkroot(name, items):
        r = tmp_path / name
        (r / "frames").mkdir(parents=True)
        for it in items:
            (r / it["image"]).write_bytes(b"x")  # 実在チェック用ダミー
        (r / "dataset.json").write_text(json.dumps(items), encoding="utf-8")
        return r

    a = mkroot("a", [
        {"id": "a1", "image": "frames/a1.png", "timeline": "T1",
         "bbox": [0.1, 0.1, 0.5, 0.5], "corrected": True, "rejected": False, "no_crop": False},
    ])
    b = mkroot("b", [
        {"id": "b1", "image": "frames/b1.png", "timeline": "corr:x",
         "bbox": [0.2, 0.2, 0.6, 0.6], "corrected": True, "rejected": False, "no_crop": False},
        {"id": "b2", "image": "frames/b2.png", "timeline": "corr:x",
         "bbox": [0, 0, 1, 1], "corrected": True, "rejected": False, "no_crop": True},  # 除外
    ])
    items = load_crop_items_multi([a, b])
    ids = {it["id"] for it in items}
    assert ids == {"a1", "b1"}  # no_crop(b2)は除外
    # image は絶対パスへ書き換わり、任意rootで解決可能
    for it in items:
        assert Path(it["image"]).is_absolute() and Path(it["image"]).exists()
