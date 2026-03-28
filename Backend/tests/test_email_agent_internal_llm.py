from __future__ import annotations

import sys
import types

import httpx
import pytest

from agents.email_agent.config import EmailAgentConfig
from agents.email_agent.internal_llm import invoke_email_mimo


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeResult:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage_metadata = {"input_tokens": 11, "output_tokens": 7}


@pytest.mark.asyncio
async def test_invoke_email_mimo_omits_temperature_for_gpt5_models(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class _FakeChatOpenAI:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        async def ainvoke(self, messages) -> _FakeResult:
            assert len(messages) == 2
            return _FakeResult("ok")

    fake_messages = types.ModuleType("langchain_core.messages")
    fake_messages.HumanMessage = _FakeMessage
    fake_messages.SystemMessage = _FakeMessage

    fake_openai = types.ModuleType("langchain_openai")
    fake_openai.ChatOpenAI = _FakeChatOpenAI

    monkeypatch.setitem(sys.modules, "langchain_core.messages", fake_messages)
    monkeypatch.setitem(sys.modules, "langchain_openai", fake_openai)

    cfg = EmailAgentConfig(
        enable_internal_llm=True,
        mimo_api_key="test-key",
        mimo_base_url="https://api.openai.com/v1",
        mimo_model="gpt-5-nano",
        gateway_internal_token="",
    )
    async with httpx.AsyncClient() as client:
        result = await invoke_email_mimo(
            cfg=cfg,
            http_client=client,
            system_content="system",
            user_message="user",
            task_id="tsk_1",
            session_id="sess_1",
            request_id="req_1",
            source="user",
            source_id="desktop",
            channel="desktop:test",
        )

    assert result == "ok"
    assert captured["model"] == "gpt-5-nano"
    assert "temperature" not in captured


@pytest.mark.asyncio
async def test_invoke_email_mimo_keeps_temperature_for_non_gpt5_models(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class _FakeChatOpenAI:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        async def ainvoke(self, messages) -> _FakeResult:
            assert len(messages) == 2
            return _FakeResult("ok")

    fake_messages = types.ModuleType("langchain_core.messages")
    fake_messages.HumanMessage = _FakeMessage
    fake_messages.SystemMessage = _FakeMessage

    fake_openai = types.ModuleType("langchain_openai")
    fake_openai.ChatOpenAI = _FakeChatOpenAI

    monkeypatch.setitem(sys.modules, "langchain_core.messages", fake_messages)
    monkeypatch.setitem(sys.modules, "langchain_openai", fake_openai)

    cfg = EmailAgentConfig(
        enable_internal_llm=True,
        mimo_api_key="test-key",
        mimo_base_url="https://api.xiaomimimo.com/v1",
        mimo_model="mimo-v2-pro",
        gateway_internal_token="",
    )
    async with httpx.AsyncClient() as client:
        result = await invoke_email_mimo(
            cfg=cfg,
            http_client=client,
            system_content="system",
            user_message="user",
            task_id="tsk_2",
            session_id="sess_2",
            request_id="req_2",
            source="user",
            source_id="desktop",
            channel="desktop:test",
            temperature=0.2,
        )

    assert result == "ok"
    assert captured["model"] == "mimo-v2-pro"
    assert captured["temperature"] == 0.2
