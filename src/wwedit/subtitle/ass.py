"""[I] style字幕の ASS 生成（メイリオ・二重枠）。

二重枠は ASS 1イベント1アウトラインの制約を、**2レイヤー重ね**で表現する:
- Layer0(outer): 太いアウトライン（濃色）。外側の枠。
- Layer1(inner): 細いアウトライン（白）＋本文色（main=緑〜水色 / intro=ピンク）。
内側の白枠の外に外側の濃枠がはみ出る＝二重枠。最上位レイヤーとして焼き込む（[I]）。

色は ASS の ``&HAABBGGRR``（alpha,B,G,R の16進）。本文は要所のみ・イントロは全文（呼び出し側で
EDL.subtitles を用意）。pure 関数なのでフォント/解像度を変えてテスト可。
"""

from __future__ import annotations

import colorsys
import hashlib
import re

from wwedit.edl.schema import Subtitle

__all__ = [
    "WHITE_RING", "MAIN_PALETTE", "COOL_KEYS", "WARM_KEYS", "INTRO_COLOR",
    "CHARACTER_COLORS", "intro_color_for",
    "CHAR_THEME_HEX", "hex_to_ass", "ass_to_rgb", "ensure_legible",
    "char_subtitle_color", "resolve_color_key",
    "is_warm_speaker", "assign_speaker_colors", "pick_main_color",
    "ass_time", "ass_escape", "build_ass",
]

# &HAABBGGRR（AA=00で不透明）。二重枠の構造（[[subtitle-double-border-spec]]）:
# inner=色付きの文字そのもの / 文字とouterの間の1次枠線=**白固定** / outer=innerと同色の外枠。
# → 内側から「色の文字 → 白枠 → 同色の外枠」。変えるのは色だけ（白は固定）。
WHITE_RING = "&H00FFFFFF"   # 1次枠線=白（固定）
MAIN_PALETTE = {
    "red": "&H002828C8",     # RGB(200,40,40) 暖色
    "purple": "&H00B43C78",  # RGB(120,60,180) 暖色寄り
    "blue": "&H00C85A1E",    # RGB(30,90,200) 寒色
    "green": "&H00379614",   # RGB(20,150,55) 深緑(g2) 寒色
}
# 話者で色を分ける（[[subtitle-speaker-colors]]）: sakamoto/mossan-hoshi=寒色, taniguchi=暖色。
COOL_KEYS = ["blue", "green"]
WARM_KEYS = ["red", "purple"]
INTRO_COLOR = "&H008C3CE6"   # イントロ既定＝ピンク RGB(230,60,140)（noa の既存イントロの色）
# イントロ字幕は**喋るキャラの配色**に合わせる（2026-08-03 ユーザー指示）。
# 根拠は mascot.md の「絵柄」の配色。ここに無いキャラは ``INTRO_COLOR``（ピンク）へ落ちる。
CHARACTER_COLORS = {
    "noa": INTRO_COLOR,             # 立ち姿素材がネオンピンク/パープル系
    "yume": "&H00AA5AF0",           # ピンク×ブラック RGB(240,90,170)
    "suzu": "&H002D6EB4",           # ハニーブラウン RGB(180,110,45)
    "ritsu": "&H006E3723",          # ネイビー×真鍮 RGB(35,55,110)
    "reika": "&H00A57D5A",          # スモーキーブルー RGB(90,125,165)
    "souta": "&H002D23A0",          # ジェットブラック×ダークレッド RGB(160,35,45)
}
SHADOW = "&H50000000"        # 半透明影（背景から浮かせる）

# のべつべキャラのテーマカラー（novtube web/shared/constants/voiceCloud.ts 準拠・
# 立ち絵から手動キュレーションされた値。priya は LP 側と drift があるが voiceCloud を正とする）。
# 本編字幕のキャラ声差し替え時（EDL.character_cast）に subtitle_speaker_colors の値として
# キャラid を書けるよう、resolve_color_key がここも解決する。
# つくよみちゃん(tsukuyomi)は**のべつべオリジナルではない**外部キャラなので意図的に含めない
# （voice_cast.NON_ORIGINAL_CHARS でも明示的に弾いている）。
CHAR_THEME_HEX = {
    "noa": "#3FA9B5",
    "tsukasa": "#4A78A6",
    "yume": "#EC4899",
    "kasumi": "#6B0716",
    "reika": "#5B7E9C",
    "ritsu": "#8B1C2C",
    "souta": "#B81E2D",
    "suzu": "#A26B3A",
    "priya": "#E0701F",
}

_HEX6_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


def hex_to_ass(hex_str: str) -> str:
    """``#RRGGBB`` → ASS ``&H00BBGGRR``。"""
    m = _HEX6_RE.match(hex_str.strip())
    if not m:
        raise ValueError(f"不正なhex色: {hex_str!r}")
    r, g, b = (int(m.group(1)[i : i + 2], 16) for i in (0, 2, 4))
    return f"&H00{b:02X}{g:02X}{r:02X}"


def ass_to_rgb(ass_color: str) -> tuple[int, int, int]:
    """ASS ``&HAABBGGRR`` → (r, g, b)。"""
    m = re.match(r"^&H([0-9a-fA-F]{8})$", ass_color.strip())
    if not m:
        raise ValueError(f"不正なASS色: {ass_color!r}")
    v = m.group(1)
    b, g, r = (int(v[i : i + 2], 16) for i in (2, 4, 6))
    return r, g, b


def ensure_legible(hex_str: str, *, min_l: float = 0.35, target_l: float = 0.45) -> str:
    """暗すぎるテーマ色の明度だけを引き上げて字幕文字色として読めるようにする。

    kasumi(#6B0716) 等の暗色は白背景の白1次枠と合わせても沈むので、HLS の L が
    ``min_l`` 未満なら ``target_l`` へリフトする（色相・彩度は維持）。明るい色は不変。
    """
    m = _HEX6_RE.match(hex_str.strip())
    if not m:
        raise ValueError(f"不正なhex色: {hex_str!r}")
    r, g, b = (int(m.group(1)[i : i + 2], 16) / 255 for i in (0, 2, 4))
    hue, lightness, sat = colorsys.rgb_to_hls(r, g, b)
    if lightness >= min_l:
        return f"#{int(r*255):02X}{int(g*255):02X}{int(b*255):02X}"
    r2, g2, b2 = colorsys.hls_to_rgb(hue, target_l, sat)
    return f"#{int(round(r2*255)):02X}{int(round(g2*255)):02X}{int(round(b2*255)):02X}"


def char_subtitle_color(char: str) -> str | None:
    """キャラid → 本編字幕用 ASS 色（テーマ色に明度補正をかけたもの）。未知は None。"""
    hex_c = CHAR_THEME_HEX.get((char or "").strip().lower())
    return hex_to_ass(ensure_legible(hex_c)) if hex_c else None


def resolve_color_key(key: str) -> str | None:
    """subtitle_speaker_colors の値を ASS 色へ解決する。

    受けるもの: パレットキー(red/purple/blue/green) / キャラid(noa/suzu/...) / ``#RRGGBB``。
    キャラidは明度補正込み、生hexは指定通り（明示指定を尊重）。未知は None（呼び出し側で無視）。
    """
    k = (key or "").strip()
    if k in MAIN_PALETTE:
        return MAIN_PALETTE[k]
    c = char_subtitle_color(k)
    if c:
        return c
    if _HEX6_RE.match(k):
        return hex_to_ass(k)
    return None


def is_warm_speaker(name: str) -> bool:
    """暖色にする話者か（taniguchi系=暖色 / sakamoto・mossan-hoshi等=寒色）。"""
    return "tanig" in (name or "").lower()


def assign_speaker_colors(speakers: list[str], key: str) -> dict[str, str]:
    """話者→ASS色を割当てる。暖色/寒色の各ペアから ``key`` のハッシュで決定的に選ぶ。

    同一動画(同一key)では同一話者は常に同色。動画ごとにペア内のどの色かは変わる。
    """
    out: dict[str, str] = {}
    for sp in sorted(set(speakers)):
        group = WARM_KEYS if is_warm_speaker(sp) else COOL_KEYS
        h = int(hashlib.sha1(f"{key}|{sp}".encode()).hexdigest(), 16)
        out[sp] = MAIN_PALETTE[group[h % len(group)]]
    return out


def pick_main_color(key: str) -> str:
    """話者不明時のフォールバック色（4色からkeyで決定的に1色）。"""
    names = sorted(MAIN_PALETTE)
    h = int(hashlib.sha1(key.encode("utf-8")).hexdigest(), 16)
    return MAIN_PALETTE[names[h % len(names)]]


def ass_time(t: float) -> str:
    """秒 → ASS タイム ``H:MM:SS.cs``（センチ秒）。"""
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    cs = int(round((t - int(t)) * 100))
    if cs == 100:  # 丸め桁上がり
        cs = 0
        s += 1
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ass_escape(text: str) -> str:
    """ASS 本文用エスケープ。改行は ``\\N``、波括弧はオーバーライド誤認を避けて全角化。"""
    return (
        text.replace("\\", "\\\\")
        .replace("\n", "\\N")
        .replace("{", "｛")
        .replace("}", "｝")
    )


def _style_line(
    name: str, fill: str, outline_col: str, outline: float, font: str, size: int
) -> str:
    # Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,
    #   Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,
    #   Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
    return (
        f"Style: {name},{font},{size},{fill},{fill},{outline_col},{SHADOW},"
        f"-1,0,0,0,100,100,0,0,1,{outline:g},0,2,80,80,70,1"
    )


def intro_color_for(char: str | None) -> str:
    """キャラIDからイントロ字幕の色を引く（未登録は既定のピンク）。"""
    return CHARACTER_COLORS.get((char or "").strip().lower(), INTRO_COLOR)


def build_ass(
    subtitles: list[Subtitle],
    *,
    color_map: dict[str, str] | None = None,
    default_color: str | None = None,
    intro_color: str | None = None,
    font: str = "Meiryo",
    size: int = 64,
    play_w: int = 1920,
    play_h: int = 1080,
    white_ring: float = 5.0,
    outer_outline: float = 9.0,
) -> str:
    """EDL.subtitles から**二重枠** ASS を生成する（[[subtitle-double-border-spec]]）。

    構造＝内側から「色の文字 → 白枠 → 同色の外枠」。ASSは1イベント1アウトラインなので **2レイヤー**:
    L0(下)=色fill＋太い色アウトライン → 外枠 / L1(上)=色fill＋白アウトライン → 色文字＋白1次枠線。

    色は**話者ごと**（``color_map``: 話者名→ASS色）。本編字幕は喋っている人の色
    （[[subtitle-speaker-colors]]）。style="intro" は ``intro_color``＝**喋るキャラの配色**
    （``intro_color_for(char)`` で引く・未指定は既定のピンク）。color_map に無い話者は
    ``default_color``。使う色ごとに style を動的生成する。
    """
    cmap = color_map or {}
    default = default_color or MAIN_PALETTE["blue"]
    intro = intro_color or INTRO_COLOR

    def color_of(sub: Subtitle) -> str:
        if sub.style == "intro":
            return intro
        return cmap.get(sub.speaker or "", default)

    # 使う色を集めて色ごとに style ペア（cNL0/cNL1）を作る
    used: list[str] = []
    for sub in subtitles:
        c = color_of(sub)
        if c not in used:
            used.append(c)
    sid = {c: f"c{i}" for i, c in enumerate(used)}

    head = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 2",
        f"PlayResX: {play_w}",
        f"PlayResY: {play_h}",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
    ]
    for c, s in sid.items():
        head += [
            _style_line(f"{s}L0", c, c, outer_outline, font, size),
            _style_line(f"{s}L1", c, WHITE_RING, white_ring, font, size),
        ]
    head += [
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    events: list[str] = []
    for sub in subtitles:
        st, en = ass_time(sub.start), ass_time(sub.end)
        txt = ass_escape(sub.text)
        s = sid[color_of(sub)]
        for layer in (0, 1):
            events.append(f"Dialogue: {layer},{st},{en},{s}L{layer},,0,0,0,,{txt}")
    return "\n".join(head + events) + "\n"
