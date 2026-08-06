"""[S] 発話の「間」を**一定の長さに揃える**後段パス（無音区間の高速化）。

無音カット後も発話の隙間（間）は残る。特に方式B（TTS読み上げ）は読み上げ計739秒に対し
出力尺909秒＝約170秒が無音で、そのまま出すと間延びする。ただし**PCシステム音声
（デモの再生音）が鳴っている区間は内容そのもの**なので速くしてはいけない。

**ただ速くするだけでは足りない**（2026-08-05 ユーザー指摘）。倍率固定で縮めると、
元の間が長かったところは縮めても長いままで、**間の長さが場所によってバラつく**＝
発話のリズムが壊れる。そこで「速くする量」ではなく**「残す間の長さ」を目標にする**:

    発話が終わった**直後から**高速化を始め、次の発話の ``target`` 秒前で通常速度に戻す。
    → 元の間が何秒でも、**耳に残る間は必ず ``target`` 秒**になる。

``target`` は「発話が連続しているときの間」＝実測の**短いギャップの中央値**から自動で決める
（#103 方式Bでは 175 ギャップ中 116 個がちょうど 0.15 秒＝`schedule_clips` の最小間隔）。
目標に届かせるには倍率が足りないことがあるので（``倍率×target`` を超える間）、
そこだけ**倍率を上げる**（`max_factor` まで）。8倍のままだと長い間だけ残ってしまう。

`compose.eyecatch_insert` と同じ「合成済み mp4 を切って繋ぐ」後段パスにしてある。
本編の巨大 filtergraph や ``_src_to_out`` 系のタイムライン計算には手を入れない
（字幕・章・リボン・ちび・図解・BGM・アイキャッチが全部そこに依存しているため）。

「発話ブロック」＝速くしてはいけない区間（すべて**出力タイムライン秒**で判定）:

* **話者音声** … 方式Bは ``meta.voice.clips``（直列スケジュール後の実クリップ位置）、
  方式A/変換無しは ``voiced_word_spans``＋余白。**方式Bで元 word タイミングを使っては
  いけない**（読み上げは元の発話位置と一致しない）。
* **字幕** … **まるごとは塞がない**。守るのは (1) 発話が終わるまで出す＝``hold_max`` 秒
  以内に終わる字幕だけブロック終端を延ばす、(2) 表示開始から ``min_read`` 秒は通常速度。
  方式Aの字幕は**要約カード**で1枚が数十秒出っぱなし（#103 は出力尺の98%を占有）なので、
  まるごと塞ぐと高速化が効かない。
* **PCシステム音声** … desktop トラックの窓RMS。しきい値は**固定にしない**
  （暗騒音の量はトラックごとに違うので、20%点＝床から相対で決める）。
* **図解** … [I] インフォグラフィックの表示中。

PC音声・図解も「イベント」として扱い、**その手前にも同じ ``target`` 秒の間を残す**。

アイキャッチ挿入と併用する場合は **speedup を最後**に掛ける（アイキャッチ自体を速く
しないため）。計画はいったん素の出力タイムラインで作り、``shift_plan_by_inserts`` で
アイキャッチ挿入後の時刻へ写像する。章時刻の補正は ``shift_chapter_lines``。
"""

from __future__ import annotations

import math
import statistics
import subprocess
import tempfile
from pathlib import Path

from wwedit.common.media import ffmpeg_error, ffmpeg_path, probe
from wwedit.compose.ffmpeg_compose import _src_to_out, out_total, subtitles_to_output
from wwedit.edl.schema import Edl, TimeRange, voiced_word_spans

__all__ = [
    "merge_spans",
    "pad_spans",
    "invert_spans",
    "speech_spans_out",
    "speech_blocks_out",
    "blocked_spans_out",
    "soft_regions_out",
    "limit_shrink_in",
    "src_spans_to_out",
    "desktop_active_spans",
    "auto_target_gap",
    "uniform_gap_plan",
    "speedup_plan",
    "shifted_time",
    "shift_chapter_lines",
    "eyecatch_inserts",
    "shift_plan_by_inserts",
    "frame_segments",
    "build_filter_script",
    "effective_plan",
    "apply_speedups",
]

Span = tuple[float, float]
#: 高速化の計画。``(開始秒, 終了秒, 倍率)`` の列（倍率は区間ごとに違いうる）。
Plan = list[tuple[float, float, float]]

#: 高速化の**下限**倍率（ユーザー指定の「8倍速」）。目標の間に届かない区間だけ上がる。
SPEEDUP_FACTOR = 8.0
#: 倍率の上限。長い間を目標へ潰しきるために上げるが、際限なくは上げない。
MAX_SPEEDUP_FACTOR = 80.0
#: 目標の間（秒）。0 なら実測から自動決定（``auto_target_gap``）。
TARGET_GAP_S = 0.0
#: 自動決定に使うギャップの上限。これ以下＝「発話が連続している」とみなす。
AUTO_GAP_CUTOFF_S = 1.0
#: 自動決定した目標のクランプ範囲と、短いギャップが無かったときの既定。
AUTO_GAP_MIN_S, AUTO_GAP_MAX_S = 0.10, 0.60
AUTO_GAP_FALLBACK_S = 0.30
#: 目標よりこれ以下しか縮まらない間は触らない（細切れセグメントを増やさない）。
MIN_GAIN_S = 0.15
#: 発話に重なる字幕が発話より後ろまで残るとき、ブロックを延ばす上限。
#: 方式Aの要約字幕は1枚で数十秒あるので、無制限に延ばすと全部が塞がる。
SUBTITLE_HOLD_MAX_S = 1.0
#: どの字幕も**表示開始から**この秒数は通常速度で流す（読む時間の保証）。
SUBTITLE_MIN_READ_S = 2.5
#: word 由来の発話区間（方式A・変換無し）に足す前後の余白。word タイミングは
#: 文字数からの見積りで打ち切っているので、そのまま境界にすると語尾を食う恐れがある。
WORD_SPAN_PAD_S = 0.25
#: 通常速度セグメントの最小フレーム数。これを割ると ffmpeg の trim/concat グラフが
#: デッドロックして止まる（末尾に9フレーム残った時に実際に固まった）。
MIN_NORMAL_FRAMES = 12
#: 高速セグメントが出す最低フレーム数。1枚だけの高速化は意味が無いうえ壊れやすい。
MIN_FAST_OUT_FRAMES = 2
#: PCシステム音声の前後に残す余白。立ち上がりを削らない。
DESKTOP_PAD_S = 0.30
#: 図解表示の前後に残す余白。
PROTECT_PAD_S = 0.30
#: 図解の表示中に**縮めてよい割合の上限**。図解は静止カードなので下の無音を詰めても
#: 読みやすさは変わらない（縮むのは表示秒数だけ）。ただし読む時間を潰さないよう頭打ちにする。
#: 丸ごと保護すると**冒頭10秒だけ間が詰まらず**リズムが崩れる（ユーザー指摘の原因）。
SOFT_SHRINK_RATIO = 0.20

#: desktop トラックのRMSを測る窓。
RMS_WIN_S = 0.05
#: 暗騒音の床（20%点）から何dB上を「鳴っている」とみなすか（**相対**＝固定閾値にしない）。
DESKTOP_MARGIN_DB = 12.0
#: これ未満のダイナミックレンジしか無いトラックは「ずっと無音」とみなす。
DESKTOP_DYNAMIC_MIN_DB = 6.0
#: PC音声の最小継続。これ未満の単発ピークはクリックノイズとして無視。
DESKTOP_MIN_DUR_S = 0.30
#: PC音声の途切れをまたいで繋ぐ許容。曲間の一瞬の谷で切らない。
DESKTOP_GAP_S = 0.50


# --------------------------------------------------------------------------- 区間演算


def merge_spans(spans, *, gap: float = 0.0) -> list[Span]:
    """区間列を昇順にソートして重なり（と ``gap`` 以下の隙間）を結合する。"""
    out: list[list[float]] = []
    for a, b in sorted((float(a), float(b)) for a, b in spans):
        if b - a <= 1e-9:
            continue
        if out and a - out[-1][1] <= gap + 1e-9:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def pad_spans(spans, pad: float, *, lo: float = 0.0, hi: float | None = None) -> list[Span]:
    """各区間を前後 ``pad`` 秒だけ広げてから結合する（``lo``/``hi`` でクランプ）。"""
    grown = []
    for a, b in spans:
        s = max(lo, a - pad)
        e = b + pad if hi is None else min(hi, b + pad)
        grown.append((s, e))
    return merge_spans(grown)


def invert_spans(busy, total: float, *, lo: float = 0.0) -> list[Span]:
    """``[lo, total]`` から ``busy`` を除いた「空き」区間を返す。"""
    out: list[Span] = []
    cur = lo
    for a, b in merge_spans(busy):
        if a > cur + 1e-9:
            out.append((cur, min(a, total)))
        cur = max(cur, b)
        if cur >= total:
            break
    if cur < total - 1e-9:
        out.append((cur, total))
    return [(a, b) for a, b in out if b - a > 1e-9]


def src_spans_to_out(spans, ranges: list[TimeRange], freezes=()) -> list[Span]:
    """素材秒の区間列を**出力タイムライン秒**へ写す（カットで分断される分は複数になる）。"""
    out: list[Span] = []
    for a, b in spans:
        for r in ranges:
            lo, hi = max(float(a), r.start), min(float(b), r.end)
            if hi - lo > 1e-6:
                out.append((_src_to_out(ranges, lo, freezes), _src_to_out(ranges, hi, freezes)))
    return merge_spans(out)


# --------------------------------------------------------------------------- 話者音声


def speech_spans_out(edl: Edl, ranges: list[TimeRange], *, freezes=()) -> list[Span]:
    """話者の声が鳴っている**出力タイムライン**区間。

    方式B（TTS読み上げ）は ``meta.voice.clips``（voice-tts-finalize が書く実クリップ位置）を
    正とする。元の word タイミングは読み上げ位置と一致しないので使わない（§14.10-1 の罠）。
    それが無い場合（方式A・変換無し）だけ ``voiced_word_spans`` から作る。
    """
    clips = ((edl.meta or {}).get("voice") or {}).get("clips") or []
    if clips:
        return merge_spans(
            (float(c["out_start"]), float(c["out_end"])) for c in clips
        )
    src: list[Span] = []
    for u in edl.utterances:
        src.extend(voiced_word_spans(u.words))
    # word 由来は**見積り**（文字数×0.22秒で打ち切っている）なので前後に余白を取る。
    # 方式Bの ``meta.voice.clips`` は実クリップ位置なので余白は要らない。
    return pad_spans(src_spans_to_out(merge_spans(src), ranges, freezes),
                     WORD_SPAN_PAD_S)


# --------------------------------------------------------------------------- PC音声


def _rms_db(path: str | Path, *, win_s: float = RMS_WIN_S):
    """音声を 8kHz mono に落として窓RMSの dBFS 列を返す（``(db, hop_s)``）。

    ``cut.energy.load_envelope`` は重なり窓の巨大インデックス配列を作るので、全長1時間の
    トラックには使えない。ここは重ならないブロックRMSで十分（0.05秒粒度）。
    """
    import numpy as np

    sr = 8000
    cmd = [ffmpeg_path(), "-v", "error", "-i", str(path),
           "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or b"").decode("utf-8", "replace").splitlines()[-10:])
        raise RuntimeError(f"PC音声の読み込みに失敗: {path}\n{tail}")
    a = np.frombuffer(proc.stdout, dtype="<i2").astype(np.float32) / 32768.0
    win = max(1, int(sr * win_s))
    n = len(a) // win
    if n == 0:
        return np.zeros(0, dtype=np.float32), win_s
    frames = a[: n * win].reshape(n, win)
    rms = np.sqrt((frames * frames).mean(axis=1) + 1e-12)
    return 20.0 * np.log10(rms), win / sr


def desktop_active_spans(
    path: str | Path,
    *,
    win_s: float = RMS_WIN_S,
    margin_db: float = DESKTOP_MARGIN_DB,
    min_dur: float = DESKTOP_MIN_DUR_S,
    gap: float = DESKTOP_GAP_S,
) -> tuple[list[Span], dict]:
    """PCシステム音声が鳴っている**素材秒**の区間と、判定情報を返す。

    しきい値は**固定にしない**。トラックごとに暗騒音の量が違うので、dBFS の20%点を床と
    みなして ``margin_db`` だけ上を「鳴っている」とする。ダイナミックレンジがほとんど
    無いトラック（ずっと無音／ずっと一定ノイズ）は「鳴っていない」と判定する。
    """
    import numpy as np

    db, hop = _rms_db(path, win_s=win_s)
    info: dict = {"path": str(path), "win_s": hop, "margin_db": margin_db}
    if db.size == 0:
        return [], {**info, "active_ratio": 0.0, "reason": "empty"}
    floor = float(np.percentile(db, 20))
    top = float(np.percentile(db, 99))
    info.update(floor_db=round(floor, 1), p99_db=round(top, 1))
    if top - floor < DESKTOP_DYNAMIC_MIN_DB:
        return [], {**info, "active_ratio": 0.0, "reason": "flat"}
    thr = floor + margin_db
    info["threshold_db"] = round(thr, 1)
    on = (db >= thr).astype(np.int8)
    d = np.diff(np.concatenate(([0], on, [0])))
    starts = np.flatnonzero(d == 1)
    ends = np.flatnonzero(d == -1)
    spans = merge_spans(
        ((float(s) * hop, float(e) * hop) for s, e in zip(starts, ends, strict=True)), gap=gap
    )
    spans = [(a, b) for a, b in spans if b - a >= min_dur]
    total = float(db.size) * hop
    info["active_ratio"] = round(sum(b - a for a, b in spans) / total, 4) if total else 0.0
    info["n_spans"] = len(spans)
    return spans, info


# --------------------------------------------------------------------------- 本体


def speech_blocks_out(
    edl: Edl, ranges: list[TimeRange], *, freezes=(),
    hold_max: float = SUBTITLE_HOLD_MAX_S, min_read: float = SUBTITLE_MIN_READ_S,
) -> list[Span]:
    """**発話ブロック**（「速くしてはいけない」区間）＝声＋字幕の必要分。

    字幕の扱いは**字幕まるごとを塞がない**。方式Aの字幕は caption-summarizer の
    **要約カード**で、1枚が12〜46秒も出っぱなしになる（#103 は70枚で出力尺の98%を占有）。
    まるごと塞ぐと高速化がまったく効かない（実測: 909.5秒中3.3秒しか縮まなかった）。
    そこで2つだけ守る:

    * **発話が終わるまでは字幕を出す** … 発話に重なる字幕は、その発話ブロックの終端を
      字幕の終端まで延ばす（ただし ``hold_max`` 秒まで。要約カードで無限に延びないため）。
    * **出た字幕は必ず読める** … 各字幕の**表示開始から ``min_read`` 秒**は通常速度で流す。
    """
    frz = tuple(freezes or ())
    speech = list(speech_spans_out(edl, ranges, freezes=frz))
    subs = [(s.start, s.end) for s in subtitles_to_output(edl.subtitles, ranges, frz)]
    blocks: list[Span] = []
    for a, b in speech:
        # **``hold_max`` 秒以内に終わる字幕だけ**が延長対象。もっと先まで残る字幕は
        # 「発話に付いた字幕」ではなく出っぱなしの要約カードなので、待っても意味が無い
        # （待つと1発話あたり ``hold_max`` 秒ずつ伸びて全部が繋がってしまう）。
        ends = [se for ss, se in subs if ss < b < se <= b + hold_max]
        blocks.append((a, max([b, *ends])))
    blocks += [(ss, min(se, ss + min_read)) for ss, se in subs]
    return merge_spans(blocks)


def blocked_spans_out(
    edl: Edl, ranges: list[TimeRange], *, freezes=(), desktop_src_spans=(),
    total: float | None = None, desktop_pad: float = DESKTOP_PAD_S,
) -> list[Span]:
    """発話以外で**速くしてはいけない**区間（＝PCシステム音声が鳴っている所）。

    図解はここに**入れない**。静止カードなので下の無音を詰めても読みやすさは変わらず、
    縮むのは表示秒数だけ。丸ごと保護していたら冒頭10秒の間だけ元のまま残り、
    「"何かありますか？"の後だけ間が長い」というユーザー指摘の原因になった。
    代わりに ``soft_regions_out`` ＋ ``limit_shrink_in`` で**縮める割合に上限**を掛ける。
    """
    frz = tuple(freezes or ())
    tot = out_total(ranges, frz) if total is None else float(total)
    out: list[Span] = []
    if desktop_src_spans:
        out += pad_spans(
            src_spans_to_out(desktop_src_spans, ranges, frz), desktop_pad, hi=tot)
    return merge_spans(out)


def soft_regions_out(edl: Edl, ranges: list[TimeRange], *, freezes=(),
                     total: float | None = None) -> list[Span]:
    """**縮めすぎてはいけない**区間（読む時間が要るもの＝図解）。出力秒。"""
    frz = tuple(freezes or ())
    tot = out_total(ranges, frz) if total is None else float(total)
    ig = edl.infographic
    if not (ig and ig.enabled and ig.path):
        return []
    return pad_spans([(ig.start_s, ig.start_s + ig.duration_s + ig.fade_s)],
                     PROTECT_PAD_S, hi=tot)


def limit_shrink_in(plan: Plan, regions, *, ratio: float = SOFT_SHRINK_RATIO) -> Plan:
    """``regions`` の中で縮む量が ``ratio`` を超えないよう、高速化区間を短く切り詰める。

    区間を捨てずに**縮める（＝先頭側だけ残す）**。捨てると間がまるごと元の長さで残り、
    かえってリズムが崩れるため。
    """
    out: Plan = []
    budgets = {(a, b): (b - a) * ratio for a, b in regions}
    for a, b, f in plan:
        for (ra, rb), left in list(budgets.items()):
            ov_a, ov_b = max(a, ra), min(b, rb)
            if ov_b <= ov_a:
                continue
            saved = (ov_b - ov_a) * (1.0 - 1.0 / f)
            if saved <= left:
                budgets[(ra, rb)] = left - saved
                continue
            # 使える残量ぶんだけ高速化する（重なりの先頭から）
            keep = left / (1.0 - 1.0 / f)
            budgets[(ra, rb)] = 0.0
            b = min(b, ov_a + keep)
        if b - a > 1e-6 and f > 1.0:
            out.append((a, b, f))
    return out


def auto_target_gap(
    blocks, total: float, *, cutoff: float = AUTO_GAP_CUTOFF_S,
    lo: float = AUTO_GAP_MIN_S, hi: float = AUTO_GAP_MAX_S,
    fallback: float = AUTO_GAP_FALLBACK_S,
) -> float:
    """「発話が連続しているときの間」を実測から決める（＝揃える目標値）。

    ``cutoff`` 以下のギャップ＝もともと詰まっている所とみなし、その**中央値**を採る。
    方式Bは ``schedule_clips`` の最小間隔がそのまま出るので綺麗に決まる（#103 は 0.15秒）。
    短いギャップが1つも無い収録では ``fallback``。
    """
    gaps = [b - a for a, b in invert_spans(blocks, total)]
    short = [g for g in gaps if g <= cutoff]
    if not short:
        return fallback
    return min(hi, max(lo, float(statistics.median(short))))


def uniform_gap_plan(
    blocks, blocked, total: float, *, target: float,
    factor: float = SPEEDUP_FACTOR, max_factor: float = MAX_SPEEDUP_FACTOR,
    min_gain: float = MIN_GAIN_S,
) -> Plan:
    """**間の長さを ``target`` 秒に揃える**高速化計画を作る。

    ``blocks``（発話）と ``blocked``（PC音声・図解）を合わせて「イベント」とし、その間の
    空きを対象にする。空き ``G`` に対し、**先頭（＝発話の終了直後）から** ``x`` 秒を
    高速化し、残り ``target`` 秒を通常速度で次のイベントへ繋ぐ:

        (G - x) + x / f = target   →   x = (G - target) * f / (f - 1)

    ``x`` が ``G`` を超える（＝ ``G > target * f``）ときは 8倍では目標に届かないので、
    **空き全部を高速化して倍率を ``G / target`` へ上げる**（``max_factor`` で頭打ち）。
    倍率を固定したままだと長い間だけ残って、リズムがバラつく（ユーザー指摘の原因）。
    """
    events = merge_spans(list(blocks) + list(blocked))
    plan: Plan = []
    for a, b in invert_spans(events, total):
        g = b - a
        if g - target <= min_gain:
            continue
        x = (g - target) * factor / (factor - 1.0)
        f = factor
        if x >= g:                       # 8倍では target に届かない → 全部速くして倍率を上げる
            x, f = g, min(max_factor, g / target)
        if f <= 1.0 or x <= 0:
            continue
        plan.append((a, a + x, f))
    return plan


def speedup_plan(
    edl: Edl, ranges: list[TimeRange], *, freezes=(), desktop_src_spans=(),
    total: float | None = None, target_gap: float = TARGET_GAP_S,
    factor: float = SPEEDUP_FACTOR, max_factor: float = MAX_SPEEDUP_FACTOR,
    desktop_pad: float = DESKTOP_PAD_S,
) -> tuple[Plan, dict]:
    """EDL から高速化計画を作る（アイキャッチ挿入は考慮しない**素の**出力時刻）。

    ``desktop_src_spans`` は ``desktop_active_spans`` の結果（**素材秒**）をそのまま渡す。
    ``target_gap<=0`` なら ``auto_target_gap`` で自動決定する。
    """
    frz = tuple(freezes or ())
    tot = out_total(ranges, frz) if total is None else float(total)
    blocks = speech_blocks_out(edl, ranges, freezes=frz)
    blocked = blocked_spans_out(edl, ranges, freezes=frz,
                                desktop_src_spans=desktop_src_spans, total=tot,
                                desktop_pad=desktop_pad)
    soft = soft_regions_out(edl, ranges, freezes=frz, total=tot)
    target = float(target_gap) if target_gap > 0 else auto_target_gap(blocks, tot)
    plan = uniform_gap_plan(blocks, blocked, tot, target=target,
                            factor=factor, max_factor=max_factor)
    plan = limit_shrink_in(plan, soft)
    facs = [f for _, _, f in plan]
    info = {
        "target_gap_s": round(target, 3),
        "auto_target": target_gap <= 0,
        "n_blocks": len(blocks),
        "n_blocked": len(blocked),
        "n_soft": len(soft),
        "n_spans": len(plan),
        "sped_s": round(sum(b - a for a, b, _ in plan), 2),
        "factor_min": round(min(facs), 1) if facs else 0.0,
        "factor_max": round(max(facs), 1) if facs else 0.0,
        "factor_median": round(statistics.median(facs), 1) if facs else 0.0,
        "saved_s": round(sum((b - a) * (1 - 1 / f) for a, b, f in plan), 2),
    }
    return plan, info


def shifted_time(t: float, plan) -> float:
    """高速化後のタイムラインでの時刻（``plan`` は同じタイムライン上の高速化計画）。"""
    saved = 0.0
    for a, b, f in plan:
        if t <= a:
            break
        saved += (min(t, b) - a) * (1.0 - 1.0 / f)
    return t - saved


def _parse_ts(s: str) -> float | None:
    parts = s.split(":")
    if not (2 <= len(parts) <= 3) or not all(p.isdigit() for p in parts):
        return None
    v = 0.0
    for p in parts:
        v = v * 60 + int(p)
    return v


def _fmt_ts(t: float) -> str:
    h, rem = divmod(int(t + 1e-6), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def shift_chapter_lines(lines, plan) -> list[str]:
    """``MM:SS タイトル`` 形式の章行を、高速化後の時刻へ書き換える。

    アイキャッチと併用する場合は ``shifted_chapter_lines`` の出力（＝アイキャッチ挿入後の
    時刻）を渡し、``plan`` も挿入後タイムラインへ写したものを渡す。補正が二重にならない
    ように、章時刻の補正は必ずこの順で1回ずつ通す。
    """
    out: list[str] = []
    for line in lines:
        head, sep, rest = line.partition(" ")
        t = _parse_ts(head)
        out.append(f"{_fmt_ts(shifted_time(t, plan))}{sep}{rest}"
                   if t is not None else line)
    return out


def eyecatch_inserts(
    edl: Edl, ranges: list[TimeRange] | None = None, *,
    duration: float = 2.0, skip_first: bool = True,
) -> list[tuple[float, float]]:
    """アイキャッチ挿入を ``[(素の出力秒, 挿入秒)]`` として返す（順序固定用）。"""
    from wwedit.compose.eyecatch_insert import eyecatch_boundaries

    bounds, _ = eyecatch_boundaries(edl, ranges, duration=duration)
    return [(b["out_at"], duration) for i, b in enumerate(bounds)
            if not (skip_first and i == 0 and b["out_at"] <= 1e-6)]


def shift_plan_by_inserts(plan, inserts) -> Plan:
    """素の出力タイムライン上の計画を、**挿入物（アイキャッチ）を跨いだ後**の時刻へ写す。

    挿入点を含む区間は挿入物を挟んで2つに割る（アイキャッチまで速くしないため）。倍率は
    そのまま引き継ぐ。
    """
    ins = sorted((float(at), float(dur)) for at, dur in inserts)
    out: Plan = []
    for a, b, f in sorted(plan):
        cur, shift = a, 0.0
        for at, dur in ins:
            if at <= a + 1e-9:
                shift += dur
                continue
            if at >= b - 1e-9:
                break
            out.append((cur + shift, at + shift, f))  # 挿入点まで
            shift += dur
            cur = at
        if b - cur > 1e-9:
            out.append((cur + shift, b + shift, f))
    return [(a, b, f) for a, b, f in out if b - a > 1e-9]


def _atempo_chain(factor: float) -> str:
    """``atempo`` は1段あたり2倍までなので段数に分解する。"""
    parts: list[float] = []
    f = float(factor)
    while f > 2.0 + 1e-9:
        parts.append(2.0)
        f /= 2.0
    if abs(f - 1.0) > 1e-6:
        parts.append(f)
    return ",".join(f"atempo={p:g}" for p in parts) or "anull"


def frame_segments(plan, total: float, *, fps: int) -> list[tuple[int, int, int]]:
    """高速化計画を**フレーム番号**の連続セグメント ``[(開始f, 終了f, 倍率)]`` に割る。

    倍率 1 は通常速度。境界は**内側へ**フレームに乗せる（発話へ食い込ませない）。

    ⚠️ **長さを「倍率の倍数」へ切り詰めてはいけない**（2026-08-05 に踏んだ）。
    A/V をぴったり合わせようとして ``fb -= (fb-fa) % fac`` としていたが、これだと
    **端数（最大で倍率−1フレーム＝69倍なら2.7秒）が通常速度で残り、間がばらつく**。
    実測で 154 本中 48 本が 0.3 秒超・最大 2.20 秒になり、リズムが崩れていた。
    正しくは**切り詰めず**、``select`` が実際に何枚出すか（``ceil(長さ/倍率)``）を
    そのまま使って音声の目標長を合わせる（`build_filter_script`）。ずれは最大1フレーム。

    **極端に短いセグメントは作らない**。ffmpeg の trim/concat グラフは数フレームの
    セグメントが混ざると**デッドロックして止まる**（末尾に9フレームの通常セグメントが
    残った時に実際に固まった。単体では動くのに繋ぐと止まるので原因が分かりにくい）。
    通常側は ``MIN_NORMAL_FRAMES`` 枚を割らないよう高速区間の方を削って譲る。
    高速側が ``MIN_FAST_OUT_FRAMES`` 枚出せないときは**捨てずに倍率を落とす**
    （捨てるとその間だけ通常速度で残り、目的である「間の均一化」が崩れる）。
    """
    n_total = int(round(total * fps))
    items = sorted(plan)
    segs: list[tuple[int, int, int]] = []
    cur = 0
    for i, (a, b, f) in enumerate(items):
        fa = max(cur, math.ceil(a * fps - 1e-6))    # 内側へ寄せる（前の発話を食わない）
        fb = min(n_total, math.floor(b * fps + 1e-6))
        # 前後の通常セグメントが短くなりすぎないよう、高速区間の方を削る
        if cur > 0:
            fa = max(fa, cur + MIN_NORMAL_FRAMES)
        nxt = (math.ceil(items[i + 1][0] * fps - 1e-6) if i + 1 < len(items) else n_total)
        fb = min(fb, nxt - MIN_NORMAL_FRAMES)
        n = fb - fa
        fac = max(2, int(round(f)))
        # 出力が少なすぎるときは**捨てずに倍率を落とす**。捨てるとその間だけ通常速度で
        # 残ってしまい、「間を一定にする」という目的そのものが崩れる。
        if n < fac * MIN_FAST_OUT_FRAMES:
            fac = max(2, n // MIN_FAST_OUT_FRAMES)
        if n < fac:
            continue
        if fa > cur:
            segs.append((cur, fa, 1))
        segs.append((fa, fb, fac))
        cur = fb
    if n_total > cur:
        segs.append((cur, n_total, 1))
    return segs


def seg_out_frames(n: int, fac: int) -> int:
    """``select='not(n-trunc(n/fac)*fac)'`` が ``n`` 枚から実際に残す枚数。

    n=0, fac, 2fac, … を残すので ``ceil(n/fac)``。**音声の目標長も映像の出力尺も
    必ずこの枚数から出す**（別々に計算すると1フレーム未満のずれが区間数ぶん累積して
    口パクと声がずれる）。
    """
    return -(-n // fac) if fac > 1 else n


def effective_plan(plan, total: float, *, fps: int) -> Plan:
    """``apply_speedups`` が**実際に**高速化する区間（秒）。章時刻の補正はこれで行う。

    フレーム境界へ寄せるぶん計画と少し変わり、実効倍率も ``長さ/出力枚数``（整数倍率
    そのものではない）になるので、計画のままで章時刻を補正すると出力とずれる。
    """
    out: Plan = []
    for fa, fb, fac in frame_segments(plan, total, fps=fps):
        if fac <= 1:
            continue
        n = fb - fa
        out.append((fa / fps, fb / fps, n / seg_out_frames(n, fac)))
    return out


def build_filter_script(segs, *, fps: int) -> str:
    """``frame_segments`` の結果から ffmpeg の filtergraph を組む（純関数・テスト可能）。

    ここに A/V 同期の細工が全部入っている（§16.5）。特に:

    * 映像は ``fps`` フィルタでなく **``select`` で間引く**（``fps`` だと区間の終わりに
      複製フレームが1枚乗り、区間数ぶん尺が伸びて音とずれる）。式にカンマを使わない書き方。
    * 音声は ``atempo``（WSOLA）の出力長を信用せず、**目標長へ強制**する。
      ⚠️ 引数なしの ``apad`` は**無限に無音を作り続けて ffmpeg がハングする**
      （``atrim`` は上流へEOFを返さない。短い高速セグメントで実際に固まった）。
      必ず ``whole_dur=`` で全長を与えて有限にすること。
    * 目標長は**映像より 0.1ms 短く**する。``concat`` は区間ごとに長い方のストリームへ
      揃えるので、音が1サンプルでも長いと映像フレームが複製されて尺が伸びる。
    """
    af = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
    chains: list[str] = []
    order: list[str] = []
    for i, (fa, fb, fac) in enumerate(segs):
        sel = f"select='not(n-trunc(n/{fac:d})*{fac:d})'," if fac > 1 else ""
        chains.append(
            f"[0:v]trim=start_frame={fa}:end_frame={fb},{sel}"
            f"setpts=N/{fps}/TB,format=yuv420p[v{i}]")
        out_dur = seg_out_frames(fb - fa, fac) / fps - 1e-4
        chains.append(
            f"[0:a]atrim={fa / fps:.6f}:{fb / fps:.6f},asetpts=PTS-STARTPTS,"
            f"{_atempo_chain(fac) if fac > 1 else 'anull'},"
            f"apad=whole_dur={out_dur:.6f},atrim=0:{out_dur:.6f},"
            f"asetpts=PTS-STARTPTS,{af}[a{i}]")
        order.append(f"[v{i}][a{i}]")
    chains.append(f"{''.join(order)}concat=n={len(order)}:v=1:a=1[outv][outa]")
    return ";\n".join(chains)


def apply_speedups(
    in_mp4: str | Path,
    out_mp4: str | Path,
    plan,
    *,
    fps: int = 30,
    crf: int = 20,
    preset: str = "medium",
) -> Path:
    """``plan``（入力mp4のタイムライン秒 ``(開始, 終了, 倍率)``）に従って詰めた mp4 を書き出す。

    区間で分割 → 該当区間だけ ``setpts`` で詰め、音声も ``atempo`` で同じだけ詰めて concat。
    音を捨てないのは、無音といっても**本編BGMは鳴っている**ため（捨てると穴が開く）。
    倍率は整数へ丸める（フレーム単位で割り切れないと A/V がずれるため）。
    """
    in_mp4, out_mp4 = Path(in_mp4).resolve(), Path(out_mp4).resolve()
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    total = probe(in_mp4).duration_s
    segs = frame_segments(plan, total, fps=fps)
    if not any(fac > 1 for _, _, fac in segs):
        raise ValueError("高速化する区間が無い")

    work = Path(tempfile.mkdtemp())
    script = work / "speedup.ffscript"
    script.write_text(build_filter_script(segs, fps=fps), encoding="utf-8")
    cmd = [ffmpeg_path(), "-y", "-i", str(in_mp4),
           "-filter_complex_script", str(script),
           "-map", "[outv]", "-map", "[outa]",
           "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
           "-c:a", "aac", "-b:a", "192k", str(out_mp4)]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        tail = ffmpeg_error(proc.stderr)
        raise RuntimeError(f"高速化の書き出しに失敗:\n{tail}")
    return out_mp4
