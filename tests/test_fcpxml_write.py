from pathlib import Path

import pytest

from wwedit.compose.fcpxml import _crop_transform, read_keep_ranges, write_fcpxml
from wwedit.edl.schema import (
    BgmCue,
    Edl,
    FramingRegion,
    Segment,
    SourceMedia,
    SpeakerTrack,
    Subtitle,
)


def _edl(tmp_path: Path) -> Edl:
    # ダミーのメディアパス（書き出し/パースに実体は不要）
    return Edl(
        recording_dir=str(tmp_path),
        source=SourceMedia(
            video_path=str(tmp_path / "video1.mp4"),
            fps=25,
            width=1920,
            height=1080,
            duration_s=100.0,
            audio_tracks=[
                SpeakerTrack(speaker="mossan-hoshi", path=str(tmp_path / "Audio Record/a1.m4a")),
                SpeakerTrack(speaker="Taniguchi", path=str(tmp_path / "Audio Record/a2.m4a")),
            ],
        ),
        segments=[
            Segment(id="s0", start=4.0, end=8.0, invalid=False),
            Segment(id="s1", start=8.0, end=12.0, invalid=True, reason="silence"),
            Segment(id="s2", start=12.0, end=20.0, invalid=False),
        ],
    )


def test_write_read_roundtrip(tmp_path: Path):
    edl = _edl(tmp_path)
    out = tmp_path / "out.fcpxml"
    write_fcpxml(edl, out)

    ranges = read_keep_ranges(out)
    # 残す区間 [4,8] と [12,20] が round-trip する（25fps量子化）
    assert len(ranges) == 2
    assert ranges[0].start == pytest.approx(4.0, abs=0.04)
    assert ranges[0].end == pytest.approx(8.0, abs=0.04)
    assert ranges[1].start == pytest.approx(12.0, abs=0.04)
    assert ranges[1].end == pytest.approx(20.0, abs=0.04)


def test_write_has_speaker_lanes(tmp_path: Path):
    edl = _edl(tmp_path)
    out = tmp_path / "out.fcpxml"
    write_fcpxml(edl, out)
    xml = out.read_text(encoding="utf-8")
    assert 'lane="-2"' in xml and 'lane="-3"' in xml
    assert 'version="1.8"' in xml


def test_write_subtitles_as_titles(tmp_path: Path):
    import xml.etree.ElementTree as ET

    edl = _edl(tmp_path)
    # 注意書き(話者なし=白) と Taniguchi の字幕（kept [4,8],[12,20] 内）
    edl.subtitles = [
        Subtitle(start=4.5, end=7.0, text="【注意】AI字幕", style="main"),
        Subtitle(start=13.0, end=16.0, text="タニグチの発話", style="main", speaker="Taniguchi"),
    ]
    out = tmp_path / "out.fcpxml"
    write_fcpxml(edl, out)

    root = ET.parse(out).getroot()
    # title エフェクト資源 + 2本の title クリップ
    assert root.find(".//resources/effect[@name='Basic Title']") is not None
    titles = root.findall(".//spine/title")
    assert len(titles) == 2
    # 出力時刻へ再マップ: 1本目 source4.5→out0.5、2本目 source13→out5.0
    starts = sorted(float(t.get("offset").rstrip("s").split("/")[0]) /
                    float(t.get("offset").rstrip("s").split("/")[1]) for t in titles)
    assert starts[0] == pytest.approx(0.5, abs=0.04)
    assert starts[1] == pytest.approx(5.0, abs=0.04)
    # テキストと色: 話者なしは白、Taniguchi(暖色)は白以外
    texts = {t.findtext(".//text-style"): t for t in titles}
    assert "【注意】AI字幕" in texts and "タニグチの発話" in texts
    disc_color = texts["【注意】AI字幕"].find(".//text-style-def/text-style").get("fontColor")
    tani_color = texts["タニグチの発話"].find(".//text-style-def/text-style").get("fontColor")
    assert disc_color == "1 1 1 1"
    assert tani_color != "1 1 1 1"


def test_crop_transform_math():
    # 中央寄せ・半分サイズの crop → scale=2、中心一致なので position=0
    xf = _crop_transform((480, 270, 960, 540), 1920, 1080)
    assert xf is not None
    sx = float(xf[0].split()[0])
    px, py = (float(v) for v in xf[1].split())
    assert sx == pytest.approx(2.0, abs=1e-3)
    assert px == pytest.approx(0.0, abs=1e-2)
    assert py == pytest.approx(0.0, abs=1e-2)
    # 左上の crop = 内容が左上 → 中央へ寄せるので右(+X)かつ下(-Y)へ移動
    xf2 = _crop_transform((0, 0, 960, 540), 1920, 1080)
    px2, py2 = (float(v) for v in xf2[1].split())
    assert px2 > 0 and py2 < 0
    # フルフレーム相当は変換なし
    assert _crop_transform((0, 0, 1920, 1080), 1920, 1080) is None


def test_write_framing_as_transform(tmp_path: Path):
    import xml.etree.ElementTree as ET

    edl = _edl(tmp_path)
    # kept [4,8] を覆う static crop と、[12,20] は全画面(変換なし)
    edl.framing = [
        FramingRegion(start=0.0, end=10.0, kind="static", bbox=(480, 270, 960, 540)),
        FramingRegion(start=10.0, end=25.0, kind="static", bbox=(0, 0, 1920, 1080)),
    ]
    out = tmp_path / "out.fcpxml"
    write_fcpxml(edl, out)
    root = ET.parse(out).getroot()
    clips = root.findall(".//spine/asset-clip")
    xforms = [c.find("adjust-transform") for c in clips]
    # 1本目クリップだけ変換が付き、2本目（全画面）は付かない
    assert xforms[0] is not None
    assert xforms[1] is None
    assert float(xforms[0].get("scale").split()[0]) == pytest.approx(2.0, abs=1e-3)


def test_write_bgm_as_music_lane(tmp_path: Path):
    import xml.etree.ElementTree as ET

    edl = _edl(tmp_path)
    # kept は [4,8]+[12,20]=12s。出力時刻でBGMを2キュー（同一ファイルは1資源に集約）
    edl.bgm = [
        BgmCue(start=0.0, end=6.0, path=str(tmp_path / "bgm/cafe1.mp3"), gain_db=-22.0),
        BgmCue(start=6.0, end=30.0, path=str(tmp_path / "bgm/cafe1.mp3"), gain_db=-22.0),
    ]
    out = tmp_path / "out.fcpxml"
    write_fcpxml(edl, out)
    root = ET.parse(out).getroot()
    # 同一pathは1資源、hasVideo=0
    music_assets = [a for a in root.findall(".//resources/asset") if a.get("hasVideo") == "0"
                    and a.get("name") == "cafe1.mp3"]
    assert len(music_assets) == 1
    # 音楽レーンのクリップ2本＋音量
    bclips = [c for c in root.findall(".//spine/asset-clip")
              if c.get("audioRole") == "music"]
    assert len(bclips) == 2
    assert bclips[0].find("adjust-volume").get("amount") == "-22dB"
    # 2本目はkept合計12sでクランプされ end=12
    durs = [float(c.get("duration").rstrip("s").split("/")[0]) /
            float(c.get("duration").rstrip("s").split("/")[1]) for c in bclips]
    assert durs[0] == pytest.approx(6.0, abs=0.04)
    assert durs[1] == pytest.approx(6.0, abs=0.04)  # [6,12) にクランプ
