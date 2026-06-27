"""[I] style字幕の ASS 生成（メイリオ・二重枠）。

二重枠は ASS 1イベント1アウトラインの制約を、**2レイヤー重ね**で表現する:
- Layer0(outer): 太いアウトライン（濃色）。外側の枠。
- Layer1(inner): 細いアウトライン（白）＋本文色（main=緑〜水色 / intro=ピンク）。
内側の白枠の外に外側の濃枠がはみ出る＝二重枠。最上位レイヤーとして焼き込む（[I]）。

色は ASS の ``&HAABBGGRR``（alpha,B,G,R の16進）。本文は要所のみ・イントロは全文（呼び出し側で
EDL.subtitles を用意）。pure 関数なのでフォント/解像度を変えてテスト可。
"""

from __future__ import annotations

import hashlib

from wwedit.edl.schema import Subtitle

__all__ = [
    "WHITE_RING", "MAIN_PALETTE", "COOL_KEYS", "WARM_KEYS", "INTRO_COLOR",
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
INTRO_COLOR = "&H008C3CE6"   # イントロ 文字＆外枠=ピンク RGB(230,60,140)・固定
SHADOW = "&H50000000"        # 半透明影（背景から浮かせる）


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


def build_ass(
    subtitles: list[Subtitle],
    *,
    color_map: dict[str, str] | None = None,
    default_color: str | None = None,
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
    （[[subtitle-speaker-colors]]）、style="intro" はピンク固定。color_map に無い話者は
    ``default_color``。使う色ごとに style を動的生成する。
    """
    cmap = color_map or {}
    default = default_color or MAIN_PALETTE["blue"]

    def color_of(sub: Subtitle) -> str:
        if sub.style == "intro":
            return INTRO_COLOR
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
