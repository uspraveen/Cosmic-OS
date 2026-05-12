from __future__ import annotations

import asyncio

from shared.usage import build_usage_event, begin_metered_call, post_usage_event


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


def test_build_usage_event_estimates_cost_from_model_specs_when_provider_cost_missing() -> None:
    event = build_usage_event(
        metered_call=begin_metered_call(prefix="call"),
        source_component="agent",
        source_id="cosmic/x-twitter-search-agent:1.0.0",
        operation="agent.x.search",
        model_key="xai:grok-4.20-beta-0309-reasoning",
        raw_usage={
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "cached_prompt_text_tokens": 100,
        },
    )

    assert event.estimated_cost_usd == 0.00482


def test_build_usage_event_estimates_fireworks_kimi_with_nested_usage_details() -> None:
    event = build_usage_event(
        metered_call=begin_metered_call(prefix="call"),
        source_component="orchestrator",
        source_id="cosmic/orchestrator:1.0.0",
        operation="orchestrator.process",
        model_key="fireworks:accounts/fireworks/models/kimi-k2p6",
        raw_usage={
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "total_tokens": 1200,
            "prompt_tokens_details": {"cached_tokens": 100},
            "completion_tokens_details": {"reasoning_tokens": 40},
        },
    )

    assert event.provider == "fireworks"
    assert event.model == "accounts/fireworks/models/kimi-k2p6"
    assert event.usage_kind == "chat_completion"
    assert event.prompt_tokens == 1000
    assert event.completion_tokens == 200
    assert event.total_tokens == 1200
    assert event.cached_tokens == 100
    assert event.reasoning_tokens == 40
    assert event.estimated_cost_usd == 0.001671


def test_build_usage_event_estimates_flat_image_cost_from_model_specs() -> None:
    event = build_usage_event(
        metered_call=begin_metered_call(prefix="call"),
        source_component="agent",
        source_id="cosmic/image-generator-agent:1.0.0",
        operation="agent.image.generate.provider",
        model_key="xai:grok-imagine-image-pro",
        raw_usage={
            "images": 2,
            "input_images": 1,
        },
    )

    assert event.estimated_cost_usd == 0.21


def test_build_usage_event_estimates_openai_image_generation_cost_from_pricing_table() -> None:
    event = build_usage_event(
        metered_call=begin_metered_call(prefix="call"),
        source_component="agent",
        source_id="cosmic/image-generator-agent:1.0.0",
        operation="agent.image.generate.provider",
        model_key="openai:gpt-image-1.5",
        raw_usage={
            "images": 1,
            "generation_quality": "high",
            "generation_size": "1024x1536",
        },
    )

    assert event.estimated_cost_usd == 0.2


def test_post_usage_event_accepts_202_response() -> None:
    class FakeResponse:
        status_code = 202

        def raise_for_status(self) -> None:
            return

    class FakeClient:
        async def post(self, *args, **kwargs):
            del args, kwargs
            return FakeResponse()

    async def run() -> None:
        event = build_usage_event(
            metered_call=begin_metered_call(prefix="call"),
            source_component="orchestrator",
            source_id="cosmic/orchestrator:1.0.0",
            operation="orchestrator.process",
            model_key="anthropic:claude-opus-4-6",
            raw_usage={"input_tokens": 8, "output_tokens": 2},
        )
        posted = await post_usage_event(
            client=FakeClient(),
            gateway_url="http://127.0.0.1:8080",
            internal_token="internal-token",
            event=event,
        )
        assert posted is True

    asyncio.run(run())
