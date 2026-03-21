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


def test_usage_store_dashboard_summary_groups_provider_and_feature(tmp_path: Path) -> None:
    store = UsageStore(tmp_path / "usage.db")
    store.initialize()

    events = [
        build_usage_event(
            metered_call=begin_metered_call(prefix="call"),
            source_component="orchestrator",
            source_id="docs",
            session_id="sess_docs",
            route="opus",
            operation="docs.search_bundle",
            model_key="anthropic:claude-sonnet-4-6",
            request_id="req_docs_1",
            raw_usage={"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160},
            metadata_json=None,
        ),
        build_usage_event(
            metered_call=begin_metered_call(prefix="call"),
            source_component="orchestrator",
            source_id="docs",
            session_id="sess_docs",
            route="opus",
            operation="docs.read_bundle",
            model_key="anthropic:claude-sonnet-4-6",
            request_id="req_docs_2",
            raw_usage={"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100},
            metadata_json=None,
        ),
        build_usage_event(
            metered_call=begin_metered_call(prefix="call"),
            source_component="gateway",
            source_id="gateway:research",
            session_id="sess_research",
            route="perplexity",
            operation="perplexity_research",
            model_key="perplexity:sonar",
            request_id="req_research_1",
            raw_usage={"prompt_tokens": 60, "completion_tokens": 15, "total_tokens": 75},
            metadata_json=None,
        ),
    ]

    for event in events:
        assert store.append(event) is True

    summary = store.dashboard_summary(period_days=30)

    assert summary["total_calls"] == 3
    assert summary["total_tokens"] == 335
    assert summary["providers"][0]["name"] == "Anthropic"
    assert summary["providers"][0]["calls"] == 2
    assert summary["usage_by_feature"][0]["label"] == "Documents"
    assert summary["usage_by_feature"][0]["count"] == 2
