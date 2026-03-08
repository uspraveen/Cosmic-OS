from __future__ import annotations

from gateway.adapters.response_processor import normalize_conversation_history
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


def test_normalize_conversation_history_trims_orphaned_assistant_edges() -> None:
    history = [
        {"role": "assistant", "content": "Older assistant reply kept by suffix pruning"},
        {"role": "assistant", "content": "Second assistant chunk"},
        {"role": "user", "content": "Question after long Claude turn"},
        {"role": "assistant", "content": "Assistant reply from prior turn"},
        {"role": "assistant", "content": "Trailing assistant that should not become the prompt target"},
    ]

    assert normalize_conversation_history(history) == [
        {
            "role": "user",
            "content": "Question after long Claude turn",
        }
    ]


def test_perplexity_adapter_drops_leading_assistant_suffix() -> None:
    adapter = PerplexityAdapter(api_key="test-key", model="sonar", timeout_sec=5.0)
    try:
        messages = adapter._build_messages(  # noqa: SLF001 - direct unit seam
            [
                {"role": "assistant", "content": "Long Opus reply that overflowed the budget"},
                {"role": "user", "content": "Now search the latest news about it"},
            ]
        )
    finally:
        import asyncio

        asyncio.run(adapter.close())

    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1] == {
        "role": "user",
        "content": "Now search the latest news about it",
    }
