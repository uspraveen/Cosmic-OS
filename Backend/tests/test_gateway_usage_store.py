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
    assert summary["providers"][0]["count"] == 2
    assert summary["providers"][0]["role"] == "claude-sonnet-4-6"
    assert summary["usage_by_feature"][0]["label"] == "Documents"
    assert summary["usage_by_feature"][0]["count"] == 2


def test_usage_store_dashboard_summary_derives_missing_cost_and_aggregates_provider_models(tmp_path: Path) -> None:
    store = UsageStore(tmp_path / "usage.db")
    store.initialize()

    fast_reasoning = build_usage_event(
        metered_call=begin_metered_call(prefix="call"),
        source_component="gateway",
        source_id="gateway:memory",
        session_id="sess_mem",
        route="internal",
        operation="gateway.memory.extract",
        model_key="xai:grok-4-1-fast-reasoning",
        request_id="req_mem_1",
        raw_usage={
            "prompt_text_tokens": 1000,
            "output_tokens": 500,
            "cached_prompt_text_tokens": 100,
        },
        metadata_json=None,
    )
    # Simulate older rows that logged tokens but missed USD.
    fast_reasoning = fast_reasoning.model_copy(update={"estimated_cost_usd": None})

    x_search = build_usage_event(
        metered_call=begin_metered_call(prefix="call"),
        source_component="agent",
        source_id="cosmic/x-twitter-search-agent:1.0.0",
        session_id="sess_x",
        route="specialist",
        operation="agent.x.search",
        model_key="xai:grok-4.20-beta-0309-reasoning",
        request_id="req_x_1",
        raw_usage={
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "cached_prompt_text_tokens": 100,
        },
        estimated_cost_usd=0.01572,
        metadata_json=None,
    )

    assert store.append(fast_reasoning) is True
    assert store.append(x_search) is True

    summary = store.dashboard_summary(period_days=30)

    assert summary["total_calls"] == 2
    assert summary["providers"][0]["name"] == "xAI"
    assert summary["providers"][0]["count"] == 2
    assert summary["providers"][0]["role"] == "grok-4.20-beta-0309-reasoning +1 more"
    assert summary["providers"][0]["cost_usd"] == 0.016155
    assert summary["total_cost_usd"] == 0.016155


def test_usage_store_dashboard_summary_absolute_range_filters_rows(tmp_path: Path) -> None:
    store = UsageStore(tmp_path / "usage.db")
    store.initialize()

    inside = build_usage_event(
        metered_call=begin_metered_call(prefix="call"),
        source_component="gateway",
        source_id="gateway:x",
        session_id="sess_in",
        route="opus",
        operation="gateway.ping",
        model_key="anthropic:claude-sonnet-4-6",
        request_id="req_in",
        raw_usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        metadata_json=None,
    ).model_copy(update={"llm_call_placed_at": "2025-06-15T12:00:00Z"})
    outside = build_usage_event(
        metered_call=begin_metered_call(prefix="call"),
        source_component="gateway",
        source_id="gateway:y",
        session_id="sess_out",
        route="opus",
        operation="gateway.ping",
        model_key="anthropic:claude-sonnet-4-6",
        request_id="req_out",
        raw_usage={"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
        metadata_json=None,
    ).model_copy(update={"llm_call_placed_at": "2025-01-10T12:00:00Z"})

    assert store.append(inside) is True
    assert store.append(outside) is True

    summary = store.dashboard_summary(
        range_start_iso="2025-06-01T00:00:00Z",
        range_end_iso="2025-07-01T00:00:00Z",
    )
    assert summary["total_calls"] == 1
    assert summary["total_tokens"] == 15
    assert summary["usage_period"]["mode"] == "absolute"


def test_usage_store_dashboard_summary_last_hours(tmp_path: Path) -> None:
    store = UsageStore(tmp_path / "usage.db")
    store.initialize()
    assert store.summary()["total_events"] == 0
    summary = store.dashboard_summary(period_hours=24)
    assert summary["usage_period"]["mode"] == "rolling_hours"
    assert summary["usage_period"]["period_hours"] == 24


def test_usage_store_usage_time_bounds(tmp_path: Path) -> None:
    store = UsageStore(tmp_path / "usage.db")
    store.initialize()
    assert store.usage_time_bounds()["earliest_call_at"] is None

    event = build_usage_event(
        metered_call=begin_metered_call(prefix="call"),
        source_component="gateway",
        source_id="gateway:x",
        session_id="sess",
        route="opus",
        operation="gateway.ping",
        model_key="anthropic:claude-sonnet-4-6",
        request_id="req_1",
        raw_usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        metadata_json=None,
    ).model_copy(update={"llm_call_placed_at": "2025-03-10T08:00:00Z"})
    assert store.append(event) is True
    bounds = store.usage_time_bounds()
    assert bounds["earliest_call_at"] == "2025-03-10T08:00:00Z"
    assert bounds["latest_call_at"] == "2025-03-10T08:00:00Z"
