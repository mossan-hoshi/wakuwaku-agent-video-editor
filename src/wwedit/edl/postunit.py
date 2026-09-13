"""投稿単位(PostUnit)ごとの出力対象を解決する（1収録→複数投稿）。

各投稿は EDL.post_units[idx] に対応し、その**ソース区間 ∩ kept** を連結して1本の動画になる。
compose は本モジュールの区間を `ranges` として受け、字幕/フレーミング/BGMはそこから一貫導出される。
"""

from __future__ import annotations

from wwedit.edl.schema import Chapter, Edl, Segment, TimeRange


def _src_to_out(ranges: list[TimeRange], t: float, freezes=()) -> float:
    """ソース秒 t を、指定 ranges を連結した出力秒へ（カット内なら次区間先頭へスナップ）。

    ``freezes``: [V] 方式Bのフリーズフレーム。t より前の区間内フリーズ分だけ後ろへずれる
    （概要欄の章時刻がレンダ結果とずれないように）。
    """
    acc = 0.0
    for r in ranges:
        if t < r.start:
            break
        if t <= r.end:
            acc += t - r.start
            break
        acc += r.end - r.start
    for f in freezes or ():
        if f.at < t and any(r.start < f.at < r.end for r in ranges):
            acc += f.extra
    return acc


def post_unit_ranges(edl: Edl, idx: int) -> list[TimeRange]:
    """投稿単位 idx の出力対象区間 = kept ∩ 単位スパン[min start, max end)。

    post_units が無い/範囲未設定なら kept 全体（=従来の1本）を返す。
    """
    units = edl.post_units or []
    if idx >= len(units) or not units[idx].ranges:
        return edl.kept_ranges()
    unit = units[idx]
    lo = min(r.start for r in unit.ranges)
    hi = max(r.end for r in unit.ranges)
    out: list[TimeRange] = []
    for r in edl.kept_ranges():
        a, b = max(r.start, lo), min(r.end, hi)
        if b > a + 1e-9:
            out.append(TimeRange(start=a, end=b))
    return out


def live_chapters(ranges: list[TimeRange], chapters, freezes=()) -> list[tuple[float, Chapter]]:
    """章を ``ranges`` の出力秒へ写し、**実際に映る章だけ**を ``(出力秒, 章)`` で返す。

    カットで**尺ゼロに潰れた章は捨て**、その位置で実際に流れる後続の章を残す。先頭は 0 に寄せる。
    章リボン（`chapter_ribbon_intervals`）・アイキャッチ（`eyecatch_boundaries`）と同じ規則で、
    概要欄・図解もこれを通す。
    🚨 通さないと (1) 冒頭を切って1章目の開始が最初の kept より前に来ると 00:00 が重複する／
    (2) 中身を全部切った章が同じ時刻で並び、YouTube の章が**全部**表示されなくなる
    （2026-09-10: 後半を丸ごと切って5章が0秒になった）。
    """
    total = sum(r.end - r.start for r in ranges) + sum(
        f.extra for f in freezes if any(r.start < f.at < r.end for r in ranges))
    pts = [(min(max(_src_to_out(ranges, c.start_at, freezes), 0.0), total), c)
           for c in sorted(chapters, key=lambda c: c.start_at)]
    out: list[tuple[float, Chapter]] = []
    for j, (ot, c) in enumerate(pts):
        oe = pts[j + 1][0] if j + 1 < len(pts) else total
        if oe - ot <= 1e-3:
            continue
        out.append((ot, c))
    if out:
        out[0] = (0.0, out[0][1])
    return out


def post_unit_chapters(edl: Edl, idx: int) -> tuple[list[TimeRange], list[Chapter]]:
    """投稿単位 idx の (出力対象区間, その単位で実際に映る章) を返す。章は開始時刻順。

    🚨 **先頭の kept より前に始まる章も拾う**。G2 で冒頭を手で切ると、1章目の開始が
    カット区間に落ちて最初の kept より前になる。`[最初の kept, 最後の kept)` で絞ると
    その章が丸ごと欠け、2章目が 00:00 に繰り上がる（2026-09-10 実害）。
    尺ゼロの章の除外は `live_chapters` に任せる。
    前の単位に属する章を拾わないよう、2本目以降は単位スパンの先頭より前を見ない。
    """
    ranges = post_unit_ranges(edl, idx)
    if not ranges:
        return [], []
    units = edl.post_units or []
    span_lo = float("-inf")
    if 0 < idx < len(units) and units[idx].ranges:
        span_lo = min(r.start for r in units[idx].ranges)
    hi = ranges[-1].end
    # 末尾は**排他**にする。単位の境界(hi)は次の単位の先頭章の開始と同じ値なので、
    # 含めると前半の概要欄に後半の1章目が混ざる（2026-08-24 実害）。
    chs = [c for c in edl.chapters if span_lo - 1e-6 <= c.start_at < hi - 1e-6]
    return ranges, [c for _, c in live_chapters(ranges, chs, tuple(edl.freezes or ()))]


def post_unit_view(edl: Edl, idx: int) -> Edl:
    """投稿単位 idx だけを**その単位の出力時刻**で見た EDL（図解の入力用）。

    章は単位内の出力秒、字幕は単位の kept 内に始まるものだけ。
    🚨 **segments も出力時刻の1本 `[0, 単位の尺]` に置き換える**。章だけ出力秒へ写して
    segments を元のままにすると、`youtube_chapter_lines` がその出力秒を**ソース秒として
    もう一度写す**（2026-09-10 実害: 図解の入力で 01:43→00:12、07:25→02:55）。
    """
    ranges, chs = post_unit_chapters(edl, idx)
    if not ranges:
        raise ValueError(f"投稿単位 {idx} に区間が無い")
    frz = tuple(edl.freezes or ())
    total = sum(r.end - r.start for r in ranges) + sum(
        f.extra for f in frz if any(r.start < f.at < r.end for r in ranges))

    def _inside(t: float) -> bool:
        return any(r.start - 1e-6 <= t < r.end + 1e-6 for r in ranges)

    return edl.model_copy(update={
        "segments": [Segment(id="post_unit_view", start=0.0, end=total, invalid=False)],
        "freezes": [],
        "chapters": [
            c.model_copy(update={
                "start_at": 0.0 if i == 0 else _src_to_out(ranges, c.start_at, frz)})
            for i, c in enumerate(chs)
        ],
        "subtitles": [s for s in edl.subtitles if _inside(s.start)],
    })


def post_unit_chapter_lines(edl: Edl, idx: int) -> list[str]:
    """投稿単位 idx の YouTube章行（**単位内の出力時刻**・先頭は必ず 00:00）。"""
    ranges, chs = post_unit_chapters(edl, idx)
    if not ranges:
        return []
    lines: list[str] = []
    frz = tuple(edl.freezes or ())
    for i, c in enumerate(chs):
        ot = 0.0 if i == 0 else _src_to_out(ranges, c.start_at, frz)
        h, rem = divmod(int(ot), 3600)
        m, s = divmod(rem, 60)
        ts = f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
        lines.append(f"{ts} {c.chapter_title or f'チャプター{i + 1}'}")
    return lines


def n_post_units(edl: Edl) -> int:
    """投稿単位の数（0なら未設定＝収録まるごと1本扱い）。"""
    return len(edl.post_units or [])
