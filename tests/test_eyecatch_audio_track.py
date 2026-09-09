"""アイキャッチは jingle が無くても**音声トラックを必ず持つ**。

由来: 2026-09-08。キャラの一言ボイスを機能ごと削除したとき、jingle を渡さない既定経路で
音声ストリームが消えた。`compose.eyecatch_insert` は本編と concat するために `[N:a]` を
参照するので、`concat=n=11:v=1:a=1 matches no streams` で落ちる。
**35分かけた本編レンダリングが最後の1工程で無駄になった。**
"""

from pathlib import Path

import wwedit.publish.eyecatch as ec


def _capture(monkeypatch):
    """ffmpeg を実行せずコマンドだけ取る。"""
    calls: list[list[str]] = []

    class _P:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(ec, "_render_ink",
                        lambda out_mp4, **kw: Path(out_mp4))
    monkeypatch.setattr(ec, "_title_card",
                        lambda title, out_png, **kw: Path(out_png))
    monkeypatch.setattr(ec, "_run", lambda cmd, **kw: (calls.append(cmd), _P())[1])
    return calls


def test_no_jingle_still_has_audio_stream(tmp_path, monkeypatch):
    calls = _capture(monkeypatch)
    ec.generate_eyecatch("章タイトル", tmp_path / "ec.mp4", seed=1, duration=2.0)
    cmd = calls[0]
    assert any(a.startswith("anullsrc=") for a in cmd), "無音でも音声入力が要る"
    assert "[aout]" in cmd, "音声を map していない"
    assert "aac" in cmd


def test_jingle_path_still_maps_audio(tmp_path, monkeypatch):
    calls = _capture(monkeypatch)
    monkeypatch.setattr(ec, "_audio_dur", lambda p: 30.0)
    j = tmp_path / "j.wav"
    j.write_bytes(b"")
    ec.generate_eyecatch("章タイトル", tmp_path / "ec.mp4", seed=1, duration=2.0,
                         jingle=j)
    cmd = calls[0]
    assert not any(a.startswith("anullsrc=") for a in cmd), "jingle があれば無音は足さない"
    assert "[aout]" in cmd
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "afade" in fc, "jingle はフェードして使う"
