from __future__ import annotations

import sys
import types

import httpx
import pytest

from agents.image_generator_agent.config import ImageGeneratorAgentConfig
from agents.image_generator_agent.internal_router import route_image_request


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeResult:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage_metadata = {"input_tokens": 9, "output_tokens": 4}
        self.response_metadata = {"id": "router_resp_1"}


@pytest.mark.asyncio
async def test_route_image_request_omits_temperature_for_gpt5_models(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class _FakeChatOpenAI:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        async def ainvoke(self, _messages) -> _FakeResult:
            return _FakeResult('{"provider":"openai","reason":"complex layout"}')

    fake_messages = types.ModuleType("langchain_core.messages")
    fake_messages.HumanMessage = _FakeMessage
    fake_messages.SystemMessage = _FakeMessage

    fake_openai = types.ModuleType("langchain_openai")
    fake_openai.ChatOpenAI = _FakeChatOpenAI

    monkeypatch.setitem(sys.modules, "langchain_core.messages", fake_messages)
    monkeypatch.setitem(sys.modules, "langchain_openai", fake_openai)

    cfg = ImageGeneratorAgentConfig(
        enable_internal_router_llm=True,
        router_api_key="test-key",
        router_base_url="https://api.openai.com/v1",
        router_model="gpt-5-mini",
        openai_api_key="openai-key",
        xai_api_key="xai-key",
        gateway_internal_token="",
    )
    async with httpx.AsyncClient() as client:
        decision = await route_image_request(
            cfg=cfg,
            http_client=client,
            agent_id="cosmic/image-generator-agent:1.0.0",
            task_id="tsk_1",
            parent_task_id=None,
            session_id="sess_1",
            request_id="req_1",
            payload={"prompt": "A complex poster with exact text labels"},
        )

    assert decision.provider == "openai"
    assert decision.model == "gpt-image-1.5"
    assert captured["model"] == "gpt-5-mini"
    assert "temperature" not in captured


@pytest.mark.asyncio
async def test_route_image_request_heuristic_defaults_to_xai_without_router_llm() -> None:
    cfg = ImageGeneratorAgentConfig(
        enable_internal_router_llm=False,
        openai_api_key="openai-key",
        xai_api_key="xai-key",
    )
    async with httpx.AsyncClient() as client:
        decision = await route_image_request(
            cfg=cfg,
            http_client=client,
            agent_id="cosmic/image-generator-agent:1.0.0",
            task_id="tsk_2",
            parent_task_id=None,
            session_id="sess_2",
            request_id="req_2",
            payload={"prompt": "A cinematic neon city street at dusk"},
        )

    assert decision.provider == "xai"
    assert decision.model == "grok-imagine-image-pro"


@pytest.mark.asyncio
async def test_route_image_request_keeps_generic_launch_poster_on_xai() -> None:
    cfg = ImageGeneratorAgentConfig(
        enable_internal_router_llm=False,
        openai_api_key="openai-key",
        xai_api_key="xai-key",
    )
    async with httpx.AsyncClient() as client:
        decision = await route_image_request(
            cfg=cfg,
            http_client=client,
            agent_id="cosmic/image-generator-agent:1.0.0",
            task_id="tsk_2b",
            parent_task_id=None,
            session_id="sess_2b",
            request_id="req_2b",
            payload={"prompt": "An Apple-like launch poster with a centered sphere on a black background"},
        )

    assert decision.provider == "xai"
    assert decision.model == "grok-imagine-image-pro"


@pytest.mark.asyncio
async def test_route_image_request_falls_back_when_default_provider_credentials_are_missing() -> None:
    cfg = ImageGeneratorAgentConfig(
        enable_internal_router_llm=False,
        openai_api_key="openai-key",
        xai_api_key="",
    )
    async with httpx.AsyncClient() as client:
        decision = await route_image_request(
            cfg=cfg,
            http_client=client,
            agent_id="cosmic/image-generator-agent:1.0.0",
            task_id="tsk_3",
            parent_task_id=None,
            session_id="sess_3",
            request_id="req_3",
            payload={"prompt": "A dramatic fantasy landscape"},
        )

    assert decision.provider == "openai"
    assert decision.model == "gpt-image-1.5"
    assert "fell back" in decision.reason
