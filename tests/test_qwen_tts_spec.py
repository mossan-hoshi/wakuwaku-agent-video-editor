"""`synth_batch` は job のキーを**そのまま**別プロセスへ渡す。

`ref_files`（コーパスの話者を参照にする）や `effect`（崩壊加工）は
ここで落とすと黙って既定動作になる＝**指定したのに効かない**という一番わかりにくい壊れ方をする。
2026-08-08 に `--ref` が無視されて「録り直しても1バイト違わない音が出た」実績があるので、
受け渡しの段でも固定しておく。
"""

import json
from pathlib import Path

import pytest

from wwedit.publish import qwen_tts


class _Proc:
    returncode = 0
    stdout = ""
    stderr = ""


@pytest.fixture
def captured(monkeypatch, tmp_path):
    """`subprocess.run` を差し替えて jobs.json を覗く。"""
    seen = {}

    py = tmp_path / "python.exe"
    py.write_text("")
    hcm = tmp_path / "hcm"
    hcm.mkdir()
    (hcm / "app.py").write_text("")
    monkeypatch.setattr(qwen_tts, "_cfg", lambda k: {
        "WWEDIT_QWEN_TTS_PYTHON": str(py),
        "WWEDIT_QWEN_TTS_DIR": str(hcm),
        "WWEDIT_QWEN_TTS_MODEL": "m",
        "WWEDIT_QWEN_HF_HOME": str(tmp_path / "hf"),
    }[k])

    def fake_run(cmd, **kw):
        spec_path, res_path = Path(cmd[-2]), Path(cmd[-1])
        seen["spec"] = json.loads(spec_path.read_text(encoding="utf-8"))
        res_path.write_text(json.dumps([
            {"out": j["out"], "duration_sec": 1.0, "sim": 1.0, "tries": 1, "sim_ok": True}
            for j in seen["spec"]["jobs"]
        ]), encoding="utf-8")
        return _Proc()

    monkeypatch.setattr(qwen_tts.subprocess, "run", fake_run)
    return seen


def test_ref_files_and_sim_refs_pass_through(captured, tmp_path):
    qwen_tts.synth_batch([{
        "text": "こんにちは", "out": tmp_path / "a.wav", "char": "tsukasa",
        "ref_files": [{"wav": "r1.wav", "text": "参照1"}],
        "sim_refs": ["s1.wav", "s2.wav"],
    }])
    job = captured["spec"]["jobs"][0]
    assert job["ref_files"] == [{"wav": "r1.wav", "text": "参照1"}]
    assert job["sim_refs"] == ["s1.wav", "s2.wav"]
    assert job["out"].endswith("a.wav")


def test_collapse_params_pass_through(captured, tmp_path):
    qwen_tts.synth_batch([{
        "text": "こわれる", "out": tmp_path / "b.wav", "char": "noa",
        "effect": "ramp_reverse", "chunk_sec": 0.5, "ramp_start": 0.5,
        "ramp_hard": 0.9, "warp": 0.0,
    }])
    job = captured["spec"]["jobs"][0]
    assert job["effect"] == "ramp_reverse"
    assert job["ramp_start"] == 0.5
    assert job["ramp_hard"] == 0.9


def test_ref_name_still_passes(captured, tmp_path):
    qwen_tts.synth_batch([{
        "text": "はい", "out": tmp_path / "c.wav", "char": "noa", "ref": "set4",
    }])
    assert captured["spec"]["jobs"][0]["ref"] == "set4"
