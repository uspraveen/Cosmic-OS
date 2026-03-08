from __future__ import annotations

from gateway.adapters.haiku import HaikuAdapter
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


def test_haiku_adapter_merges_consecutive_turns() -> None:
    adapter = HaikuAdapter(
        api_key="test-key",
        model="claude-haiku-4-5",
        anthropic_version="2023-06-01",
        max_tokens=16000,
        thinking_budget_tokens=10000,
        timeout_sec=5.0,
    )
    try:
        messages = adapter._build_messages(sample_history())  # noqa: SLF001 - direct unit seam
    finally:
        import asyncio

        asyncio.run(adapter.close())

    assert messages == [
        {
            "role": "user",
            "content": "First question\n\nFollow-up after an error",
        },
        {
            "role": "assistant",
            "content": "Prior answer\n\nExtra assistant note",
        },
        {
            "role": "user",
            "content": "Latest question",
        },
    ]
