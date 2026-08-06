"""[S2] **読み上げ主・映像従**の時間ワープ（素材⇄出力の可変速写像）。

## なぜ後段パスではダメなのか

以前は「合成済み mp4 の無音区間を丸ごと 8倍速にする」後段パスだった。これは**音声ごと**
早回しするので、間に鳴っている BGM・PC音声まで速くなり、発話ブロックの判定を1つでも
外すと読み上げが即巻き込まれる。ユーザー指摘:

> 本来は読み上げ・キャラの口パク・字幕は通常速度で最後まで読み上げている間に
> **収録映像だけ**が高速化することを想定しています。全部レンダリングしてから
> 高速化すりゃそりゃこうなるよ。合成前に調整しないと

したがって速度は**合成前**に決め、**収録映像にだけ**掛ける。字幕・ちびキャラ・リボン・
図解は出力タイムライン（＝読み上げのタイムライン）で動くので、自動的に等速のまま連動する。

## 座標系

3つある。混ぜると必ず壊れるので名前を分ける。

* **raw** … 収録ファイルの秒。EDL の segments / utterances / framing はこれ。
* **src'** … keep区間を連結した秒（＝**これまでの**出力タイムライン）。`_src_to_out` の値域。
* **out** … ワープ**後**の出力秒。字幕・章・ちび・読み上げは最終的にこれで動く。

``Warp`` は **src' → out** の区分線形写像。raw への変換は既存の `out_to_src` を使う
（ワープ後のレンダは src' 区間を keep区間の境界で割って素材から trim する）。

## どこを速くするか

1. 読み上げが鳴っているあいだは**映像も等速**（見せたい所を崩さない）。
2. 収まらず余った素材を、次の ``target``（既定0.15秒）の「間」へ押し込む。
   上限倍率（既定8倍）で収まるなら、**間がちょうど target になる倍率**（8倍以下）を使う。
3. 8倍でも収まらないときだけ、**発話映像の末尾**を8倍の対象に広げる。
   **発話の頭から早送りにはならない。**
4. 発話まるごと8倍でも足りないときだけ、間の倍率を ``gap_max_rate``（既定80倍）まで上げる。
5. 逆に**読み上げの方が長い**ときは、``lookahead`` 秒まで先の映像へ食い込み、
   それを超えたら**フリーズフレーム**で止める。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from wwedit.compose.ffmpeg_compose import _src_to_out, out_total
from wwedit.edl.schema import TimeRange

__all__ = [
    "SPEECH_MAX_RATE",
    "GAP_MAX_RATE",
    "TARGET_GAP_S",
    "WarpSeg",
    "Warp",
    "build_warp",
    "anchors_from_report",
    "anchors_with_rows",
]

#: 発話中（＝読み上げが鳴っている間）の映像倍率の上限。
#: 当初 3.0 にしていたが「間は8倍なんだから3倍以下に頑張って遅くしなくてよい」との判断で 8.0。
#: 低く抑えると**映像が読み上げの内容から遅れる**（追いつけないぶんが次の間へ持ち越される）。
SPEECH_MAX_RATE = 8.0
#: 間（無音）の映像倍率の上限。誰も見ていないので大きくてよい。
GAP_MAX_RATE = 80.0
#: 揃える間の長さ（秒）。発話と発話のあいだは常にこの長さになる。
TARGET_GAP_S = 0.15
#: 読み上げが元発話より長いとき、**次の発話の映像へ何秒まで食い込んでよいか**。
#: 0 にすると必ずフリーズになるが、テンポの速い掛け合いでは元発話が1秒でも読み上げは
#: 2〜3秒になるので**毎回フリーズ**が入る（実測: 144本・155.6秒＝出力の20%・最長14.6秒）。
#: 少しの先行は繋いで見せ、大きくずれるときだけフリーズで止める。
#: #103 実測（先行秒 → フリーズ本数/合計/最長）:
#:   0秒→144本/155.6s/14.6s ・ 1秒→95本/109.3s ・ 2秒→71本/78.8s ・ 3秒→51本/55.3s
#:   **5秒→21本/29.6s（出力の3.8%）** ・ 8秒→9本/10.8s
#: 画面共有が主なので5秒の先行はほぼ気づかない。フリーズの方が目立つので 5.0 を既定にする。
LOOKAHEAD_S = 5.0
#: 数値誤差の許容。
EPS = 1e-6


@dataclass(frozen=True)
class WarpSeg:
    """出力 ``out_dur`` 秒のあいだに素材 ``[src_start, src_end)`` を流す区間。

    ``src_end == src_start`` は**フリーズ**（素材が尽きた末尾など）。
    """

    src_start: float
    src_end: float
    out_dur: float
    kind: str = "gap"          # speech / gap / hold / freeze

    @property
    def src_dur(self) -> float:
        return self.src_end - self.src_start

    @property
    def rate(self) -> float:
        """映像の再生倍率（1.0=等速）。フリーズは 0.0。"""
        return self.src_dur / self.out_dur if self.out_dur > EPS else 0.0


@dataclass
class Warp:
    """``WarpSeg`` の列。src'（keep連結秒）→ out（ワープ後の出力秒）。

    ``placements`` は ``build_warp`` に渡した ``anchors`` と**同じ並び**で
    ``(out開始, out尺)``。読み上げクリップ・字幕・口パクはこの位置に置く。
    PC音声で倍率1.0を強制された区間が発話に食い込むと **out尺は読み上げ実尺より長くなる**
    （余りは無音＝画面の音を聞かせる間）ので、実尺を勝手に足して位置を計算してはいけない。
    """

    segs: list[WarpSeg]
    placements: list[tuple[float, float]] = field(default_factory=list)

    @property
    def out_total(self) -> float:
        return sum(s.out_dur for s in self.segs)

    @property
    def src_total(self) -> float:
        return self.segs[-1].src_end if self.segs else 0.0

    def src_to_out(self, t: float) -> float:
        """src' 秒 → out 秒。区間内は線形補間。範囲外は端にクランプ。"""
        acc = 0.0
        for s in self.segs:
            if t < s.src_start:
                return acc
            if t <= s.src_end + EPS and s.src_dur > EPS:
                return acc + (t - s.src_start) / s.rate
            acc += s.out_dur
        return acc

    def out_to_src(self, t: float) -> float:
        """out 秒 → src' 秒（``src_to_out`` の逆）。フリーズ中は素材時刻が進まない。"""
        acc = 0.0
        for s in self.segs:
            if t <= acc + s.out_dur + EPS:
                return s.src_start + (t - acc) * s.rate
            acc += s.out_dur
        return self.src_total

    def rate_at(self, t: float) -> float:
        """out 秒 t の時点で映像が何倍速か（説明・検証用）。"""
        acc = 0.0
        for s in self.segs:
            if t <= acc + s.out_dur + EPS:
                return s.rate
            acc += s.out_dur
        return 1.0


def anchors_with_rows(
    rows: list[dict], ranges: list[TimeRange], *, freezes=(),
) -> list[tuple[tuple[float, float, float], dict]]:
    """``voice_tts_report.json`` の行 → ``((src'開始, src'終了, 読み上げ秒), 元の行)``。

    ``src_start``/``src_end`` は**元発話**の raw 秒なので src' へ写す。読み上げ秒は
    合成済み wav の実尺（``tts_s``）。読み上げが元発話より短いぶんだけ映像が速くなる。

    行を一緒に返すのは、``Warp.placements`` と**同じ並び**で読み上げ・字幕・口パクを
    置き直すため。別々に絞り込むと並びがずれて全部が別人の声になる。
    """
    out: list[tuple[tuple[float, float, float], dict]] = []
    for r in rows:
        d = float(r.get("tts_s") or 0.0)
        if d <= EPS:
            continue
        a = _src_to_out(ranges, float(r["src_start"]), freezes)
        b = _src_to_out(ranges, float(r["src_end"]), freezes)
        out.append(((a, max(a, b), d), r))
    out.sort(key=lambda x: x[0])
    return out


def anchors_from_report(
    rows: list[dict], ranges: list[TimeRange], *, freezes=(),
) -> list[tuple[float, float, float]]:
    """``anchors_with_rows`` のアンカーだけ版。"""
    return [a for a, _ in anchors_with_rows(rows, ranges, freezes=freezes)]


def build_warp(
    anchors,
    ranges: list[TimeRange],
    *,
    freezes=(),
    total: float | None = None,
    target_gap: float = TARGET_GAP_S,
    speech_max_rate: float = SPEECH_MAX_RATE,
    gap_max_rate: float = GAP_MAX_RATE,
    hold_spans=(),
    lookahead: float = LOOKAHEAD_S,
    fps: int | None = None,
) -> Warp:
    """読み上げ ``anchors`` に映像を合わせる Warp を作る。

    ``anchors`` = ``(src'開始, src'終了, 出力での秒数)``。出力タイムラインは
    **読み上げを順に並べ、あいだに ``target_gap`` 秒を置いたもの**になる。

    素材の消費はカーソル方式で貪欲に進める:

    考え方（ユーザー指示のとおり）:

    1. 読み上げが鳴っているあいだは**映像も等速**で流す（見せたい所を崩さない）。
    2. 読み上げに収まらず**余った素材**を、次の ``target`` 秒の「間」へ押し込む。
       余りが ``speech_max_rate`` 倍で収まるなら、**間がちょうど ``target`` になる倍率**
       （＝上限以下）を使う。8倍固定にはしない。
    3. 上限倍率でも収まらないときだけ、**発話映像の末尾**を上限倍率の対象に広げる。
       末尾を ``x`` 秒ぶん速くすると ``(R-1)·x`` だけ余分に素材を食えるので、
       ``x = (余り - target·R) / (R - 1)``。速い側が末尾に来るので、直後の間の
       高速化と連続して自然に見える（**発話の頭から早送りにはならない**）。
    4. 発話まるごと上限倍率でも足りないときだけ、間の倍率を ``gap_max_rate`` まで上げる。
    5. 逆に**読み上げの方が元発話より長い**ときは、素材が尽きた所で**フリーズフレーム**に
       する。等速で先へ流すと**次の発話の映像を先に消費**してしまい、映像だけが話の内容より
       先に進み続ける（ユーザー指摘）。どの区間も ``a_{i+1}``（次の発話の頭）を超えない。

    ``hold_spans`` は**倍率1.0を強制する src' 区間**（PCシステム音声が鳴っている所）。
    ここを速くすると画面と音がずれるうえ音程も変わる。

    ``fps`` を渡すと各区間の出力尺を**フレームに丸めた上で**タイムラインを積む。
    丸め前の値で字幕や読み上げの位置を決めると、レンダ結果と最大1フレームずつずれて
    積み上がる（393区間あるので無視できない）。
    """
    tot = out_total(ranges, freezes) if total is None else float(total)
    holds = _merge(hold_spans)
    segs: list[WarpSeg] = []
    places: list[tuple[float, float]] = []
    cur = 0.0                       # 消費済み src'
    out_cur = 0.0                   # 確定済み out

    def emit_at(rate: float, out_dur: float, kind: str,
                *, limit: float | None = None) -> None:
        """倍率と出力尺を決め打ちで流す（hold の区間だけ 1.0 に落とす）。

        ``limit``（素材秒）を超えて進まない。**読み上げが元発話より長いときは
        ここで素材が尽き、残りは自動的にフリーズフレームになる**。等速で先へ流すと
        次の発話の映像を先に消費してしまい、映像だけが話の内容より先に進み続ける。
        """
        nonlocal cur, out_cur
        stop = tot if limit is None else min(tot, limit)
        take = max(0.0, min(rate * out_dur, stop - cur))
        _lay(take, rate, out_dur, kind)

    def emit(src_to: float, out_dur: float, kind: str, max_rate: float,
             *, extend: bool) -> None:
        """``cur`` から ``src_to`` を目指して ``out_dur`` 秒ぶん映像を流す（間・末尾用）。

        ``extend=True``（間）… ``src_to`` へ必ず追いつく。上限倍率で足りないときだけ
        **間を延ばす**。間は無音なので全体を速くしてよい。
        """
        nonlocal cur, out_cur
        want = max(0.0, min(src_to, tot) - cur)
        if out_dur <= EPS:
            out_dur = want / max_rate
        if out_dur <= EPS:
            return
        rate = min(max_rate, max(1.0, want / out_dur))
        if extend and want / rate > out_dur:
            out_dur = want / rate
        take = min(rate * out_dur, tot - cur)
        _lay(take, rate, out_dur, kind)

    def _lay(take: float, rate: float, out_dur: float, kind: str) -> None:
        """実際に ``WarpSeg`` を並べる（hold で割る／素材が尽きたらフリーズ）。"""
        nonlocal cur, out_cur
        # 素材が尽きたぶんは末尾フレームで埋める（フリーズ）
        frozen = out_dur - take / rate if rate > EPS else out_dur
        for a, b in _split_by_holds(cur, cur + take, holds):
            is_hold = _in_holds(a, b, holds)
            r = 1.0 if is_hold else rate
            seg = WarpSeg(a, b, _snap((b - a) / r, fps), "hold" if is_hold else kind)
            segs.append(seg)
            out_cur += seg.out_dur
        cur += take
        if frozen > EPS:
            frozen = _snap(frozen, fps)
            segs.append(WarpSeg(cur, cur, frozen, "freeze"))
            out_cur += frozen

    items = list(anchors)
    # 収録の頭にある素材（最初の発話より前）は間として流す
    if items and items[0][0] - cur > EPS:
        emit(items[0][0], target_gap, "gap", gap_max_rate, extend=True)
    for i, (_a, _b, dur) in enumerate(items):
        nxt = items[i + 1][0] if i + 1 < len(items) else tot
        want = max(0.0, min(nxt, tot) - cur)      # この窓で消費したい素材
        # 発話ぶんを等速で流したあとに**余る**素材。これを間に押し込む
        left = want - dur
        started = out_cur
        if left <= EPS:
            # 余りが無い＝速くする必要が無い。発話も間もそのまま等速。
            # ただし**次の発話の映像には食い込まない**（limit）。読み上げの方が長ければ
            # 素材がそこで尽き、残りはフリーズフレームになる。
            ahead = nxt + lookahead
            emit_at(1.0, dur, "speech", limit=ahead)
            places.append((started, out_cur - started))
            if target_gap > EPS:
                emit_at(1.0, target_gap, "gap", limit=ahead)
            continue
        if left <= target_gap * speech_max_rate + EPS:
            # **間だけで収まる**。8倍固定ではなく、間がちょうど target になる倍率にする
            emit_at(1.0, dur, "speech")
            places.append((started, out_cur - started))
            emit_at(max(1.0, left / target_gap) if target_gap > EPS else speech_max_rate,
                    target_gap, "gap")
            continue
        # 間を上限倍率にしても収まらない → **発話映像の末尾**を上限倍率の対象に広げる。
        # 末尾を x 秒ぶん上限倍率にすると (R-1)·x だけ余分に素材を食える。
        r = speech_max_rate
        x = _snap((left - target_gap * r) / (r - 1.0), fps)
        if x >= dur - EPS:
            # 発話まるごと上限倍率でも足りない → 間の倍率を上げる（無音なので上げてよい）
            emit_at(r, dur, "speech")
            places.append((started, out_cur - started))
            rest = max(0.0, min(nxt, tot) - cur)
            emit_at(min(gap_max_rate, max(r, rest / target_gap)) if target_gap > EPS else r,
                    target_gap, "gap")
            continue
        emit_at(1.0, dur - x, "speech")           # 発話の頭は等速のまま
        emit_at(r, x, "speech")                   # 末尾だけ速くする
        places.append((started, out_cur - started))
        emit_at(r, target_gap, "gap")
    # 末尾に残った素材は間と同じ扱いで流し切る
    if tot - cur > EPS:
        emit(tot, max(target_gap, (tot - cur) / gap_max_rate), "gap", gap_max_rate,
             extend=True)
    return Warp([s for s in segs if s.out_dur > EPS], places)


def _snap(dur: float, fps: int | None) -> float:
    """出力尺をフレームに丸める（最低1フレーム）。``fps`` が None ならそのまま。"""
    if not fps or dur <= EPS:
        return dur
    return max(1, round(dur * fps)) / fps


def _merge(spans) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted((float(x), float(y)) for x, y in (spans or ())):
        if out and a <= out[-1][1] + EPS:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _split_by_holds(a: float, b: float, holds) -> list[tuple[float, float]]:
    """``[a,b)`` を hold 区間の境界で割る（hold の内/外が混ざった区間を作らない）。"""
    if b - a <= EPS:
        return []
    cuts = {a, b}
    for ha, hb in holds:
        for c in (ha, hb):
            if a < c < b:
                cuts.add(c)
    xs = sorted(cuts)
    return [(xs[i], xs[i + 1]) for i in range(len(xs) - 1) if xs[i + 1] - xs[i] > EPS]


def _in_holds(a: float, b: float, holds) -> bool:
    mid = (a + b) / 2
    return any(ha - EPS <= mid <= hb + EPS for ha, hb in holds)
