"""[V] 方式B: Whisper文字起こし→LLM整形→Qwen3-TTS読み上げで声を差し替える。

流れ（CLI は publish voice-tts-prepare / voice-tts / voice-tts-finalize）:

1. **prepare**: kept区間と交差する発話を ``voice_tts_input.tsv`` へ書き出す
   （``idx<TAB>speaker<TAB>char<TAB>slot_s<TAB>gap_s<TAB>text``）。
2. **voice-scripter スキル**（LLM）が読み上げ文へ整形 → ``voice_tts_decisions.json``。
3. **voice-tts**: ``qwen_tts.synth_batch`` で一括合成（既存wavスキップ＝分割実行・再開可）。
   行ごとに ``fit_plan`` で尺合わせを判定: **無加工 > atempo(≤1.12) > テキスト短縮(1周) >
   フリーズフレーム** の優先順。短縮が要る行は ``voice_tts_shorten.tsv`` を書いてスキル再実行を
   促す。全行確定したら ``voice_tts_report.json``。
4. **finalize**: report から ``EDL.freezes`` を確定し、**σ（stretched）タイムライン**上に
   全長トラックを組み立てて ``voice_path`` をセットする。発話がカット穴を跨ぐ場合は
   クリップを分割配置し、concat 後に切れ目なく繋がるようにする。

一文レベルで start/end が合っていれば違和感は出ない、という前提（ユーザー確認済み）。
笑い声などの非言語音は本方式では消える（仕様）。
"""

from __future__ import annotations

import json
import subprocess
import wave
from pathlib import Path

from wwedit.compose.ffmpeg_compose import (
    _split_ranges_at_freezes,
    _src_to_out,
    out_to_src,
    out_total,
)
from wwedit.edl.schema import (
    MAX_SEC_PER_CHAR,
    MIN_WORD_S,
    PUNCT_CHARS,
    Edl,
    Freeze,
    Subtitle,
    TimeRange,
    Utterance,
)

__all__ = [
    "TSV_NAME", "DECISIONS_NAME", "SHORTEN_NAME", "RECHECK_NAME", "REPORT_NAME",
    "SPEAKER_SIM_NAME",
    "TERMS_NAME", "SUB_LINE_CHARS", "MIN_SUB_DUR", "TURN_GAP_S", "CLIP_GAP",
    "SEC_PER_CHAR", "reading_rows",
    "eligible_utterances", "tts_units", "kept_text", "write_tts_input",
    "load_decisions", "load_terms", "apply_terms",
    "fit_plan", "schedule_clips", "out_to_sigma_segments",
    "place_clip", "wav_duration", "wrap_two_lines", "subtitles_from_reading",
    "resolve_overlaps",
]

TSV_NAME = "voice_tts_input.tsv"
DECISIONS_NAME = "voice_tts_decisions.json"
SHORTEN_NAME = "voice_tts_shorten.tsv"
#: 話者チェックを通らなかった行（＝台本の言い回しを見直す対象）。
RECHECK_NAME = "voice_tts_recheck.tsv"
#: 話者同一性の**全行**のスコア（合格した行も残す）。後から耳で「別人だ」と言われたときに
#: 「その行のスコアは幾つだったのか」を確かめられないと、指標を直しようがない。
SPEAKER_SIM_NAME = "speaker_sim.json"
REPORT_NAME = "voice_tts_report.json"
TERMS_NAME = "voice_tts_terms.json"

#: 尺合わせのパラメータ。atempo は 1.12 倍速まで（知覚されにくい範囲）。
ATEMPO_MAX = 1.12
FIT_MARGIN_S = 0.15

#: 方式Bの字幕は**読み上げ文そのまま**を2行で出す。1行の最大文字数（2行なので×2が1枚の上限）。
SUB_LINE_CHARS = 20
#: 重なり解消で打ち切った結果これ未満になった字幕は捨てる（一瞬だけ光る字幕を出さない）。
MIN_SUB_DUR = 0.4


def eligible_utterances(edl: Edl) -> list[tuple[int, Utterance]]:
    """TTS対象の発話（マイク話者・kept区間と交差するもの）を (元index, 発話) で返す。"""
    mic = {t.speaker for t in edl.source.audio_tracks if not t.is_desktop_audio}
    ranges = edl.kept_ranges()
    out: list[tuple[int, Utterance]] = []
    for i, u in enumerate(edl.utterances):
        if u.speaker not in mic or not (u.text or "").strip():
            continue
        os_, oe = _src_to_out(ranges, u.start), _src_to_out(ranges, u.end)
        if oe - os_ > 1e-3:
            out.append((i, u))
    return out


#: ターン分割の無音しきい値（秒）。これ以上の無音でユニットを切る。
TURN_GAP_S = 1.0
#: これより短いユニットは捨てる（息継ぎだけの断片を読み上げない）。
MIN_UNIT_S = 0.15


def _voiced_end(w) -> float:
    """word の**実際に声が出ている終端**を文字数から見積もる。

    Whisper の word タイミングは**隙間ゼロ**で、無音は句読点トークンが吸っている
    （§14.7-5 の罠）。そのため素の ``w.end`` で gap を測ってもターンは切れない。
    """
    core = "".join(c for c in (w.text or "") if c not in PUNCT_CHARS)
    if not core:
        return w.start
    return min(w.end, w.start + max(MIN_WORD_S, len(core) * MAX_SEC_PER_CHAR))


def tts_units(edl: Edl, *, gap: float = TURN_GAP_S) -> list[dict]:
    """読み上げの単位＝**ターン**を返す（``uid``/``u_idx``/``speaker``/``start``/``end``/``text``）。

    **utterance をそのまま読み上げ単位にしてはいけない。** 話者別 utterance は相槌を挟んで
    数十秒〜100秒超の塊になり、その中に相手のターンが丸ごと入る。塊のまま読み上げると
    2人が同時に喋り（実走で18組・106秒が重複）、直列化すると最大25.8秒ずれた。
    kept な word を無音で切って**実際のターン**に割り直す（実測 292ユニット・中央値1.5秒）。
    """
    mic = {t.speaker for t in edl.source.audio_tracks if not t.is_desktop_audio}
    ranges = edl.kept_ranges()
    raw: list[dict] = []
    for i, u in enumerate(edl.utterances):
        if u.speaker not in mic:
            continue
        words = [w for w in (u.words or [])
                 if any(r.start <= (w.start + w.end) / 2 <= r.end for r in ranges)]
        if not words:
            # word タイミングが無い発話は塊のまま1ユニット（判定材料が無いので分割できない）
            if (u.words or []) or not (u.text or "").strip():
                continue
            os_, oe = _src_to_out(ranges, u.start), _src_to_out(ranges, u.end)
            if oe - os_ > 1e-3:
                raw.append({"u_idx": i, "speaker": u.speaker, "start": u.start,
                            "end": u.end, "text": " ".join(u.text.split())})
            continue
        # 無音（前の word の**声の終端**から次の word の頭まで）が gap を超えたら切る
        groups: list[list] = [[words[0]]]
        end_of = [_voiced_end(words[0])]
        for w in words[1:]:
            if w.start - end_of[-1] > gap:
                groups.append([w])
                end_of.append(_voiced_end(w))
            else:
                groups[-1].append(w)
                end_of[-1] = max(end_of[-1], _voiced_end(w))
        for g, g_end in zip(groups, end_of, strict=True):
            text = "".join((w.text or "") for w in g).strip()
            if text and g_end - g[0].start >= MIN_UNIT_S:
                raw.append({"u_idx": i, "speaker": u.speaker, "start": g[0].start,
                            "end": g_end, "text": text})

    raw.sort(key=lambda x: (x["start"], x["speaker"]))

    # **相手が挟まっていない同一話者の連続ユニットは繋ぎ直す。**
    # 無音だけで切ると文の途中で割れる（実走で「こ」「れ、えーと…」に分断された）。
    # 分ける目的は相手のターンと正しく噛み合わせることなので、間に相手が居ないなら分けない。
    merged: list[dict] = []
    for un in raw:
        if merged and merged[-1]["speaker"] == un["speaker"]:
            prev = merged[-1]
            prev["end"] = max(prev["end"], un["end"])
            prev["text"] = (prev["text"] + un["text"]).strip()
            continue
        merged.append(dict(un))

    for uid, unit in enumerate(merged):
        unit["uid"] = uid
    return merged


#: 1クリップ＝1文。**これより短い文は隣とくっつける**（材料が短いと声質が安定しない）。
SENT_MIN_CHARS = 6

#: 1文の長さの上限（字）。**超えるとそこだけ粒度がターン単位に戻る**。
#: 実走で1文203字（約27秒）の台詞があり、文単位にした意味が消えていた（2026-08-06）。
#: 字幕も1枚40字で割れるので、長い1文は字幕とも噛み合わない。
SENT_MAX_CHARS = 60

#: 文の終わり。``.`` は「リリア3.5」を割ってしまうので**入れない**。
SENT_END_CHARS = "。！？!?"


def split_sentences(text: str, *, min_chars: int = SENT_MIN_CHARS) -> list[str]:
    """読み上げ文を**文ごと**に割る。**合成の単位＝後処理の単位**にするため。

    ターン丸ごとを1本のwavにすると、3文入りの行で「1文目だけ別人の声」になっても
    クリップ平均に薄まって検出できず、直すときも**丸ごと引き直し**になる（実走で
    idx=142「まあまあ、意味わかんないですね」がこれ）。感情・口パク・字幕も
    ターン単位でしか付けられない。文で割ると、悪い1文だけ引き直せて、表情も字幕も
    文ごとに付く（2026-08-06 ユーザー指摘）。

    短すぎる断片は**次の文へくっつける**（最後なら前へ）。「はい。」だけのクリップは
    材料が1秒に満たず、参照音声に寄せきれずスコアが暴れるため。
    """
    text = " ".join((text or "").split())
    if not text:
        return []
    raw: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in SENT_END_CHARS:
            raw.append(buf)
            buf = ""
    if buf.strip():
        raw.append(buf)

    strip_chars = SENT_END_CHARS + "、，,  "
    out: list[str] = []
    carry = ""
    for part in raw:
        # 「！？」の後半のような**記号だけの断片は前の文の末尾へ**（次へ送ると
        # 「？すごいですね」のように次の文の頭に記号が付く）
        if not part.strip(strip_chars) and not carry and out:
            out[-1] = out[-1] + part
            continue
        part = carry + part
        carry = ""
        # 長さは**記号を除いた中身**で測る（句読点で水増ししない）
        if len(part.strip(strip_chars)) < min_chars:
            carry = part
            continue
        out.append(part.strip())
    if carry.strip():
        if out:
            out[-1] = (out[-1] + carry).strip()
        else:
            out.append(carry.strip())
    return [p for p in out if p]


def clip_name(uid: int, sub: int, n_sub: int) -> str:
    """クリップのファイル名。**1文だけのターンは従来どおり ``u0017.wav``**。

    文分割を入れる前に合成済みのクリップを、名前を変えずにそのまま再利用するため。
    """
    return f"u{uid:04d}.wav" if n_sub <= 1 else f"u{uid:04d}_{sub:02d}.wav"


def tts_clips(
    units: list[dict], decisions: dict[int, str], *, min_chars: int = SENT_MIN_CHARS,
) -> list[dict]:
    """ターンを**文ごとのクリップ**へ割る（合成・話者チェック・字幕・感情の共通単位）。

    ``decisions`` は uid → 読み上げ文。**空文字はそのターンを読まない**（スキルが隣へ
    文をまとめた）、**キー無しは未決定**なので元テキストで合成する。
    """
    out: list[dict] = []
    for un in units:
        uid = un["uid"]
        if uid in decisions:
            text = decisions[uid]
            if not text:
                continue
        else:
            text = " ".join(un["text"].split())
        sents = split_sentences(text, min_chars=min_chars)
        for j, s in enumerate(sents):
            out.append({
                "uid": uid, "sub": j, "n_sub": len(sents), "text": s,
                # 台帳・話者チェックの見出しに使う一意キー（``"17"`` / ``"17.1"``）
                "key": str(uid) if len(sents) <= 1 else f"{uid}.{j}",
                "speaker": un["speaker"], "u_idx": un["u_idx"],
                "start": un["start"], "end": un["end"],
                "wav": clip_name(uid, j, len(sents)),
                "first": j == 0, "last": j == len(sents) - 1,
            })
    return out


def long_sentences(
    clips: list[dict], *, max_chars: int = SENT_MAX_CHARS,
) -> list[dict]:
    """**長すぎる1文**を拾う（台本の見直し対象）。

    スキルに「60字を超えない」と書いてあっても書き手は見落とす。合成に入る前に
    ここで数えて警告する（読み上げの単位が文なので、長い1文だけ粒度が粗くなる）。
    """
    return [c for c in clips if len(c["text"]) > max_chars]


def kept_text(u: Utterance, ranges: list[TimeRange]) -> str:
    """**カット後に残った word だけ**を繋いだ発話テキストを返す。

    utterance は数十秒の塊なので、kept 区間と少しでも交差すれば対象になる。そこで
    ``u.text``（元の全文）をそのまま台本の材料にすると、**カットしたはずの内容が
    読み上げに混ざる**（実走で「今日は20分ぐらいで抜けます」が残って発覚。558 word 中
    414 しか残っていない発話だった）。word 単位で kept 判定して組み直す。

    word タイミングが無い発話は元テキストへフォールバックする（判定材料が無い）。
    """
    words = u.words or []
    if not words:
        return " ".join((u.text or "").split())
    kept = [w.text for w in words
            if any(r.start <= (w.start + w.end) / 2 <= r.end for r in ranges)]
    return "".join(kept).strip()


def write_tts_input(edl: Edl, out_tsv: Path) -> int:
    """**ターン単位**のTSV（idx/speaker/char/slot_s/gap_s/text）を書き出し、行数を返す。

    ``idx`` は ``tts_units`` の ``uid``（開始順の通し番号）。
    ``slot_s`` = そのターンの kept 実尺。``gap_s`` = 次のターン（どの話者でも）開始までの余白。
    ``text`` は **kept 区間に残った word だけ**＝カット済み内容を台本へ渡さない。
    """
    ranges = edl.kept_ranges()
    total = out_total(ranges)
    units = tts_units(edl)
    lines = ["idx\tspeaker\tchar\tslot_s\tgap_s\ttext"]
    for k, un in enumerate(units):
        os_ = _src_to_out(ranges, un["start"])
        oe = _src_to_out(ranges, un["end"])
        nxt = _src_to_out(ranges, units[k + 1]["start"]) if k + 1 < len(units) else total
        char = edl.character_cast.get(un["speaker"], "")
        text = " ".join(un["text"].split())
        lines.append(f"{un['uid']}\t{un['speaker']}\t{char}\t"
                     f"{max(0.0, oe - os_):.2f}\t{max(0.0, nxt - oe):.2f}\t{text}")
    out_tsv.parent.mkdir(parents=True, exist_ok=True)
    out_tsv.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(units)


def load_decisions(path: Path, *, names: dict[str, str] | None = None) -> dict[int, str]:
    """voice-scripter の決定JSON ``{"lines":[{"idx","text"}]}`` を読み込む。

    **空文字はそのまま返す**（= そのターンは読み上げないという意図）。ターン分割は
    相手の発話をまたいで文を割ることがあるので、スキルが隣のターンへ文をまとめて
    こちらを空にできるようにしてある。**キーが無い** ターンだけが「未決定」。

    ここで **人名を ``.env`` のマップで置換**する（``WWEDIT_SUBTITLE_NAME_MAP``・
    漢字→カタカナ等）。読み上げ文は合成にも字幕にも使われるので、**唯一の入口である
    ここで潰す**のが確実（要約字幕は元から置換していたのに、方式Bの読み上げ字幕だけ
    素通りして漢字の実名が出た＝2026-08-05 ユーザー指摘）。カタカナにしておくと
    TTS の誤読も防げるので、読み上げ側にとっても正しい。
    """
    from wwedit.privacy.masking import apply_name_replacements

    nmap = name_replacements() if names is None else names
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[int, str] = {}
    for row in data.get("lines", []):
        if "idx" not in row:
            continue
        text = (row.get("text") or "").strip()
        out[int(row["idx"])] = apply_name_replacements(text, nmap) if nmap else text
    return out


def load_terms(path: Path) -> list[tuple[str, str]]:
    """用語表記の対応表 ``{"terms":[{"read","display"}]}`` を読む（長い読みから順に返す）。

    方式Bは**読みと表記の要求が逆**になる:
    - **読み上げ**は誤読させないためカタカナで書く（``Lyria 3.5`` → ``リリア3.5``）
    - **字幕**は正式表記で出す（``リリア3.5`` → ``Lyria 3.5``）

    正しい表記は**画面OCR**（`screen_text.txt`）から取る（[[chapter-proper-nouns-need-ocr]]）。
    STTの聞き取りに引きずられると表記ゆれが出る。
    """
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    pairs = [(str(t["read"]), str(t["display"]))
             for t in data.get("terms", []) if t.get("read") and t.get("display")]
    return sorted(pairs, key=lambda p: -len(p[0]))   # 長い読みを先に当てる


def apply_terms(text: str, terms: list[tuple[str, str]]) -> str:
    """読み上げ文の**カタカナ表記を正式表記へ戻す**（字幕用）。"""
    for read, display in terms:
        text = text.replace(read, display)
    return text


def fit_plan(
    slot_s: float, gap_s: float, tts_s: float, *,
    shorten_tried: bool = False, atempo_max: float = ATEMPO_MAX,
    margin: float = FIT_MARGIN_S,
) -> dict:
    """1発話の尺合わせ判定。優先順: 無加工 > atempo > テキスト短縮(1周) > フリーズ。

    返り値 ``{"action", "atempo", "extra", "budget"}``。
    - keep:   そのまま（budget = slot+gap-margin 内。余りは無音）
    - atempo: ``atempo`` 倍速で budget ぴったりに収める（≤ atempo_max）
    - shorten: テキストを budget 相当へ短縮すべき（スキル再実行）
    - freeze: ``extra`` 秒のフリーズフレームで動画側を伸ばす（音声は無加工）
    """
    budget = max(0.1, slot_s + gap_s - margin)
    if tts_s <= budget:
        return {"action": "keep", "atempo": 1.0, "extra": 0.0, "budget": budget}
    ratio = tts_s / budget
    if ratio <= atempo_max:
        return {"action": "atempo", "atempo": round(ratio, 4), "extra": 0.0, "budget": budget}
    if not shorten_tried:
        return {"action": "shorten", "atempo": 1.0, "extra": 0.0, "budget": budget}
    return {"action": "freeze", "atempo": 1.0,
            "extra": round(tts_s - budget, 3), "budget": budget}


#: 発話どうしの間（秒）。**最小値ではなく固定値**。
CLIP_GAP = 0.15

#: 読み上げ尺の見積り（秒/字）。Qwen3-TTS の実測値（4788字→643秒）。
#: 合成前に仮字幕を置く時と、生成の尺ヒントに使う。
SEC_PER_CHAR = 0.134


def schedule_clips(
    items: list[tuple[float, float, object]], *, gap: float = CLIP_GAP,
    hold_spans: list[tuple[float, float]] = (),
    src_ends: dict | None = None,
) -> list[tuple[float, float, object]]:
    """クリップを**必ず重ならないように直列化**する。間は ``gap`` 固定。

    ``[(希望開始, 尺, key)]`` → ``[(実開始, 尺, key)]``。

    ⚠️ **台詞は絶対に重ねない。**「相槌だけ相手の声に重ねる」は**却下済みの設計**で、
    二度と実装しないこと（ユーザー指示・複数回）。重なりは配置で解く問題ではなく、
    **台本の時点でターン（タイミング）を加味して書くことで起きなくする**。
    重ねる引数（``overlay`` / ``speaker_of``）は意図的に**存在しない**。

    ⚠️ 間は**固定**。`max(希望位置, 前の終わり + gap)` にすると、元の会話の長い沈黙が
    そのまま残って「間がぐちゃぐちゃ」になる（ユーザー指摘）。前の終わりから ``gap`` 秒後に
    置く。映像側は `timewarp` がこの並びに合わせて可変速で追従する。

    唯一の例外が ``hold_spans``（＝**PCシステム音が鳴っている素材区間**）。デモの音が
    そのまま鳴っているので、そこは**鳴っている長さぶんだけ待つ**（間 = ``gap`` ＋
    その区間で実際に鳴っている秒数）。判定には ``src_ends``（key → 元の発話終端の src' 秒）が要る。

    ⚠️ **「hold に掛かったら元の間隔をまるごと残す」ではない**。実測（#103）では、
    長い間 147.0秒のうち**実際に鳴っていたのは 16.8秒だけ**で、32.0秒や22.6秒の間が
    「PC音声のせい」に見えて中身はただの沈黙だった。それを残すと出力が113秒膨らむ。
    """
    holds = _merge_spans(hold_spans)
    ends = src_ends or {}
    out: list[tuple[float, float, object]] = []
    prev_end: float | None = None
    prev_src_end: float | None = None
    # 同じ希望位置のもの（＝**同じターンを割った文どうし**）は**入力順**のまま。
    # 尺を第2キーにすると短い文が先に出て、台詞の順番が入れ替わる。
    for want, dur, key in sorted(items, key=lambda x: x[0]):
        if prev_end is None:
            start = want
        else:
            # この2発話のあいだに実際に鳴っている秒数だけ足す
            a = prev_src_end if prev_src_end is not None else want
            start = prev_end + gap + _span_overlap(a, want, holds)
        out.append((start, dur, key))
        prev_end = start + dur
        prev_src_end = float(ends.get(key, want + dur))
    return out


def _merge_spans(spans) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted((float(x), float(y)) for x, y in (spans or ())):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _span_overlap(a: float, b: float, spans: list[tuple[float, float]]) -> float:
    """``[a, b)`` が ``spans`` と重なる合計秒数。"""
    if b <= a:
        return 0.0
    return sum(max(0.0, min(b, y) - max(a, x)) for x, y in spans)


def reading_rows(
    edl: Edl, decisions: dict[int, str], *, sec_per_char: float = SEC_PER_CHAR,
) -> list[dict]:
    """**合成前**に、読み上げ文の文字数から尺を見積もった仮の配置行を作る。

    G2（編集確認）で字幕の内容を確認できるようにするため。本番の配置は合成後に
    実尺で決まる（`voice-tts` → `voice-tts-finalize`）ので、ここは**内容の確認用**。
    """
    ranges = edl.kept_ranges()
    clips = tts_clips(tts_units(edl), decisions)
    items = [(_src_to_out(ranges, c["start"]), len(c["text"]) * sec_per_char, i)
             for i, c in enumerate(clips)]
    rows = []
    for start, dur, i in schedule_clips(items):
        c = clips[i]
        rows.append({"idx": c["uid"], "sub": c["sub"], "speaker": c["speaker"],
                     "text": c["text"], "out_start": round(start, 3),
                     "tts_s": round(dur, 3), "atempo": 1.0})
    return rows


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as wf:
        return wf.getnframes() / wf.getframerate()


def wrap_two_lines(text: str, line_chars: int = SUB_LINE_CHARS) -> str:
    """1枚の字幕を**最大2行**へ折り返す（``\\n`` 区切り）。

    ASS は ``WrapStyle: 2``（自動折返しなし）なので、改行はここで明示的に入れる。
    句読点の直後で割れるならそこで、無ければ中央付近で機械的に割る。
    """
    text = " ".join(text.split())
    if len(text) <= line_chars:
        return text
    half = len(text) / 2
    best, best_d = None, None
    for i, ch in enumerate(text):
        if ch in "、。！？!?，,." and 0 < i + 1 < len(text):
            d = abs((i + 1) - half)
            if best_d is None or d < best_d:
                best_d, best = d, i + 1
    cut = best if best is not None and max(best, len(text) - best) <= line_chars else None
    if cut is None:
        cut = min(line_chars, max(1, int(round(half))))
    return text[:cut].strip() + "\n" + text[cut:].strip()


def name_replacements() -> dict[str, str]:
    """字幕の人名表記マップ（``.env`` の ``WWEDIT_SUBTITLE_NAME_MAP``・漢字→カタカナ等）。

    秘匿情報なのでコードに直書きしない（[[pii-masking-and-ocr-engine]]）。
    要約字幕（`subtitle.summarize`）は元から適用していたが、方式Bの読み上げ字幕が
    素通りしていて漢字の実名が出てしまった（2026-08-05 ユーザー指摘）。
    """
    from wwedit.privacy.masking import load_name_replacements

    return load_name_replacements()


def subtitles_from_reading(
    rows: list[dict], decisions: dict[int, str], ranges: list[TimeRange],
    freezes=(), *, line_chars: int = SUB_LINE_CHARS,
    terms: list[tuple[str, str]] | None = None,
    names: dict[str, str] | None = None,
) -> list[Subtitle]:
    """読み上げ文（＝実際に喋る内容）から**ソース時刻の2行字幕**を作る。

    方式Bでは正確な発話内容が確定しているので、Whisper由来の字幕ではなく読み上げ文を
    そのまま出す。字幕は「読み上げクリップが**実際に鳴っている出力区間**」（＝直列
    スケジュール後の ``out_start``）に比例配分し、``out_to_src`` でソース時刻へ戻す
    （EDL.subtitles はソース時刻が正・非破壊）。
    フリーズ中はソース時刻が進まないので、最後の1枚が ``_src_to_out`` 側で自動的に延びる。

    ``names`` 未指定なら ``.env`` の人名マップを自動で読む（渡さない実装ミスを防ぐ）。
    """
    from wwedit.privacy.masking import apply_name_replacements
    from wwedit.subtitle.build import split_text

    nmap = name_replacements() if names is None else names
    out: list[Subtitle] = []
    for r in rows:
        # 行が持つ読み上げ文が正（**1行＝1文＝1クリップ**）。`decisions` 参照は
        # 文分割を入れる前の古い report を読んだ時のフォールバック。
        text = (r.get("text") or decisions.get(int(r["idx"]), "")).strip()
        if not text:
            continue
        # 人名は .env のマップで置換（漢字の実名を字幕に出さない）→ そのあと
        # 字幕を**正式表記**へ戻す（読み上げ用のカタカナ→表記）
        text = apply_name_replacements(text, nmap) if nmap else text
        text = apply_terms(text, terms) if terms else text
        parts = split_text(text, max_chars=line_chars * 2)
        if not parts:
            continue
        clip_dur = float(r["tts_s"]) / max(1.0, float(r.get("atempo") or 1.0))
        out_at = (float(r["out_start"]) if "out_start" in r
                  else _src_to_out(ranges, float(r["u_start"]), freezes))
        n_chars = sum(len(p) for p in parts) or 1
        pos = out_at
        for p in parts:
            end = pos + clip_dur * (len(p) / n_chars)
            s = out_to_src(ranges, pos, freezes)
            e = out_to_src(ranges, end, freezes)
            if e - s > 1e-3:
                out.append(Subtitle(start=round(s, 3), end=round(e, 3),
                                    text=wrap_two_lines(p, line_chars),
                                    style="main", speaker=r["speaker"]))
            pos = end
    out.sort(key=lambda s: s.start)
    return resolve_overlaps(out)


def resolve_overlaps(subs: list[Subtitle], *, min_dur: float = MIN_SUB_DUR) -> list[Subtitle]:
    """時間の重なりを解消して**同時に1枚だけ**が出るようにする（start昇順の入力）。

    話者別 utterance は互いに大きく重なる（相槌を挟みながら喋るので、片方の塊の中に
    もう片方の塊が丸ごと入る）。そのまま字幕にすると画面下部で2人分が重なるので、
    **後から始まる字幕が勝ち**、前の字幕はその開始時刻で打ち切る。
    打ち切って ``min_dur`` 未満になった字幕は捨てる（一瞬だけ光る字幕を出さない）。

    音声は両方鳴ったままなので、消えるのは「読めない側の文字」だけ＝非破壊の方針を崩さない。
    """
    out: list[Subtitle] = []
    for i, s in enumerate(subs):
        end = s.end
        for nxt in subs[i + 1:]:
            if nxt.start >= end:
                break
            end = min(end, nxt.start)
        if end - s.start >= min_dur:
            out.append(s.model_copy(update={"end": round(end, 3)}))
    return out


def apply_atempo(src: Path, dst: Path, atempo: float) -> Path:
    """atempo で倍速をかけたクリップを書き出す（1.0 はコピー相当）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-filter:a", f"atempo={atempo:g}", str(dst)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"atempo 失敗 {src.name}:\n{(proc.stderr or '')[-500:]}")
    return dst


def out_to_sigma_segments(
    ranges: list[TimeRange], freezes: list[Freeze]
) -> list[tuple[float, float, float]]:
    """最終出力タイムライン→σ（stretched ソース）座標の区分線形マップを作る。

    返り値 ``[(out_start, out_end, sigma_start), ...]``（傾き1）。フリーズ区間は直前
    piece の末尾に含まれる（映像 tpad と同じ構造）ので、フリーズ中に流れる音声は
    σ 座標でその位置に置けばよい。
    """
    segs: list[tuple[float, float, float]] = []
    out_pos = 0.0
    cum = 0.0
    for r, extra in _split_ranges_at_freezes(ranges, freezes):
        d = (r.end - r.start) + extra
        segs.append((out_pos, out_pos + d, r.start + cum))
        out_pos += d
        cum += extra
    return segs


def place_clip(
    segs: list[tuple[float, float, float]], out_at: float, clip_dur: float,
) -> list[tuple[float, float, float]]:
    """出力タイムライン ``[out_at, out_at+clip_dur)`` を占めるクリップの配置を返す。

    返り値 ``[(clip_offset, sigma_pos, dur), ...]``。カット穴・フリーズを跨ぐ場合は
    複数 piece になり、concat 後に切れ目なく繋がる。セグメント範囲外に食み出した分は
    捨てられる（呼び出し側が budget/freeze で防ぐ）。
    """
    out: list[tuple[float, float, float]] = []
    end = out_at + clip_dur
    for os_, oe, sig in segs:
        a, b = max(out_at, os_), min(end, oe)
        if b - a <= 1e-6:
            continue
        out.append((a - out_at, sig + (a - os_), b - a))
    return out
