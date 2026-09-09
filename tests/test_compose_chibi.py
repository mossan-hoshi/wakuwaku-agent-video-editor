"""compose_kept のちびキャラ統合テスト（ffmpeg は subprocess 注入で非実行）。"""

from __future__ import annotations

from pathlib import Path

from wwedit.compose import ffmpeg_compose
from wwedit.edl.schema import (
    ChibiConfig,
    Edl,
    Segment,
    SourceMedia,
    SpeakerTrack,
    Subtitle,
    Utterance,
    Word,
)


def _edl() -> Edl:
    return Edl(
        recording_dir="2026-01-01",
        source=SourceMedia(
            video_path="v.mp4", duration_s=60.0,
            audio_tracks=[
                SpeakerTrack(speaker="A", path="a.m4a"),
                SpeakerTrack(speaker="B", path="b.m4a"),
            ],
        ),
        segments=[Segment(id="s0", start=0.0, end=60.0)],
        utterances=[
            Utterance(speaker="A", text="こんにちは", start=1.0, end=2.0,
                      words=[Word(text="こんにちは", start=1.0, end=2.0)]),
        ],
        subtitles=[Subtitle(start=1.0, end=2.0, text="こんにちは", speaker="A")],
        character_cast={"A": "noa", "B": "suzu"},
        chibi=ChibiConfig(enabled=True),
    )


def _fake_assets(root: Path) -> None:
    for char in ("noa", "suzu"):
        d = root / char / "normal"
        d.mkdir(parents=True)
        (d / "mouth_closed.png").write_bytes(b"x")
        (d / "mouth_open.png").write_bytes(b"x")


def test_compose_kept_chibi_script(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WWEDIT_CHIBI_ASSETS", str(tmp_path / "assets"))
    _fake_assets(tmp_path / "assets")

    captured: dict = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = list(cmd)
        for i, a in enumerate(cmd):
            if a == "-filter_complex_script":
                captured["script"] = Path(cmd[i + 1]).read_text(encoding="utf-8")

        class R:
            returncode = 0
            stderr = ""
        return R()

    monkeypatch.setattr(ffmpeg_compose.subprocess, "run", fake_run)
    edl = _edl()
    ffmpeg_compose.compose_kept(
        edl, tmp_path / "out.mp4", audio="embedded", subtitles=True, chibi=True,
        chibi_fx=False,
    )
    cmd, script = captured["cmd"], captured["script"]
    # ffconcat 入力が左右2本（エフェクト無効時）
    assert cmd.count("concat") == 2 and cmd.count("-safe") == 2
    # スプライトは fps=30・既定高さ320・RGBA で、左右アンカーへ overlay
    # 左は対面させるため hflip、右は素のまま（既定 flip_sides=["left"]）
    assert "fps=30,scale=-1:320,hflip,format=rgba" in script
    assert "fps=30,scale=-1:320,format=rgba" in script
    assert script.count("hflip") == 1
    assert "overlay=24:H-h-24:eof_action=pass" in script
    assert "overlay=W-w-24:H-h-24:eof_action=pass" in script
    # レイヤー順: ちびキャラ → 字幕（ass）の順（字幕がちびの上）
    assert script.index("overlay=24:H-h-24") < script.index("ass=")


def test_compose_kept_chibi_fx_adds_own_tracks(tmp_path: Path, monkeypatch):
    """[E] エフェクトはちびとは**別トラック**（入力+2）で、字幕より下・hflip なし。"""
    monkeypatch.setenv("WWEDIT_CHIBI_ASSETS", str(tmp_path / "assets"))
    _fake_assets(tmp_path / "assets")

    captured: dict = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = list(cmd)
        for i, a in enumerate(cmd):
            if a == "-filter_complex_script":
                captured["script"] = Path(cmd[i + 1]).read_text(encoding="utf-8")

        class R:
            returncode = 0
            stderr = ""
        return R()

    monkeypatch.setattr(ffmpeg_compose.subprocess, "run", fake_run)
    edl = _edl()
    edl.utterances[0].emotion = "surprised"
    ffmpeg_compose.compose_kept(
        edl, tmp_path / "out.mp4", audio="embedded", subtitles=True, chibi=True,
        chibi_fx=True,
    )
    cmd, script = captured["cmd"], captured["script"]
    # ちび2本 + エフェクトは**出る側だけ**（この EDL は片方しか感情が付かない）。
    # 出ない側まで ffconcat を流すと、透明フレームを全長ぶんアルファ合成することになる。
    assert cmd.count("concat") == 3 and cmd.count("-safe") == 3
    assert script.count("format=rgba[cfx") == 1
    # 出ている区間だけ合成する（実測でエフェクトは全長の 0.2〜1.2% しか出ない）
    assert "eof_action=pass:enable='between(t," in script
    # ⚠️ エフェクトは反転しない（「!」「?」が鏡文字になる）。hflip は依然ちびの1回だけ。
    assert script.count("hflip") == 1
    # エフェクトは縦横比を変えない（scale を掛けず、生成時点の実寸で置く）
    assert "fps=30,format=rgba[cfx" in script
    # レイヤー順: ちび → エフェクト → 字幕
    import re
    cfx = re.search(r"\[cfx\d\]", script)
    assert cfx and script.index("[chb0]") < cfx.start() < script.index("ass=")


def test_compose_kept_chibi_off_keeps_script_clean(tmp_path: Path, monkeypatch):
    captured: dict = {}

    def fake_run(cmd, **kw):
        for i, a in enumerate(cmd):
            if a == "-filter_complex_script":
                captured["script"] = Path(cmd[i + 1]).read_text(encoding="utf-8")

        class R:
            returncode = 0
            stderr = ""
        return R()

    monkeypatch.setattr(ffmpeg_compose.subprocess, "run", fake_run)
    edl = _edl()
    ffmpeg_compose.compose_kept(edl, tmp_path / "out.mp4", audio="embedded")
    assert "chb" not in captured["script"]  # chibi=False では一切混ざらない
