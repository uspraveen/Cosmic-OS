from __future__ import annotations

from gateway.adapters.gemini import GeminiAdapter
from gateway.adapters.perplexity import PerplexityAdapter


def sample_history():
    return [
        {"role": "user", "content": "First question"},
        {"role": "user", "content": "Follow-up after an error"},
        {"role": "assistant", "content": "Prior answer"},
        {"role": "assistant", "content": "Extra assistant note"},
        {"role": "user", "content": "Latest question"},
    ]


def test_perplexity_adapter_merges_consecutive_turns() -> None:
    adapter = PerplexityAdapter(api_key="test-key", model="sonar", timeout_sec=5.0)
    try:
        messages = adapter._build_messages(sample_history())  # noqa: SLF001 - direct unit seam
    finally:
        import asyncio

        asyncio.run(adapter.close())

    assert messages[0]["role"] == "system"
    assert messages[1] == {
        "role": "user",
        "content": "First question\n\nFollow-up after an error",
    }
    assert messages[2] == {
        "role": "assistant",
        "content": "Prior answer\n\nExtra assistant note",
    }
    assert messages[3] == {
        "role": "user",
        "content": "Latest question",
    }


def test_gemini_adapter_merges_consecutive_turns() -> None:
    adapter = GeminiAdapter(api_key="test-key", model="gemini-3-flash-preview", timeout_sec=5.0)
    try:
        contents = adapter._build_contents(sample_history())  # noqa: SLF001 - direct unit seam
    finally:
        import asyncio

        asyncio.run(adapter.close())

    assert contents == [
        {
            "role": "user",
            "parts": [{"text": "First question\n\nFollow-up after an error"}],
        },
        {
            "role": "model",
            "parts": [{"text": "Prior answer\n\nExtra assistant note"}],
        },
        {
            "role": "user",
            "parts": [{"text": "Latest question"}],
        },
    ]
