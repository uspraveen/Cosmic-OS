from __future__ import annotations

import pytest

from agents.tabular_agent.config import normalize_mimo_openai_base_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        ("https://api.mimo-v2.com", "https://api.mimo-v2.com/v1"),
        ("https://api.mimo-v2.com/", "https://api.mimo-v2.com/v1"),
        ("https://api.mimo-v2.com/v1", "https://api.mimo-v2.com/v1"),
        ("https://api.mimo-v2.com/v1/", "https://api.mimo-v2.com/v1"),
        (
            "https://api.xiaomimimo.com/v1/chat/completions",
            "https://api.xiaomimimo.com/v1",
        ),
        ("https://example.com/v1/chat/completions", "https://example.com/v1"),
    ],
)
def test_normalize_mimo_openai_base_url(raw: str, expected: str) -> None:
    assert normalize_mimo_openai_base_url(raw) == expected
