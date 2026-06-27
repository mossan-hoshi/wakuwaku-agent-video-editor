"""bgm.select（whitelist化・決定的選曲）のテスト。"""

from __future__ import annotations

from wwedit.bgm.select import (
    canonical_stem,
    is_valid_bgm,
    list_bgms,
    order_bgms,
    pick_bgm,
)


def test_canonical_stem_strips_variant():
    assert canonical_stem("Cotton Candy Skies (2).mp3") == "Cotton Candy Skies"
    assert canonical_stem("Bubblegum Skies.mp3") == "Bubblegum Skies"


def test_is_valid_bgm_rejects_junk():
    assert is_valid_bgm("x.mp3") and is_valid_bgm("x.wav")
    assert not is_valid_bgm("AlbumArtSmall.jpg")
    assert not is_valid_bgm("Folder.jpg")
    assert not is_valid_bgm("desktop.ini")


def test_list_bgms_dedups_and_filters(tmp_path):
    for n in [
        "Bubblegum Skies.mp3",
        "Bubblegum Skies (1).mp3",  # 重複 → 統合
        "Cotton Candy Skies (2).mp3",  # 素の名前は無い → これが残る
        "AlbumArtSmall.jpg",  # ゴミ
        "desktop.ini",  # ゴミ
    ]:
        (tmp_path / n).write_bytes(b"x")
    got = sorted(p.name for p in list_bgms(tmp_path))
    assert got == ["Bubblegum Skies.mp3", "Cotton Candy Skies (2).mp3"]


def test_list_bgms_missing_dir():
    assert list_bgms("/no/such/dir") == []


def test_pick_bgm_deterministic(tmp_path):
    files = []
    for n in ["a.mp3", "b.mp3", "c.mp3"]:
        p = tmp_path / n
        p.write_bytes(b"x")
        files.append(p)
    bgms = list_bgms(tmp_path)
    # 同じkeyは同じ曲、別keyで分散しうる
    assert pick_bgm(bgms, "section0") == pick_bgm(bgms, "section0")
    assert pick_bgm([], "x") is None


def test_order_bgms_shuffle(tmp_path):
    for n in [f"{c}.mp3" for c in "abcdefgh"]:
        (tmp_path / n).write_bytes(b"x")
    bgms = list_bgms(tmp_path)  # 正規名順 a..h
    ordered = order_bgms(bgms, "rec-2026-06-04")
    # 全曲を1回ずつ含む（連続再生用・欠落/重複なし）
    assert sorted(p.name for p in ordered) == [p.name for p in bgms]
    # 決定的：同じkeyは常に同じ並び（再レンダリングで再現）
    assert order_bgms(bgms, "rec-2026-06-04") == ordered
    # ランダム：名前順そのままではない & 別keyで並びが変わりうる
    assert ordered != bgms
    assert order_bgms(bgms, "rec-other") != ordered
    assert order_bgms([], "x") == []
