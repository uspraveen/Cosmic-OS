from __future__ import annotations

from shared.usage import build_usage_event, begin_metered_call


def test_build_usage_event_normalizes_provider_usage_and_cost() -> None:
    event = build_usage_event(
        metered_call=begin_metered_call(prefix="call"),
        source_component="gateway",
        source_id="gateway:haiku",
        session_id="sess_1",
        route="haiku",
        operation="gateway.direct_chat",
        model_key="anthropic:claude-haiku-4-5",
        request_id="req_1",
        provider_request_id="anth_req_1",
        raw_usage={
            "input_tokens": 41,
            "output_tokens": 7,
            "cache_read_input_tokens": 3,
            "cost": {
                "total_cost": 0.0042,
            },
        },
        metadata_json={"channel": "desktop:desk_a"},
    )

    assert event.provider == "anthropic"
    assert event.model == "claude-haiku-4-5"
    assert event.usage_kind == "messages"
    assert event.prompt_tokens == 41
    assert event.completion_tokens == 7
    assert event.total_tokens == 48
    assert event.cached_tokens == 3
    assert event.reasoning_tokens == 0
    assert event.estimated_cost_usd == 0.0042
    assert event.metadata_json["channel"] == "desktop:desk_a"
    assert event.metadata_json["raw_usage"]["output_tokens"] == 7


def test_build_usage_event_clamps_and_backfills_missing_totals() -> None:
    event = build_usage_event(
        metered_call=begin_metered_call(prefix="call"),
        source_component="session_manager",
        source_id="cosmic-memory",
        operation="memory.graph_extract",
        model_key="xai:grok-4-1-fast-reasoning",
        raw_usage={
            "prompt_text_tokens": 12,
            "output_tokens": 4,
            "cached_prompt_text_tokens": 99,
            "reasoning_tokens": 77,
        },
    )

    assert event.provider == "xai"
    assert event.model == "grok-4-1-fast-reasoning"
    assert event.prompt_tokens == 12
    assert event.completion_tokens == 4
    assert event.total_tokens == 16
    assert event.cached_tokens == 12
    assert event.reasoning_tokens == 4
