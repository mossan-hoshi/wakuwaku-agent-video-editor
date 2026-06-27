"""自前の動画エディタ（手修正の主舞台）の FastAPI バックエンド — 第一スライス: 字幕。

手修正は DaVinci ではなく**このローカルWebアプリ**で行う方針。SSOTである EDL を直接
編集し（非破壊）、各修正を ``correction_log.jsonl`` に追記して将来のカスタムモデル学習
データに使う（[[nondestructive-overlay-compose]] のEDL=SSOT、M5学習ループ）。

このスライスでできること:
- 字幕一覧の取得（ソース時刻＋カット後の出力時刻＋話者色つき）。
- 字幕の本文/話者/スタイル/時刻の修正。
- 話者ごとの字幕色の上書き（red/purple/blue/green、auto で既定へ戻す）。
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

__all__ = ["create_editor_app", "SubtitleEdit", "SpeakerColor"]


class SubtitleEdit(BaseModel):
    text: str | None = None
    speaker: str | None = None
    style: str | None = None  # main | intro
    start: float | None = None  # ソース秒
    end: float | None = None


class SpeakerColor(BaseModel):
    speaker: str
    color: str  # red|purple|blue|green|auto


class SegmentEdit(BaseModel):
    invalid: bool | None = None  # True=カット / False=復活 / None=変更なし
    reason: str = "manual"
    start: float | None = None  # 境界移動（隣接区間も追従）
    end: float | None = None


class SplitAt(BaseModel):
    at: float  # この時刻で跨ぐ区間を2分割


class CutRange(BaseModel):
    start: float
    end: float  # [start,end] を非破壊カット（invalid化）。端が区間途中なら分割して範囲を正確化


class FramingEdit(BaseModel):
    start: float | None = None
    end: float | None = None
    kind: str | None = None  # static | loading | pending
    loading_label: str | None = None
    warning: str | None = None
    # crop枠 bbox = (x, y, w, h) ピクセル。専用クロップモデルの書き戻し or ソース映像上での手調整。
    bbox: list[int] | None = None
    clear_crop: bool = False  # True=全画面(no_crop)へ戻す（bbox=None）


class ChapterEdit(BaseModel):
    start_at: float | None = None
    chapter_title: str | None = None
    section_title: str | None = None
    is_required: bool | None = None


class SubtitleNew(BaseModel):
    start: float
    end: float
    text: str = "新規字幕"
    speaker: str | None = None
    style: str = "main"


class ChapterNew(BaseModel):
    start_at: float
    chapter_title: str = "新規章"
    section_title: str = ""


class PostUnitEdit(BaseModel):
    title: str | None = None


class MergeDir(BaseModel):
    dir: int = 1  # -1=前(左)の隣接と結合 / 1=次(右)の隣接と結合


def _src_to_out(ranges: list, t: float) -> float:
    """ソース秒 t を keep連結後の出力秒へ（カット内なら次keep先頭へスナップ）。"""
    acc = 0.0
    for r in ranges:
        if t < r.start:
            return acc
        if t <= r.end:
            return acc + (t - r.start)
        acc += r.end - r.start
    return acc


def _ass_to_css(ass_color: str) -> str:
    """ASS ``&HAABBGGRR`` を CSS ``#RRGGBB`` へ（UI表示用）。失敗時は白。"""
    s = ass_color.strip().lstrip("&Hh").strip()
    try:
        v = int(s, 16)
    except ValueError:
        return "#ffffff"
    bb, gg, rr = (v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF
    return f"#{rr:02x}{gg:02x}{bb:02x}"


def create_editor_app(edl_path: str | Path, preview_path: str | Path | None = None):
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, HTMLResponse

    from wwedit.edl.schema import load_edl, save_edl
    from wwedit.subtitle.ass import MAIN_PALETTE, assign_speaker_colors

    edl_path = Path(edl_path)
    preview_path = Path(preview_path) if preview_path else None
    static_dir = Path(__file__).parent / "static"
    log_path = edl_path.parent / "correction_log.jsonl"

    app = FastAPI(title="wwedit editor")

    def _log(entry: dict) -> None:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _resolved_colors(edl) -> dict[str, str]:
        spk = [s.speaker for s in edl.subtitles if s.speaker]
        cmap = assign_speaker_colors(spk, edl.recording_dir or "main")
        for sp, key in (edl.subtitle_speaker_colors or {}).items():
            if key in MAIN_PALETTE:
                cmap[sp] = MAIN_PALETTE[key]
        return cmap

    def _sub_css(s, cmap) -> str:
        """字幕の本文色(CSS)。ass.py に合わせ intro=ピンク/話者色/既定=blue（白にしない）。"""
        from wwedit.subtitle.ass import INTRO_COLOR

        if s.style == "intro":
            color = INTRO_COLOR
        elif s.speaker and s.speaker in cmap:
            color = cmap[s.speaker]
        else:
            color = MAIN_PALETTE["blue"]
        return _ass_to_css(color)

    @app.get("/api/timeline")
    def get_timeline() -> dict:
        """タイムラインNLE 用の全データ（ソース時間軸）。"""
        edl = load_edl(edl_path)
        ranges = edl.kept_ranges()
        cmap = _resolved_colors(edl)
        subs = []
        for i, s in enumerate(sorted(edl.subtitles, key=lambda x: x.start)):
            subs.append({
                "idx": i, "source_start": s.start, "source_end": s.end,
                "out_start": _src_to_out(ranges, s.start), "out_end": _src_to_out(ranges, s.end),
                "text": s.text, "style": s.style, "speaker": s.speaker,
                "css_color": _sub_css(s, cmap),
            })
        return {
            "recording_dir": edl.recording_dir,
            "source": {
                "duration": edl.source.duration_s, "fps": edl.source.fps or 30,
                "width": edl.source.width, "height": edl.source.height,
                "url": "/media/source", "name": Path(edl.source.video_path).name,
            },
            "preview": {
                "url": "/media/preview",
                "available": bool(preview_path and preview_path.exists()),
            },
            "kept_ranges": [{"start": r.start, "end": r.end} for r in ranges],
            "segments": [
                {"id": sg.id, "start": sg.start, "end": sg.end, "invalid": sg.invalid,
                 "reason": sg.reason} for sg in edl.segments
            ],
            "framing": [
                {"start": f.start, "end": f.end, "kind": f.kind, "bbox": f.bbox,
                 "loading_label": f.loading_label, "warning": f.warning}
                for f in (edl.framing or [])
            ],
            "subtitles": subs,
            "subtitle_speaker_colors": edl.subtitle_speaker_colors or {},
            "chapters": [
                {"start_at": c.start_at, "chapter_title": c.chapter_title,
                 "section_title": c.section_title, "is_required": c.is_required}
                for c in (edl.chapters or [])
            ],
            "bgm": [{"start": b.start, "end": b.end, "path": Path(b.path).name if b.path else None}
                    for b in (edl.bgm or [])],
            "post_units": [
                {"id": p.id, "title": p.title,
                 "start": min((r.start for r in p.ranges), default=0.0),
                 "end": max((r.end for r in p.ranges), default=0.0),
                 "n_ranges": len(p.ranges), "chapter_ids": p.chapter_ids}
                for p in (edl.post_units or [])
            ],
        }

    @app.get("/api/transcript")
    def get_transcript() -> dict:
        """ライブ文字起こしテロップ用に、単語(ソース時刻)を時刻順で返す。"""
        edl = load_edl(edl_path)
        words = []
        for u in edl.utterances:
            for w in u.words:
                if w.text and w.text.strip():
                    words.append({"t": w.start, "text": w.text, "speaker": u.speaker})
        words.sort(key=lambda x: x["t"])
        return {"words": words}

    @app.get("/media/source")
    def media_source():
        edl = load_edl(edl_path)
        p = Path(edl.source.video_path)
        if not p.exists():
            raise HTTPException(404, "source 動画が見つからない")
        return FileResponse(str(p))  # Starlette FileResponse は Range 対応（スクラブ可）

    @app.get("/media/preview")
    def media_preview():
        if not (preview_path and preview_path.exists()):
            raise HTTPException(404, "レンダリング結果が指定されていない")
        return FileResponse(str(preview_path))

    @app.post("/api/segment/split")
    def split_segment(payload: SplitAt) -> dict:
        """``at`` を跨ぐ区間を、その時刻で連続する2区間に分割する（属性は継承）。"""
        from wwedit.edl.schema import Segment

        edl = load_edl(edl_path)
        at = float(payload.at)
        target = next((s for s in edl.segments if s.start < at < s.end), None)
        if target is None:
            raise HTTPException(400, "再生ヘッドが区間を跨いでいない（境界上/区間外）")
        ids = {s.id for s in edl.segments}
        nid = f"{target.id}_s"
        n = 2
        while nid in ids:
            nid = f"{target.id}_s{n}"
            n += 1
        right = Segment(id=nid, start=at, end=target.end, invalid=target.invalid,
                        reason=target.reason, note=target.note)
        i = edl.segments.index(target)
        target.end = at
        edl.segments.insert(i + 1, right)
        save_edl(edl, edl_path)
        _log({"type": "segment_split", "id": target.id, "at": at, "new_id": nid})
        return {"ok": True, "at": at, "new_id": nid}

    @app.post("/api/segment/cut-range")
    def cut_range(payload: CutRange) -> dict:
        """[start,end] を非破壊カット（invalid化）。端が区間途中なら分割して範囲に揃える。"""
        from wwedit.edl.schema import Segment

        edl = load_edl(edl_path)
        lo, hi = sorted((float(payload.start), float(payload.end)))
        if hi - lo < 0.05:
            raise HTTPException(400, "カット範囲が短すぎます（In/Out を確認）")

        def _split_at(at: float) -> None:
            target = next((s for s in edl.segments if s.start + 1e-6 < at < s.end - 1e-6), None)
            if target is None:
                return
            ids = {s.id for s in edl.segments}
            nid, n = f"{target.id}_s", 2
            while nid in ids:
                nid, n = f"{target.id}_s{n}", n + 1
            right = Segment(id=nid, start=at, end=target.end, invalid=target.invalid,
                            reason=target.reason, note=target.note)
            i = edl.segments.index(target)
            target.end = at
            edl.segments.insert(i + 1, right)

        _split_at(lo)
        _split_at(hi)
        n_cut = 0
        for s in edl.segments:
            if s.start >= lo - 1e-6 and s.end <= hi + 1e-6 and not s.invalid:
                s.invalid = True
                s.reason = "manual"
                n_cut += 1
        save_edl(edl, edl_path)
        _log({"type": "cut_range", "start": lo, "end": hi, "n_cut": n_cut})
        return {"ok": True, "start": lo, "end": hi, "n_cut": n_cut}

    @app.post("/api/framing/split")
    def split_framing(payload: SplitAt) -> dict:
        """``at`` を跨ぐ調整クリップ(framing)を、その時刻で2区間に分割する（種別/cropを継承）。"""
        from wwedit.edl.schema import FramingRegion

        edl = load_edl(edl_path)
        at = float(payload.at)
        target = next((r for r in (edl.framing or []) if r.start < at < r.end), None)
        if target is None:
            raise HTTPException(400, "再生ヘッドが調整クリップを跨いでいない（境界上/区間外）")
        right = FramingRegion(start=at, end=target.end, kind=target.kind, bbox=target.bbox,
                              loading_label=target.loading_label, warning=target.warning)
        i = edl.framing.index(target)
        target.end = at
        edl.framing.insert(i + 1, right)
        save_edl(edl, edl_path)
        new_idx = sorted(edl.framing, key=lambda r: r.start).index(right)
        _log({"type": "framing_split", "at": at, "new_idx": new_idx})
        return {"ok": True, "at": at, "idx": new_idx}

    @app.post("/api/segment/{seg_id}")
    def edit_segment(seg_id: str, payload: SegmentEdit) -> dict:
        """カット区間の 有効/無効 切替、または 境界(start/end)移動（隣接区間も追従）。"""
        edl = load_edl(edl_path)
        segs = sorted(edl.segments, key=lambda s: s.start)
        sg = next((s for s in segs if s.id == seg_id), None)
        if sg is None:
            raise HTTPException(404, f"segment {seg_id} not found")
        i = segs.index(sg)
        before = {"invalid": sg.invalid, "start": sg.start, "end": sg.end}
        if payload.invalid is not None:
            sg.invalid = payload.invalid
            if payload.invalid:
                sg.reason = payload.reason
        if payload.start is not None:
            lo = (segs[i - 1].start + 0.05) if i > 0 else 0.0
            sg.start = max(lo, min(sg.end - 0.05, float(payload.start)))
            if i > 0:
                segs[i - 1].end = sg.start  # 連続性維持
        if payload.end is not None:
            hi = (segs[i + 1].end - 0.05) if i + 1 < len(segs) else edl.source.duration_s
            sg.end = min(hi, max(sg.start + 0.05, float(payload.end)))
            if i + 1 < len(segs):
                segs[i + 1].start = sg.end
        save_edl(edl, edl_path)
        _log({"type": "segment_edit", "id": seg_id, "before": before,
              "after": {"invalid": sg.invalid, "start": sg.start, "end": sg.end}})
        return {"ok": True, "id": seg_id, "invalid": sg.invalid, "start": sg.start, "end": sg.end}

    @app.post("/api/subtitle")
    def add_subtitle(payload: SubtitleNew) -> dict:
        """再生ヘッド位置などに字幕を新規追加。追加後の(start順)idxを返す。"""
        from wwedit.edl.schema import Subtitle

        if payload.end <= payload.start:
            raise HTTPException(400, "end は start より後である必要があります")
        edl = load_edl(edl_path)
        edl.subtitles.append(Subtitle(start=payload.start, end=payload.end, text=payload.text,
                                      style=payload.style, speaker=payload.speaker))
        save_edl(edl, edl_path)
        order = sorted(edl.subtitles, key=lambda s: s.start)
        idx = next(i for i, s in enumerate(order)
                   if s.start == payload.start and s.text == payload.text)
        _log({"type": "subtitle_add", "idx": idx, "start": payload.start, "end": payload.end})
        return {"ok": True, "idx": idx}

    @app.post("/api/subtitle/{idx}/merge")
    def merge_subtitle(idx: int, payload: MergeDir) -> dict:
        """選択字幕を隣接（隙間なく接する）字幕と結合し、内容を隣接側に統一する。"""
        edl = load_edl(edl_path)
        subs = sorted(edl.subtitles, key=lambda s: s.start)
        if not (0 <= idx < len(subs)):
            raise HTTPException(404, f"subtitle {idx} not found")
        j = idx + (1 if payload.dir > 0 else -1)
        if not (0 <= j < len(subs)):
            raise HTTPException(400, "結合できる隣接字幕がありません")
        cur, nb = subs[idx], subs[j]
        touch = (abs(cur.start - nb.end) < 0.05 if payload.dir < 0
                 else abs(cur.end - nb.start) < 0.05)
        if not touch:
            raise HTTPException(400, "隣接字幕と隙間なく接していません")
        cur.start = min(cur.start, nb.start)
        cur.end = max(cur.end, nb.end)
        cur.text, cur.speaker, cur.style = nb.text, nb.speaker, nb.style  # 内容=隣接側
        edl.subtitles.remove(nb)
        save_edl(edl, edl_path)
        new_idx = sorted(edl.subtitles, key=lambda s: s.start).index(cur)
        _log({"type": "subtitle_merge", "idx": idx, "dir": payload.dir, "text": cur.text})
        return {"ok": True, "idx": new_idx}

    @app.post("/api/framing/{idx}/merge")
    def merge_framing(idx: int, payload: MergeDir) -> dict:
        """選択フレーミング区間を隣接区間と結合し、種別/crop等を隣接側に統一する。"""
        edl = load_edl(edl_path)
        regs = sorted(edl.framing or [], key=lambda r: r.start)
        if not (0 <= idx < len(regs)):
            raise HTTPException(404, f"framing {idx} not found")
        j = idx + (1 if payload.dir > 0 else -1)
        if not (0 <= j < len(regs)):
            raise HTTPException(400, "結合できる隣接区間がありません")
        cur, nb = regs[idx], regs[j]
        touch = (abs(cur.start - nb.end) < 0.05 if payload.dir < 0
                 else abs(cur.end - nb.start) < 0.05)
        if not touch:
            raise HTTPException(400, "隣接区間と隙間なく接していません")
        cur.start = min(cur.start, nb.start)
        cur.end = max(cur.end, nb.end)
        cur.kind, cur.bbox = nb.kind, nb.bbox  # 内容=隣接側
        cur.loading_label, cur.warning = nb.loading_label, nb.warning
        edl.framing.remove(nb)
        save_edl(edl, edl_path)
        new_idx = sorted(edl.framing, key=lambda r: r.start).index(cur)
        _log({"type": "framing_merge", "idx": idx, "dir": payload.dir, "kind": cur.kind})
        return {"ok": True, "idx": new_idx}

    @app.post("/api/chapter")
    def add_chapter(payload: ChapterNew) -> dict:
        """再生ヘッド位置などに章を新規追加。追加後の(start_at順)idxを返す。"""
        from wwedit.edl.schema import Chapter

        edl = load_edl(edl_path)
        edl.chapters.append(Chapter(start_at=max(0.0, payload.start_at),
                                    chapter_title=payload.chapter_title,
                                    section_title=payload.section_title))
        save_edl(edl, edl_path)
        order = sorted(edl.chapters, key=lambda c: c.start_at)
        idx = next(i for i, c in enumerate(order) if c.start_at == max(0.0, payload.start_at))
        _log({"type": "chapter_add", "idx": idx, "start_at": payload.start_at})
        return {"ok": True, "idx": idx}

    @app.post("/api/postunit/{idx}")
    def edit_postunit(idx: int, payload: PostUnitEdit) -> dict:
        """投稿単位（セクション）のタイトルを編集する。"""
        edl = load_edl(edl_path)
        pus = edl.post_units or []
        if not (0 <= idx < len(pus)):
            raise HTTPException(404, f"post_unit {idx} not found")
        p = pus[idx]
        before = p.title
        if payload.title is not None:
            p.title = payload.title
        save_edl(edl, edl_path)
        _log({"type": "postunit_edit", "idx": idx, "before": before, "after": p.title})
        return {"ok": True, "title": p.title}

    @app.post("/api/framing/{idx}")
    def edit_framing(idx: int, payload: FramingEdit) -> dict:
        """シーン(framing区間)の範囲/種別/ローディングラベル/警告/cropを編集する。"""
        edl = load_edl(edl_path)
        regs = sorted(edl.framing or [], key=lambda r: r.start)
        if not (0 <= idx < len(regs)):
            raise HTTPException(404, f"framing {idx} not found")
        f = regs[idx]
        before = {"start": f.start, "end": f.end, "kind": f.kind,
                  "loading_label": f.loading_label, "warning": f.warning, "bbox": f.bbox}
        if payload.kind in ("static", "loading", "pending"):
            f.kind = payload.kind
        if payload.start is not None:
            f.start = float(payload.start)
        if payload.end is not None:
            f.end = float(payload.end)
        if payload.loading_label is not None:
            f.loading_label = payload.loading_label or None
        if payload.warning is not None:
            f.warning = payload.warning
        if payload.clear_crop:
            f.bbox = None
        elif payload.bbox is not None:
            if len(payload.bbox) != 4:
                raise HTTPException(400, "bbox は (x, y, w, h) の4要素")
            f.bbox = tuple(int(v) for v in payload.bbox)
        if f.end <= f.start:
            raise HTTPException(400, "end は start より後である必要があります")
        save_edl(edl, edl_path)
        after = {"start": f.start, "end": f.end, "kind": f.kind,
                 "loading_label": f.loading_label, "warning": f.warning, "bbox": f.bbox}
        _log({"type": "framing_edit", "idx": idx, "before": before, "after": after})
        return {"ok": True, "before": before, "after": after}

    @app.post("/api/chapter/{idx}")
    def edit_chapter(idx: int, payload: ChapterEdit) -> dict:
        """チャプターの開始位置/タイトル/必須フラグを編集する。"""
        edl = load_edl(edl_path)
        chaps = sorted(edl.chapters or [], key=lambda c: c.start_at)
        if not (0 <= idx < len(chaps)):
            raise HTTPException(404, f"chapter {idx} not found")
        c = chaps[idx]
        before = {"start_at": c.start_at, "chapter_title": c.chapter_title,
                  "section_title": c.section_title, "is_required": c.is_required}
        if payload.start_at is not None:
            c.start_at = max(0.0, float(payload.start_at))
        if payload.chapter_title is not None:
            c.chapter_title = payload.chapter_title
        if payload.section_title is not None:
            c.section_title = payload.section_title
        if payload.is_required is not None:
            c.is_required = payload.is_required
        save_edl(edl, edl_path)
        after = {"start_at": c.start_at, "chapter_title": c.chapter_title,
                 "section_title": c.section_title, "is_required": c.is_required}
        _log({"type": "chapter_edit", "idx": idx, "before": before, "after": after})
        return {"ok": True, "before": before, "after": after}

    @app.get("/api/edl")
    def get_edl() -> dict:
        edl = load_edl(edl_path)
        ranges = edl.kept_ranges()
        cmap = _resolved_colors(edl)
        subs = []
        for i, s in enumerate(sorted(edl.subtitles, key=lambda x: x.start)):
            subs.append({
                "idx": i,
                "source_start": s.start,
                "source_end": s.end,
                "out_start": _src_to_out(ranges, s.start),
                "out_end": _src_to_out(ranges, s.end),
                "text": s.text,
                "style": s.style,
                "speaker": s.speaker,
                "css_color": _sub_css(s, cmap),
            })
        return {
            "recording_dir": edl.recording_dir,
            "subtitle_speaker_colors": edl.subtitle_speaker_colors or {},
            "subtitles": subs,
        }

    @app.post("/api/subtitle/{idx}")
    def edit_subtitle(idx: int, payload: SubtitleEdit) -> dict:
        edl = load_edl(edl_path)
        subs = sorted(edl.subtitles, key=lambda x: x.start)
        if not (0 <= idx < len(subs)):
            raise HTTPException(404, f"subtitle {idx} not found")
        s = subs[idx]
        before = {"text": s.text, "speaker": s.speaker, "style": s.style,
                  "start": s.start, "end": s.end}
        if payload.text is not None:
            s.text = payload.text
        if payload.speaker is not None:
            s.speaker = payload.speaker or None
        if payload.style in ("main", "intro"):
            s.style = payload.style
        if payload.start is not None:
            s.start = float(payload.start)
        if payload.end is not None:
            s.end = float(payload.end)
        if s.end <= s.start:
            raise HTTPException(400, "end は start より後である必要があります")
        save_edl(edl, edl_path)
        after = {"text": s.text, "speaker": s.speaker, "style": s.style,
                 "start": s.start, "end": s.end}
        _log({"type": "subtitle_edit", "idx": idx, "before": before, "after": after})
        return {"ok": True, "before": before, "after": after}

    @app.post("/api/speaker-color")
    def set_speaker_color(payload: SpeakerColor) -> dict:
        if payload.color not in (*MAIN_PALETTE.keys(), "auto"):
            raise HTTPException(400, f"未知の色: {payload.color}")
        edl = load_edl(edl_path)
        colors = dict(edl.subtitle_speaker_colors or {})
        before = colors.get(payload.speaker, "auto")
        if payload.color == "auto":
            colors.pop(payload.speaker, None)
        else:
            colors[payload.speaker] = payload.color
        edl.subtitle_speaker_colors = colors
        save_edl(edl, edl_path)
        _log({"type": "speaker_color", "speaker": payload.speaker,
              "before": before, "after": payload.color})
        return {"ok": True, "subtitle_speaker_colors": colors}

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (static_dir / "editor.html").read_text(encoding="utf-8")

    return app
