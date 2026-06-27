"""[I] 要約字幕（要所・10〜12秒間隔）のファイル経由 I/O。

字幕は文字起こしそのまま（全時間帯）ではなく、**会話の流れを読んで要所を短く要約**し、
10〜12秒間隔で出す。要約判断は LLM（``caption-summarizer`` スキル、Sonnetサブエージェント・
ファイルI/O＝主ループにtranscriptを載せない [[content-gen-sonnet-subagent]]）に任せる。

**タイミングはカット後（無音/フィラー除去後）に合わせる**のが要点 [[subtitle-double-border-spec]]:
発話単位の粗いアンカーだと長い発話でズレるため、**単語タイムスタンプで「カット後に残る実発話」を
~12秒の窓に切り**、各窓を字幕の単位にする。各窓は実際に喋っている瞬間にアンカーされる。
"""

from __future__ import annotations

import json
from pathlib import Path

from wwedit.edl.schema import Edl, Subtitle, SubtitleStyle, TimeRange

__all__ = ["build_caption_windows", "write_caption_input", "apply_captions"]


def _mmss(t: float) -> str:
    m, s = divmod(int(t), 60)
    return f"{m:02d}:{s:02d}"


def _src_to_out(ranges: list[TimeRange], t: float) -> float:
    """ソース時刻 t を keep連結後の出力タイムライン秒へ。カット内なら次keep先頭へスナップ。"""
    acc = 0.0
    for r in ranges:
        if t < r.start:
            return acc
        if t <= r.end:
            return acc + (t - r.start)
        acc += r.end - r.start
    return acc


def _in_kept(ranges: list[TimeRange], t: float) -> bool:
    return any(r.start <= t <= r.end for r in ranges)


def build_caption_windows(
    edl: Edl, *, window_s: float = 12.0, anchor_frac: float = 0.45
) -> list[dict]:
    """単語TSから「カット後に残る実発話」を ~window_s の**時間窓**に切る（字幕の単位）。

    各窓 ``{idx, source_start, anchor, out_start, speaker, text}``: 話者は窓内の**多数決**
    （主に喋っている人＝色分け）。``anchor``=窓を ``anchor_frac`` 進んだ所の実発話ソース時刻
    （字幕の表示開始に使う）。窓頭に出すと要約が発話に先行して見えるため、中ほどに寄せる。
    相槌が挟まっても窓は割らず時間で区切る。カット区間内の語は除外。
    """
    ranges = edl.kept_ranges()
    kept: list[tuple[float, float, str, str]] = []  # (src, out, text, speaker)
    for u in edl.utterances:
        for w in u.words:
            if w.text and _in_kept(ranges, w.start):
                kept.append((w.start, _src_to_out(ranges, w.start), w.text, u.speaker))
    kept.sort(key=lambda x: x[1])

    # まず時間窓に分割（語リストを保持）
    groups: list[list[tuple[float, float, str, str]]] = []
    for item in kept:
        if not groups or item[1] - groups[-1][0][1] >= window_s:
            groups.append([item])
        else:
            groups[-1].append(item)

    windows: list[dict] = []
    for i, g in enumerate(groups):
        out0 = g[0][1]
        counts: dict[str, int] = {}
        for _s, _o, _t, sp in g:
            counts[sp] = counts.get(sp, 0) + 1
        windows.append({
            "idx": i,
            "source_start": g[0][0],     # 窓(発話チャンク)の開始＝表示開始
            "source_end": g[-1][0],      # 窓の終端＝表示終了（発話の終わりまで出す）
            "out_start": out0,
            "speaker": max(counts, key=counts.get),
            "text": "".join(w[2] for w in g),
        })
    return windows


def write_caption_input(edl: Edl, out_path: str | Path, *, window_s: float = 12.0) -> Path:
    """要約字幕用の入力TSVを書く。各行＝カット後 ~window_s の実発話窓（出力時刻付き）。

    各行 ``<idx>\\t<出力mm:ss>\\t<speaker>\\t<text>``。LLM は各窓を短い要約字幕にする
    （相槌だけの窓は飛ばす）。``utt`` に窓idxを返させ、コードが時刻へ変換（時刻計算させない）。
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wins = build_caption_windows(edl, window_s=window_s)
    lines = [
        "# idx\tout_time\tspeaker\ttext"
        "（各行=カット後 約12秒の実発話窓。各窓を短い要約字幕に。相槌だけの窓は飛ばす。utt=窓idx）"
    ]
    for w in wins:
        # 全空白（\t \n \r や Unicode 行区切り \x85/  等）を1スペースへ畳む＝必ず1行1窓。
        # （\n だけ除去では \r 等が残り splitlines/行ベース処理が壊れる）
        text = " ".join(w["text"].split())
        lines.append(f"{w['idx']}\t{_mmss(w['out_start'])}\t{w['speaker']}\t{text}")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


DISCLAIMER_TEXT = "【注意】本編字幕はAI作成のため用語・内容間違いありえます🫠"


def apply_captions(
    edl: Edl,
    decisions_path: str | Path,
    *,
    min_s: float = 1.2,
    style: SubtitleStyle = "main",
    window_s: float = 12.0,
    disclaimer: str | None = DISCLAIMER_TEXT,
    disclaimer_s: float = 6.0,
) -> Edl:
    """LLM の要約字幕決定（``{"captions":[{"utt":<窓idx>,"text":str}]}``）を EDL.subtitles へ。

    各字幕は対応**窓（発話チャンク）の [開始, 終了] 全体**を表示区間にする（ソース時刻）。
    発話の終わりまで字幕を出す＝早く消えない。compose 側で出力タイムラインへ再マップ。次字幕の
    開始は越えない（重なり防止）。話者は窓の多数決（色分け用）。
    """
    from wwedit.privacy.masking import apply_name_replacements, load_name_replacements

    name_map = load_name_replacements()  # 漢字名→カタカナ等（.env、コード非直書き）
    dec = json.loads(Path(decisions_path).read_text(encoding="utf-8"))
    wins = build_caption_windows(edl, window_s=window_s)
    items: list[tuple[float, float, str, str | None]] = []  # (start, win_end, text, speaker)
    for c in dec.get("captions", []):
        idx = int(c.get("utt", -1))
        text = apply_name_replacements(str(c.get("text", "")).strip(), name_map)
        if 0 <= idx < len(wins) and text:
            w = wins[idx]
            items.append((w["source_start"], w["source_end"], text, w["speaker"]))
    items.sort(key=lambda x: x[0])
    subs: list[Subtitle] = []
    for i, (start, win_end, text, speaker) in enumerate(items):
        # 切れ目なし: 次字幕が出る瞬間まで前の字幕を伸ばす（最後は窓終端まで）。
        end = items[i + 1][0] if i + 1 < len(items) else max(win_end, start + min_s)
        if end > start:
            subs.append(Subtitle(start=start, end=end, text=text, style=style, speaker=speaker))

    # 本編開始時の注意書き（AI生成の免責）を最初の字幕として差し込む（話者なし＝既定色）
    ranges = edl.kept_ranges()
    if disclaimer and ranges:
        d_start = ranges[0].start
        d_end = d_start + disclaimer_s
        if subs:
            d_end = min(d_end, subs[0].start)  # 最初の実字幕に被せない
        if d_end > d_start:
            subs.insert(0, Subtitle(start=d_start, end=d_end, text=disclaimer, style=style))

    edl.subtitles = subs
    return edl
