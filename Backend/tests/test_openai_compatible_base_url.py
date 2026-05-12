from __future__ import annotations

import pytest

from agents.tabular_agent.config import normalize_openai_compatible_base_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        ("https://api.openai.com", "https://api.openai.com/v1"),
        ("https://api.openai.com/", "https://api.openai.com/v1"),
        ("https://api.openai.com/v1", "https://api.openai.com/v1"),
        ("https://api.openai.com/v1/", "https://api.openai.com/v1"),
        (
            "https://api.openai.com/v1/chat/completions",
            "https://api.openai.com/v1",
        ),
        ("https://example.com/v1/chat/completions", "https://example.com/v1"),
    ],
)
def test_normalize_openai_compatible_base_url(raw: str, expected: str) -> None:
    assert normalize_openai_compatible_base_url(raw) == expected
