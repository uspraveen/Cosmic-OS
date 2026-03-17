from __future__ import annotations

from pathlib import Path

from gateway.usage_store import UsageStore
from shared.usage import build_usage_event, begin_metered_call


def test_usage_store_appends_and_deduplicates(tmp_path: Path) -> None:
    store = UsageStore(tmp_path / "usage.db")
    store.initialize()

    event = build_usage_event(
        metered_call=begin_metered_call(prefix="call"),
        source_component="gateway",
        source_id="gateway:perplexity",
        session_id="sess_1",
        route="perplexity",
        operation="gateway.direct_chat",
        model_key="perplexity:sonar",
        request_id="req_1",
        raw_usage={
            "prompt_tokens": 9,
            "completion_tokens": 3,
            "total_tokens": 12,
        },
        metadata_json={"channel": "desktop:desk_a"},
    )

    assert store.append(event) is True
    assert store.append(event) is False

    summary = store.summary()
    assert summary["total_events"] == 1
    assert summary["failed_events"] == 0

    recent = store.list_recent(limit=10)
    assert len(recent) == 1
    assert recent[0]["llm_call_id"] == event.llm_call_id
    assert recent[0]["metadata_json"]["channel"] == "desktop:desk_a"
    assert recent[0]["success"] is True
