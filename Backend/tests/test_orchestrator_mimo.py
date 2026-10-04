from __future__ import annotations

import asyncio
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


class DelayedStream(httpx.AsyncByteStream):
    def __init__(self, delta):
        self.delta = delta
        self.closed = False
    async def __aiter__(self):
        yield ("data: " + json.dumps({"choices": [{"delta": self.delta}]}) + "\n\n").encode()
        await asyncio.sleep(0.2)
        yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        yield b'data: [DONE]\n\n'
    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_mimo_thinking_deadline_retries_once_with_thinking_disabled():
    rt = runtime()
    rt.config.openrouter_mimo_thinking_timeout_sec = 0.05
    first = DelayedStream({"reasoning": "Still planning"})
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            assert body['reasoning'] == {'enabled': True}
            return httpx.Response(200, stream=first)
        assert body['reasoning'] == {'enabled': False}
        assert body['messages'] == requests[0]['messages']
        assert body['tools'] == requests[0]['tools']
        from test_orchestrator_reasoning_effort import SSEByteStream
        return httpx.Response(200, stream=SSEByteStream([
            b'data: {"choices":[{"delta":{"content":"Finished."},"finish_reason":"stop"}]}\n\n',
            b'data: [DONE]\n\n',
        ]))
    rt._record_internal_usage_event = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rt._client = client
        payloads = [p async for p in rt._stream_openai_chat_events(model_name=rt.config.openrouter_mimo_model,
            messages=[{'role':'user','content':'Test'}], tools=[{'type':'function','function':{'name':'test','parameters':{'type':'object'}}}], usage_context=None)]
    assert len(requests) == 2
    assert first.closed
    assert any(p.get('_cosmic_thinking_budget_exceeded') for p in payloads)
    assert payloads[-1]['choices'][0]['delta']['content'] == 'Finished.'
    assert rt._record_internal_usage_event.call_args_list[0].kwargs['error_code'] == 'stream_cancelled'


@pytest.mark.asyncio
@pytest.mark.parametrize('delta', [{'content':'An answer already started.'}, {'tool_calls':[{'index':0,'id':'call_one'}]}])
async def test_mimo_deadline_never_replays_started_text_or_tool_calls(delta):
    rt = runtime()
    rt.config.openrouter_mimo_thinking_timeout_sec = 0.05
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, stream=DelayedStream(delta))
    rt._record_internal_usage_event = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rt._client = client
        payloads = [p async for p in rt._stream_openai_chat_events(model_name=rt.config.openrouter_mimo_model,
            messages=[{'role':'user','content':'Test'}], tools=[], usage_context=None)]
    assert len(requests) == 1
    assert not any(p.get('_cosmic_thinking_budget_exceeded') for p in payloads)


@pytest.mark.asyncio
async def test_mimo_budget_fallback_reports_progress_and_does_not_replay_abandoned_reasoning(tmp_path):
    from test_orchestrator_reasoning_effort import _signed_task, SSEByteStream
    requests = []
    def handler(request):
        if not str(request.url).endswith('/chat/completions'):
            return httpx.Response(204)
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(200, stream=DelayedStream({'reasoning':'Abandoned planning'}))
        if len(requests) == 2:
            assert body['reasoning'] == {'enabled': False}
            delta = {'tool_calls':[{'index':0,'id':'call_one','type':'function',
                'function':{'name':'session_history','arguments':'{}'}}]}
            reason = 'tool_calls'
        else:
            assert body['reasoning'] == {'enabled': True}
            assistant = next(m for m in body['messages'] if m.get('tool_calls'))
            assert not assistant.get('reasoning')
            assert not assistant.get('reasoning_details')
            delta, reason = {'content':'Finished.'}, 'stop'
        return httpx.Response(200, stream=SSEByteStream([
            ('data: '+json.dumps({'choices':[{'delta':delta,'finish_reason':reason}]})+'\n\n').encode(),
            b'data: [DONE]\n\n',
        ]))
    config = OrchestratorConfig(internal_token='internal-token', signing_secret='signing-secret',
        openrouter_api_key='openrouter-test-key', openrouter_mimo_thinking_timeout_sec=0.05,
        task_ledger_db_path=tmp_path/'ledger.db')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rt = OrchestratorRuntime(config, client=client)
        await rt.start()
        try:
            rt._tool_executor.execute = AsyncMock(return_value='{"messages":[]}')
            events = [e async for e in rt.stream_task(_signed_task('signing-secret'))]
        finally:
            await rt.stop()
    assert len(requests) == 3
    assert any(e.get('status') == 'reasoning_budget' for e in events)
    complete = next(e for e in events if e['type']=='response.complete')
    assert complete['content'] == 'Finished.'
    assert complete['metrics']['reasoning_effort'] is None
    assert complete['metrics']['thinking_mode'] == 'bounded'
    assert complete['metrics']['thinking_timeout_sec'] == 0.05
    assert 'Abandoned planning' in complete['thinking_text']


def test_mimo_thinking_budget_defaults_and_env(monkeypatch):
    monkeypatch.delenv('ORCHESTRATOR_MIMO_THINKING_TIMEOUT_SEC', raising=False)
    assert OrchestratorConfig.from_env().openrouter_mimo_thinking_timeout_sec == 30
    monkeypatch.setenv('ORCHESTRATOR_MIMO_THINKING_TIMEOUT_SEC','20')
    assert OrchestratorConfig.from_env().openrouter_mimo_thinking_timeout_sec == 20


@pytest.mark.asyncio
async def test_canceling_mimo_task_does_not_start_a_fallback_request():
    rt = runtime()
    requests = []
    stream = DelayedStream({'reasoning':'Planning'})
    def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=stream)
    rt._record_internal_usage_event = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rt._client = client
        async def consume():
            return [p async for p in rt._stream_openai_chat_events(model_name=rt.config.openrouter_mimo_model,
                messages=[{'role':'user','content':'Test'}], tools=[], usage_context=None)]
        task = asyncio.create_task(consume())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(requests) == 1
    assert stream.closed
