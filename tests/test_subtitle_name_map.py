"""字幕3経路すべてで .env の人名マップが効くこと（1つ忘れると実名が画面に出る）。"""
from __future__ import annotations

from wwedit.edl.schema import Edl, SourceMedia, TimeRange, Utterance
from wwedit.publish.voice_tts import subtitles_from_reading
from wwedit.subtitle.build import subtitles_from_utterances


def _edl(**kw) -> Edl:
    return Edl(recording_dir="2026-08-03",
               source=SourceMedia(video_path="v.mp4", fps=25, duration_s=100.0), **kw)


def test_subtitles_from_reading_applies_name_map(monkeypatch):
    monkeypatch.setenv("WWEDIT_SUBTITLE_NAME_MAP", "山田=ヤマダ,佐藤=サトウ")
    rows = [{"idx": 1, "speaker": "a", "out_start": 0.0, "tts_s": 2.0}]
    subs = subtitles_from_reading(rows, {1: "山田さんと佐藤さんの話"},
                                  [TimeRange(start=0, end=100)])
    joined = "".join(s.text for s in subs)
    assert "山田" not in joined and "佐藤" not in joined
    assert "ヤマダ" in joined and "サトウ" in joined


def test_subtitles_from_reading_applies_names_before_terms(monkeypatch):
    """人名(漢字→カナ)を先に、用語(カナ→正式表記)を後に当てる。"""
    monkeypatch.setenv("WWEDIT_SUBTITLE_NAME_MAP", "山田=ヤマダ")
    rows = [{"idx": 1, "speaker": "a", "out_start": 0.0, "tts_s": 2.0}]
    subs = subtitles_from_reading(rows, {1: "山田さんがリリアを試した"},
                                  [TimeRange(start=0, end=100)],
                                  terms=[("リリア", "Lyria")])
    joined = "".join(s.text for s in subs)
    assert "ヤマダ" in joined and "Lyria" in joined


def test_subtitles_from_utterances_applies_name_map(monkeypatch):
    monkeypatch.setenv("WWEDIT_SUBTITLE_NAME_MAP", "山田=ヤマダ")
    edl = _edl(utterances=[Utterance(speaker="a", text="山田さんどうぞ", start=0, end=3)])
    joined = "".join(s.text for s in subtitles_from_utterances(edl))
    assert "山田" not in joined and "ヤマダ" in joined


def test_no_name_map_leaves_text_untouched(monkeypatch):
    monkeypatch.delenv("WWEDIT_SUBTITLE_NAME_MAP", raising=False)
    monkeypatch.chdir("/")          # .env を読ませない
    rows = [{"idx": 1, "speaker": "a", "out_start": 0.0, "tts_s": 2.0}]
    subs = subtitles_from_reading(rows, {1: "そのままの文"}, [TimeRange(start=0, end=100)])
    assert "".join(s.text for s in subs) == "そのままの文"


def test_load_decisions_applies_name_map(tmp_path, monkeypatch):
    """読み上げ文の入口で潰す＝合成にも字幕にも同じカタカナが渡る。"""
    monkeypatch.setenv("WWEDIT_SUBTITLE_NAME_MAP", "山田=ヤマダ")
    from wwedit.publish.voice_tts import load_decisions

    p = tmp_path / "d.json"
    p.write_text('{"lines":[{"idx":1,"text":"山田さんです"},{"idx":2,"text":""}]}',
                 encoding="utf-8")
    got = load_decisions(p)
    assert got == {1: "ヤマダさんです", 2: ""}
