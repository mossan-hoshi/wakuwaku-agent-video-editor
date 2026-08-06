"""Recut が書き出した .fcpxml の読み込み。

構造（実データ確認済み）:
- ``<sequence>/<spine>`` 直下に同一ソース動画 ``r1`` の ``<asset-clip>`` が連続。
- 各クリップ属性: ``start``(ソースIN点) / ``duration`` / ``offset``(出力位置)。すべて ``N/25s``。
- ソースは30fps系だが start/duration も25fps基底で表現される。秒に直せば一貫するので
  ``common.timecode.parse_rational`` で秒へ変換して扱う。
- クリップ内にネストされた ``<asset-clip lane="-2/-3">`` は話者別音声（ここでは無視）。

**残す区間 = 各ビデオクリップの [start, start+duration]（ソース秒）。**
**除去された無音 = 隣接する残す区間どうしの隙間。**
crop/transform 等のフレーミング情報は fcpxml に存在しない。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from urllib.parse import quote

from wwedit.common.timecode import format_rational, parse_rational
from wwedit.edl.schema import Edl, Segment

__all__ = [
    "KeepRange",
    "read_keep_ranges",
    "keep_ranges_to_segments",
    "write_fcpxml",
]


@dataclass
class KeepRange:
    """ソースタイムライン上で残す区間（秒）。"""

    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def _video_clips(spine: ET.Element) -> list[ET.Element]:
    """spine 直下のビデオ asset-clip（lane を持たない .mp4 クリップ）。"""
    clips = []
    for child in spine:
        if child.tag != "asset-clip":
            continue
        if child.get("lane"):  # ネストされた音声はトップレベルには来ないが念のため
            continue
        name = child.get("name", "")
        if name.lower().endswith(".mp4"):
            clips.append(child)
    return clips


def read_keep_ranges(fcpxml_path: str | Path) -> list[KeepRange]:
    """.fcpxml から残す区間（ソース秒）を昇順で返す。"""
    tree = ET.parse(fcpxml_path)
    root = tree.getroot()
    spine = root.find(".//sequence/spine")
    if spine is None:
        raise ValueError(f"spine が見つからない: {fcpxml_path}")

    ranges: list[KeepRange] = []
    for clip in _video_clips(spine):
        start = float(parse_rational(clip.get("start", "0s")))
        dur = float(parse_rational(clip.get("duration", "0s")))
        if dur <= 0:
            continue
        ranges.append(KeepRange(start=start, end=start + dur))

    ranges.sort(key=lambda r: r.start)
    return ranges


def keep_ranges_to_segments(
    ranges: list[KeepRange], source_duration_s: float | None = None
) -> list[Segment]:
    """残す区間列を、無音(invalid)を挟んだ連続 Segment 列に変換する。

    隣接する残す区間どうしの隙間を ``reason="silence"`` の無効区間として補完する。
    """
    segments: list[Segment] = []
    idx = 0
    prev_end = 0.0
    for r in ranges:
        # 直前との隙間 = 除去された無音
        if r.start > prev_end + 1e-6:
            segments.append(
                Segment(
                    id=f"seg{idx:04d}",
                    start=prev_end,
                    end=r.start,
                    invalid=True,
                    reason="silence",
                )
            )
            idx += 1
        segments.append(Segment(id=f"seg{idx:04d}", start=r.start, end=r.end, invalid=False))
        idx += 1
        prev_end = r.end

    # 末尾の無音（ソース尺が分かる場合のみ）
    if source_duration_s is not None and source_duration_s > prev_end + 1e-6:
        segments.append(
            Segment(
                id=f"seg{idx:04d}",
                start=prev_end,
                end=source_duration_s,
                invalid=True,
                reason="silence",
            )
        )
    return segments


def _file_url(path: str) -> str:
    """ローカルパスを fcpxml の src 形式 (file://localhost/...) へ。"""
    p = str(path).replace("\\", "/")
    return "file://localhost/" + quote(p, safe="/:")


def _src_to_out(kept: list, t: float) -> float:
    """ソース秒 t を keep連結後の出力秒へ（カット内なら次keep先頭へスナップ）。"""
    acc = 0.0
    for r in kept:
        if t < r.start:
            return acc
        if t <= r.end:
            return acc + (t - r.start)
        acc += r.duration
    return acc


def _framing_at(edl: Edl, t: float):
    """ソース秒 t を含む framing 区間を返す（半開[start,end)・無ければ None）。"""
    for r in edl.framing or []:
        if r.start - 1e-6 <= t < r.end - 1e-6:
            return r
    return None


def _crop_transform(bbox, w: int, h: int) -> tuple[str, str] | None:
    """crop bbox(px x,y,w,h) を fcpxml adjust-transform の (scale, position) へ。

    bbox 部分矩形を全画面に充填する＝中心を合わせて scale=W/bw 拡大。
    座標系は FCPX 準拠＝position は出力px・原点中央・+X右/+Y上（画像yは下向きなので反転）。
    全画面（crop無し）相当なら None（変換を書かない）。
    """
    bx, by, bw, bh = bbox
    if bw <= 0 or bh <= 0:
        return None
    s = w / bw  # 16:9維持なので h/bh と一致
    if s <= 1.0 + 1e-3:  # 実質フルフレーム＝crop無し
        return None
    cx = bx + bw / 2
    cy = by + bh / 2
    pos_x = -s * (cx - w / 2)
    pos_y = s * (cy - h / 2)  # 画面+Yは上、画像+yは下＝符号反転
    return f"{s:.5f} {s:.5f}", f"{pos_x:.3f} {pos_y:.3f}"


def _ass_bgr_to_rgba(ass_color: str) -> str:
    """ASS の ``&HAABBGGRR`` 色を fcpxml の ``R G B A``(0-1 float) へ。失敗時は白。"""
    s = ass_color.strip().lstrip("&Hh").strip()
    try:
        v = int(s, 16)
    except ValueError:
        return "1 1 1 1"
    bb = (v >> 16) & 0xFF
    gg = (v >> 8) & 0xFF
    rr = v & 0xFF
    return f"{rr / 255:.3f} {gg / 255:.3f} {bb / 255:.3f} 1"


def write_fcpxml(edl: Edl, out_path: str | Path) -> None:
    """EDL の残す区間を Recut 形式の .fcpxml（カットタイムライン）に書き出す。

    用途は **全編集情報の書き出し（記録・相互運用、および緊急時のみ DaVinci で開く）**。
    手修正の主舞台は自前Webアプリ側で、fcpxml は常用の編集UIではない。
    カット（残す区間）＋話者別音声＋字幕(title・話者色・出力時刻再マップ)
    ＋フレーミング(crop=adjust-transform)＋BGM(音楽レーン・adjust-volume)を出力する。
    ※話者音声の loudnorm は適応的(レンダ時測定)で静的fcpxml値にできないため、話者クリップは
    素レベルのまま（BGM の相対音量のみ adjust-volume で表現）。
    """
    fps = edl.source.fps or 25
    src = edl.source
    speakers = [t for t in src.audio_tracks if not t.is_desktop_audio]

    def tc(seconds: float) -> str:
        return format_rational(Fraction(seconds).limit_denominator(10**6), fps)

    root = ET.Element("fcpxml", version="1.8")
    resources = ET.SubElement(root, "resources")
    ET.SubElement(
        resources,
        "format",
        id="r1",
        frameDuration=f"1/{fps}s",
        width=str(src.width),
        height=str(src.height),
    )
    ET.SubElement(
        resources,
        "asset",
        id="r2",
        name=Path(src.video_path).name,
        src=_file_url(src.video_path),
        start="0/1s",
        duration=tc(src.duration_s),
        format="r1",
        hasAudio="1",
        hasVideo="1",
        audioSources="1",
        audioChannels="2",
        audioRate="48k",
    )
    speaker_refs: list[str] = []
    for i, sp in enumerate(speakers):
        ref = f"r{3 + i}"
        speaker_refs.append(ref)
        ET.SubElement(
            resources,
            "asset",
            id=ref,
            name=Path(sp.path).name,
            src=_file_url(sp.path),
            start="0/1s",
            duration=tc(src.duration_s),
            format="r1",
            hasAudio="1",
            hasVideo="0",
            audioSources="1",
            audioChannels="2",
            audioRate="48k",
        )

    # 字幕用の title エフェクト資源（Basic Title）。字幕があるときだけ追加。
    title_ref: str | None = None
    color_map: dict[str, str] = {}
    if edl.subtitles:
        from wwedit.subtitle.ass import assign_speaker_colors, resolve_color_key

        title_ref = f"r{3 + len(speakers)}"
        _title_uid = (
            ".../Titles.localized/Build In:Out.localized/"
            "Basic Title.localized/Basic Title.moti"
        )
        ET.SubElement(resources, "effect", id=title_ref, name="Basic Title", uid=_title_uid)
        spk = [s.speaker for s in edl.subtitles if s.speaker]
        color_map = assign_speaker_colors(spk, edl.recording_dir or "main")
        for sp, key in (edl.subtitle_speaker_colors or {}).items():
            c = resolve_color_key(key)
            if c:
                color_map[sp] = c

    # BGM 音声資源（重複pathは1資源・hasVideo=0）。EDL.bgm が空なら追加しない。
    bgm_total = sum(r.duration for r in edl.kept_ranges())
    bgm_ref: dict[str, str] = {}
    if edl.bgm:
        nid = 3 + len(speakers) + (1 if title_ref else 0)
        for cue in edl.bgm:
            if cue.path in bgm_ref:
                continue
            ref = f"r{nid}"
            nid += 1
            bgm_ref[cue.path] = ref
            ET.SubElement(
                resources, "asset", id=ref, name=Path(cue.path).name,
                src=_file_url(cue.path), start="0/1s", duration=tc(max(bgm_total, 1.0)),
                format="r1", hasAudio="1", hasVideo="0", audioSources="1",
                audioChannels="2", audioRate="48k",
            )

    library = ET.SubElement(root, "library")
    event = ET.SubElement(library, "event", name=Path(out_path).stem)
    project = ET.SubElement(event, "project", name=Path(out_path).stem)

    kept = edl.kept_ranges()
    total = sum(r.duration for r in kept)
    sequence = ET.SubElement(
        project, "sequence", duration=tc(total), format="r1", tcStart="0/1s", audioLayout="stereo"
    )
    spine = ET.SubElement(sequence, "spine")

    out_pos = 0.0
    for r in kept:
        clip = ET.SubElement(
            spine,
            "asset-clip",
            name=Path(src.video_path).name,
            offset=tc(out_pos),
            ref="r2",
            start=tc(r.start),
            duration=tc(r.duration),
            audioRole="dialogue",
            format="r1",
        )
        # フレーミング(crop): クリップ中点を含む static 区間の bbox を adjust-transform で表現。
        # 単一変換(キーフレーム無)＝1クリップ1フレーミング前提（stable区間は粗く大半が単一）。
        fr = _framing_at(edl, (r.start + r.end) / 2)
        if fr is not None and fr.kind == "static" and fr.bbox:
            xf = _crop_transform(fr.bbox, src.width, src.height)
            if xf is not None:
                ET.SubElement(clip, "adjust-transform", scale=xf[0], position=xf[1])
        for j, ref in enumerate(speaker_refs):
            ET.SubElement(
                clip,
                "asset-clip",
                name=Path(speakers[j].path).name,
                offset=tc(out_pos),
                ref=ref,
                start=tc(r.start),
                duration=tc(r.duration),
                audioRole="dialogue",
                lane=str(-(2 + j)),
            )
        out_pos += r.duration

    # 字幕を title クリップとして lane=1 に重ねる（ソース時刻→出力時刻へ再マップ）。
    if title_ref:
        ti = 0
        for sub in sorted(edl.subtitles, key=lambda s: s.start):
            o_start = _src_to_out(kept, sub.start)
            o_end = _src_to_out(kept, sub.end)
            if o_end <= o_start:
                continue
            color = color_map.get(sub.speaker) if sub.speaker else None
            font_color = _ass_bgr_to_rgba(color) if color else "1 1 1 1"
            title = ET.SubElement(
                spine,
                "title",
                name=(sub.text[:40] or "字幕"),
                offset=tc(o_start),
                ref=title_ref,
                duration=tc(o_end - o_start),
                lane="1",
            )
            ts_id = f"ts{ti}"
            text_el = ET.SubElement(title, "text")
            style_el = ET.SubElement(text_el, "text-style", ref=ts_id)
            style_el.text = sub.text
            tsd = ET.SubElement(title, "text-style-def", id=ts_id)
            ET.SubElement(
                tsd,
                "text-style",
                font="Meiryo",
                fontSize="72",
                fontColor=font_color,
                bold="1",
                alignment="center",
            )
            ti += 1

    # BGM を音楽レーン（話者より下のlane）に音声クリップとして敷く。
    # BgmCue.start/end は**出力(最終)タイムライン秒**として解釈（BGMは最終尺に重なる）。
    # 音量は cue.gain_db を adjust-volume で表現（compose の target-LUFS ダッキングの静的近似）。
    if bgm_ref:
        bgm_lane = -(2 + len(speakers))
        for cue in edl.bgm:
            o0 = max(0.0, cue.start)
            o1 = min(cue.end, bgm_total) if bgm_total else cue.end
            if o1 <= o0:
                continue
            bclip = ET.SubElement(
                spine, "asset-clip", name=Path(cue.path).name,
                offset=tc(o0), ref=bgm_ref[cue.path], start="0/1s",
                duration=tc(o1 - o0), audioRole="music", lane=str(bgm_lane),
            )
            ET.SubElement(bclip, "adjust-volume", amount=f"{cue.gain_db:g}dB")

    ET.indent(root, space="  ")
    xml = ET.tostring(root, encoding="unicode")
    Path(out_path).write_text('<?xml version="1.0" encoding="UTF-8"?>\n' + xml, encoding="utf-8")
