from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from orchestrator.config import OrchestratorConfig
from orchestrator.runtime import OrchestratorRuntime
from gateway.preferences.store import GatewayPreferenceStore
from shared.model_specs import lookup_model_spec, infer_model_provider


class Stream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"reasoning":"Checking image"},"finish_reason":null}]}\n\n'
        yield b'data: {"choices":[{"delta":{"content":"A red square."},"finish_reason":null}]}\n\n'
        yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":100,"completion_tokens":5,"total_tokens":105,"prompt_tokens_details":{"cached_tokens":20}}}\n\n'
        yield b'data: [DONE]\n\n'


def runtime():
    rt = object.__new__(OrchestratorRuntime)
    rt.config = OrchestratorConfig(openrouter_api_key="openrouter-test-key", fireworks_api_key="fireworks-test-key")
    return rt


def test_mimo_is_default_and_keeps_images_on_native_model():
    rt = runtime()
    assert rt.config.orchestrator_default_provider == "openrouter_mimo"
    for has_images in (False, True):
        selection = rt._effective_fireworks_selection_for_images(
            preferred_provider="openrouter_mimo", preferred_model="xiaomi/mimo-v2.6-pro", has_images=has_images,
        )
        assert selection.effective_provider == "openrouter_mimo"
        assert selection.effective_model == "xiaomi/mimo-v2.6-pro"
        assert selection.fallback_reason is None
    glm = rt._effective_fireworks_selection_for_images(
        preferred_provider="fireworks_glm", preferred_model="accounts/fireworks/models/glm-5p3", has_images=True,
    )
    assert glm.effective_provider == "fireworks_kimi"
    assert glm.fallback_reason == "image_input"
    flash = rt._effective_fireworks_selection_for_images(
        preferred_provider="fireworks_glm", preferred_model="accounts/fireworks/models/glm-5p3-flash", has_images=True,
    )
    assert flash.effective_provider == "fireworks_glm"
    assert flash.fallback_reason is None


def test_preference_store_defaults_to_mimo_and_preserves_explicit_glm(tmp_path):
    store = GatewayPreferenceStore(tmp_path / "preferences.db")
    store.initialize()
    assert store.get_cosmic_orchestrator_model()["model"] == "xiaomi/mimo-v2.6-pro"
    store.set_cosmic_orchestrator_model("fireworks_glm", model="accounts/fireworks/models/glm-5p3-flash")
    store.initialize()
    assert store.get_cosmic_orchestrator_model()["provider"] == "fireworks_glm"
    saved = store.set_cosmic_orchestrator_model("openrouter")
    assert saved["provider"] == "openrouter_mimo"
    assert saved["model"] == "xiaomi/mimo-v2.6-pro"


def test_mimo_pricing_and_capabilities():
    spec = lookup_model_spec("openrouter", "xiaomi/mimo-v2.6-pro")
    assert spec is not None
    assert spec.pricing["input_per_1m_usd"] == 0.435
    assert spec.pricing["output_per_1m_usd"] == 0.87
    assert spec.capabilities["supports_image_input"]
    assert spec.capabilities["supports_tool_calling"]
    assert infer_model_provider(spec.base_url, spec.model) == "openrouter"


@pytest.mark.asyncio
async def test_mimo_stream_uses_openrouter_key_and_usage_with_images_and_tools():
    rt = runtime()
    requests = []
    def handler(request):
        requests.append(request)
        assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer openrouter-test-key"
        body = json.loads(request.content)
        assert body["model"] == "xiaomi/mimo-v2.6-pro"
        assert "reasoning_effort" not in body
        assert body["reasoning"] == {"enabled": True}
        assert body["tools"]
        assert body["messages"][0]["content"][1]["type"] == "image_url"
        return httpx.Response(200, stream=Stream())
    rt._record_internal_usage_event = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rt._client = client
        payloads = [p async for p in rt._stream_openai_chat_events(
            model_name="xiaomi/mimo-v2.6-pro",
            messages=[{"role":"user", "content":[{"type":"text", "text":"Describe"}, {"type":"image_url", "image_url":{"url":"https://example.test/red.png"}}]}],
            tools=[{"type":"function", "function":{"name":"memory_search", "parameters":{"type":"object"}}}],
            usage_context={"operation":"orchestrator_test"},
        )]
    assert len(requests) == 1
    assert payloads[1]["choices"][0]["delta"]["content"] == "A red square."
    call = rt._record_internal_usage_event.call_args.kwargs
    assert call["model_key"] == "openrouter:xiaomi/mimo-v2.6-pro"
    assert call["raw_usage"]["prompt_tokens"] == 100


@pytest.mark.asyncio
async def test_default_mimo_runs_full_orchestrator_image_turn(tmp_path):
    from test_orchestrator_reasoning_effort import _signed_task
    from shared import sign_task_envelope
    requests = []
    def handler(request):
        if str(request.url).endswith("/chat/completions"):
            requests.append(request)
            body = json.loads(request.content)
            assert body["model"] == "xiaomi/mimo-v2.6-pro"
            system = body["messages"][0]["content"]
            assert "Keep planning concise" in system
            assert "Xiaomi MiMo on OpenRouter with native image support" in system
            assert "call `think_deeper` once to raise" not in system
            assert body["reasoning"] == {"enabled": True}
            assert request.url.host == "openrouter.ai"
            assert request.headers["authorization"] == "Bearer openrouter-test-key"
            assert any(isinstance(m.get("content"), list) and any(p.get("type") == "image_url" for p in m["content"] if isinstance(p, dict)) for m in body["messages"])
            return httpx.Response(200, stream=Stream())
        return httpx.Response(204)
    config = OrchestratorConfig(internal_token="internal-token", signing_secret="signing-secret",
        openrouter_api_key="openrouter-test-key", task_ledger_db_path=tmp_path / "ledger.db")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rt = OrchestratorRuntime(config, client=client)
        await rt.start()
        try:
            task = _signed_task("signing-secret").model_copy(update={"input_artifacts":[
                {"kind":"image", "mime":"image/png", "filename":"red.png", "provider_url":"https://example.test/red.png"}
            ]})
            task = task.model_copy(update={"signature":sign_task_envelope(task, "signing-secret")})
            events = [event async for event in rt.stream_task(task)]
        finally:
            await rt.stop()
    assert requests
    assert events[0]["model_provider"] == "openrouter_mimo"
    completed = next(e for e in events if e["type"] == "response.complete")
    assert completed["content"] == "A red square."
    assert completed["model"] == "xiaomi/mimo-v2.6-pro"
    assert not any(e.get("status") == "model_switch" for e in events)


@pytest.mark.asyncio
async def test_mimo_keeps_reasoning_through_tool_calls_and_streams_followup_thinking(tmp_path):
    from test_orchestrator_reasoning_effort import _signed_task, SSEByteStream
    requests = []
    details = [{"type": "reasoning.text", "text": "Need the stored session.", "format": "unknown", "index": 0}]
    def handler(request):
        if not str(request.url).endswith("/chat/completions"):
            return httpx.Response(204)
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            chunks = [
                {"choices": [{"delta": {"reasoning": "Need the stored session.", "reasoning_details": details}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_history", "type": "function",
                    "function": {"name": "session_history", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}]},
            ]
        else:
            previous = next(m for m in body["messages"] if m.get("tool_calls"))
            assert previous["reasoning"] == "Need the stored session."
            assert previous["reasoning_details"] == details
            chunks = [
                {"choices": [{"delta": {"reasoning": "The tool finished; now continue."}}]},
                {"choices": [{"delta": {"content": "Finished."}, "finish_reason": "stop"}]},
            ]
        return httpx.Response(200, stream=SSEByteStream([
            ("data: " + json.dumps(chunk) + "\n\n").encode() for chunk in chunks
        ] + [b"data: [DONE]\n\n"]))
    config = OrchestratorConfig(internal_token="internal-token", signing_secret="signing-secret",
        openrouter_api_key="openrouter-test-key", task_ledger_db_path=tmp_path / "ledger.db")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rt = OrchestratorRuntime(config, client=client)
        await rt.start()
        try:
            rt._tool_executor.execute = AsyncMock(return_value='{"messages": []}')
            events = [event async for event in rt.stream_task(_signed_task("signing-secret"))]
        finally:
            await rt.stop()
    assert len(requests) == 2
    thoughts = ''.join(e['content'] for e in events if e['type'] == 'response.thinking.chunk')
    assert thoughts == 'Need the stored session.The tool finished; now continue.'
    assert next(e for e in events if e['type'] == 'response.complete')['content'] == 'Finished.'
