from pathlib import Path

import pytest

from wwedit.eval.golden import (
    GOLDEN_DIRS,
    interval_total,
    removed_silence_from_fcpxml,
    score_cuts,
)


def test_score_cuts_metrics():
    gt = [(0.0, 10.0), (20.0, 30.0)]  # 正解カット 20s
    pred = [(0.0, 5.0), (20.0, 30.0)]  # 予測 15s, 重なり 15s
    m = score_cuts(pred, gt)
    assert m["recall"] == pytest.approx(15 / 20)
    assert m["precision"] == pytest.approx(15 / 15)
    assert m["iou"] == pytest.approx(15 / 20)  # union = 15+20-15 = 20


def _golden_fcpxml() -> list[Path]:
    out = []
    for d in GOLDEN_DIRS:
        cands = sorted(Path(d).glob("video*.fcpxml"))
        if cands:
            out.append(cands[0])
    return out


@pytest.mark.skipif(not _golden_fcpxml(), reason="編集済みゴールデンが無い環境")
@pytest.mark.parametrize("fcpxml", _golden_fcpxml(), ids=lambda p: p.parent.name)
def test_removed_silence_extractable(fcpxml: Path):
    from wwedit.compose.fcpxml import read_keep_ranges

    keep = read_keep_ranges(fcpxml)
    assert len(keep) > 50  # 実編集は多数のクリップに分割されている
    duration = max(r.end for r in keep)
    gaps = removed_silence_from_fcpxml(fcpxml, duration)
    # 除去区間は正・非重複・総和は全体未満
    assert all(e > s for s, e in gaps)
    assert interval_total(gaps) < duration
