import pytest

from wwedit.ingest.normalize import normalize_folder_name


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-06-04", "2026-06-04"),
        ("2026-02-06 18.01.05 わくわく勉強会", "2026-02-06"),
        ("2024-08-29 08.05.57 [要録画🔴] わく枠べんきょ会", "2024-08-29"),
        ("20240808", "2024-08-08"),
        ("2025-07-26_saburo", "2025-07-26"),
        ("2024-10-31_wakuwaku", "2024-10-31"),
    ],
)
def test_normalize(raw, expected):
    assert normalize_folder_name(raw) == expected


def test_normalize_invalid():
    with pytest.raises(ValueError):
        normalize_folder_name("no-date-here")
