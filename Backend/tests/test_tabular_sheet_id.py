from __future__ import annotations

import pytest

from shared.tabular_artifacts import validate_safe_sheet_id


def test_validate_safe_sheet_id_accepts() -> None:
    assert validate_safe_sheet_id("sheet_1") == "sheet_1"
    assert validate_safe_sheet_id("A") == "A"
    assert validate_safe_sheet_id("a" * 80) == "a" * 80


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "a" * 81,
        "bad-id",
        "x/y",
        "..",
        "two words",
        "a\tb",
        "séét",
    ],
)
def test_validate_safe_sheet_id_rejects(raw: str) -> None:
    with pytest.raises(ValueError, match="sheet_id must"):
        validate_safe_sheet_id(raw)
