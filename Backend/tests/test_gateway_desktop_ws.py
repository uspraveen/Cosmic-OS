from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.channels.base import ChannelUnavailableError
from gateway.channels.routes import router as channel_router
from gateway.config import GatewayConfig
from gateway.memory_client import MemoryPromptContext
from gateway.runtime import SYSTEM_CRON_DAILY_ROLLOVER, GatewayRuntime
from gateway.session_store import utcnow_iso


class FakeDirectAdapter:
    def __init__(self, route: str) -> None:
        self.route = route
        self.last_memory_context: str | None = None
        self.api_key = "test-anthropic-key"
        self.model = "claude-haiku-4-5"

    async def close(self) -> None:
        return

    async def stream(
        self,
        *,
        request_id: str,
        session_id: str,
        history,
        send,
        store_assistant_message,
        channel: str,
        memory_context: str | None = None,
    ) -> None:
        self.last_memory_context = memory_context
        assert history[-1]["role"] == "user"
        await send(
            {
                "type": "response.chunk",
                "request_id": request_id,
                "session_id": session_id,
                "content": "Hello",
                "done": False,
            }
        )
        await send(
            {
                "type": "response.complete",
                "request_id": request_id,
                "session_id": session_id,
                "content": "Hello from fake adapter",
                "route": self.route,
                "awaiting_reply": False,
                "metrics": {"rtt_ms": 12},
            }
        )
        store_assistant_message(
            "Hello from fake adapter",
            awaiting_reply=False,
            metadata=None,
            channel=channel,
            route=self.route,
        )

    async def generate_text(
        self,
        *,
        system_prompt: str,
        messages,
        max_tokens: int,
    ) -> tuple[str, dict[str, object], str]:
        return (
            "## Summary\n- Daily summary generated for testing.\n",
            {"output_tokens": 64},
            "end_turn",
        )


class FakeOrchestratorClient:
    def __init__(self) -> None:
        self.last_task = None

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def stream_task(self, task) -> object:
        self.last_task = task
        yield {
            "type": "task.created",
            "task_id": task.task_id,
            "session_id": task.session_id,
            "channel": task.channel,
            "route": "opus",
            "status": "running",
        }
        yield {
            "type": "response.thinking.chunk",
            "task_id": task.task_id,
            "request_id": task.input.get("request_id"),
            "session_id": task.session_id,
            "channel": task.channel,
            "content": "Let me think this through.",
            "done": False,
        }
        yield {
            "type": "response.chunk",
            "task_id": task.task_id,
            "request_id": task.input.get("request_id"),
            "session_id": task.session_id,
            "channel": task.channel,
            "content": "Thin Opus answer",
            "done": False,
        }
        yield {
            "type": "response.complete",
            "task_id": task.task_id,
            "request_id": task.input.get("request_id"),
            "session_id": task.session_id,
            "channel": task.channel,
            "content": "Thin Opus answer",
            "route": "opus",
            "awaiting_reply": True,
            "metrics": {"rtt_ms": 32},
        }
        yield {
            "type": "task.completed",
            "task_id": task.task_id,
            "session_id": task.session_id,
            "channel": task.channel,
            "route": "opus",
            "status": "completed",
        }

    async def list_active_tasks(self, *, session_id: str | None = None, channel: str | None = None) -> list[dict]:
        return []

    async def cancel_task(self, task_id: str) -> bool:
        return False


class FakeCancellableDirectAdapter:
    def __init__(self, route: str) -> None:
        self.route = route
        self.started = asyncio.Event()
        self.last_memory_context: str | None = None

    async def close(self) -> None:
        return

    async def stream(
        self,
        *,
        request_id: str,
        session_id: str,
        history,
        send,
        store_assistant_message,
        channel: str,
        memory_context: str | None = None,
    ) -> None:
        self.last_memory_context = memory_context
        assert history[-1]["role"] == "user"
        self.started.set()
        await send(
            {
                "type": "response.chunk",
                "request_id": request_id,
                "session_id": session_id,
                "content": "Partial answer",
                "done": False,
            }
        )
        await asyncio.sleep(60)


class FakeCancellableOrchestratorClient:
    def __init__(self) -> None:
        self._cancellations: dict[str, asyncio.Event] = {}

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def stream_task(self, task) -> object:
        cancel_event = asyncio.Event()
        self._cancellations[task.task_id] = cancel_event
        try:
            yield {
                "type": "task.created",
                "task_id": task.task_id,
                "request_id": task.input.get("request_id"),
                "session_id": task.session_id,
                "channel": task.channel,
                "route": "opus",
                "status": "running",
            }
            await cancel_event.wait()
            yield {
                "type": "task.cancelled",
                "task_id": task.task_id,
                "request_id": task.input.get("request_id"),
                "session_id": task.session_id,
                "channel": task.channel,
                "route": "opus",
                "status": "cancelled",
                "message": "Response stopped.",
            }
        finally:
            self._cancellations.pop(task.task_id, None)

    async def list_active_tasks(self, *, session_id: str | None = None, channel: str | None = None) -> list[dict]:
        return []

    async def cancel_task(self, task_id: str) -> bool:
        cancel_event = self._cancellations.get(task_id)
        if cancel_event is None:
            return False
        cancel_event.set()
        return True


class FakeWhatsAppChannelAdapter:
    platform = "whatsapp"

    def __init__(self) -> None:
        self.sent_events: list[dict[str, object]] = []
        self.allowed_phone = "+12153079021"
        self.connected = True

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def on_message(self, callback) -> None:
        self._callback = callback

    def normalize_message(self, payload: dict[str, object]) -> dict[str, object]:
        sender = payload.get("sender")
        phone = None
        if isinstance(sender, dict):
            phone = sender.get("phone")
        return {
            "content": str(payload.get("text") or ""),
            "session_id": None,
            "channel": f"whatsapp:{phone or '+15551234567'}",
            "metadata": {
                "platform": "whatsapp",
                "message_id": "wamid_test_1",
            },
        }

    async def send(self, message: dict[str, object], channel: str | None = None) -> None:
        self.sent_events.append(
            {
                **message,
                "channel": channel or message.get("channel"),
            }
        )

    async def get_status(self) -> dict[str, object]:
        return {
            "connected": self.connected,
        }

    async def get_config(self) -> dict[str, object]:
        return {
            "allowed_phone": self.allowed_phone,
            "self_chat_only": False,
        }

    async def update_config(
        self,
        *,
        allowed_phone: str | None = None,
        self_chat_only: bool | None = None,
    ) -> dict[str, object]:
        if allowed_phone is not None:
            self.allowed_phone = allowed_phone
        return {
            "allowed_phone": self.allowed_phone,
            "self_chat_only": bool(self_chat_only),
        }


class FakeTelegramChannelAdapter:
    platform = "telegram"

    def __init__(self) -> None:
        self.sent_events: list[dict[str, object]] = []
        self.allowed_chat_id = 12345
        self.allowed_user_id = 12345

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def on_message(self, callback) -> None:
        self._callback = callback

    def verify_webhook_secret(self, provided_secret: str | None) -> None:
        if (provided_secret or "").strip() != "telegram-secret":
            raise PermissionError("Invalid Telegram webhook secret token")

    def normalize_message(self, payload: dict[str, object]) -> dict[str, object] | None:
        message = payload.get("message")
        if not isinstance(message, dict):
            return None
        chat = message.get("chat")
        sender = message.get("from")
        if not isinstance(chat, dict) or not isinstance(sender, dict):
            return None
        if chat.get("type") != "private":
            return None
        chat_id = int(chat.get("id") or 0)
        return {
            "content": str(message.get("text") or ""),
            "session_id": None,
            "channel": f"telegram:chat_{chat_id}",
            "metadata": {
                "platform": "telegram",
                "chat_id": chat_id,
                "user_id": int(sender.get("id") or 0),
                "message_id": int(message.get("message_id") or 0),
            },
        }

    async def send(self, message: dict[str, object], channel: str | None = None) -> None:
        self.sent_events.append(
            {
                **message,
                "channel": channel or message.get("channel"),
            }
        )

    async def get_status(self) -> dict[str, object]:
        return {
            "status": "connected",
            "allowed_chat_id": self.allowed_chat_id,
            "allowed_user_id": self.allowed_user_id,
        }

    async def sync_webhook(self) -> dict[str, object]:
        return {"url": "https://example.com/channels/telegram/webhook"}

    async def delete_webhook(self, *, drop_pending_updates: bool = False) -> dict[str, object]:
        return {"url": "", "drop_pending_updates": drop_pending_updates}

    async def send_test_message(self, *, chat_id: int, message: str) -> dict[str, object]:
        return {"chat_id": chat_id, "text": message}

    async def download_file(self, file_id: str) -> tuple[bytes, str | None]:
        return (b"telegram-media", "image/jpeg")


class FlakyWhatsAppChannelAdapter(FakeWhatsAppChannelAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.fail_response_complete = True

    async def send(self, message: dict[str, object], channel: str | None = None) -> None:
        if self.fail_response_complete and message.get("type") == "response.complete":
            raise ChannelUnavailableError("bridge temporarily unavailable")
        await super().send(message, channel=channel)


class FlakyDesktopChannelAdapter:
    platform = "desktop"

    def __init__(self) -> None:
        self.sent_events: list[dict[str, object]] = []
        self.available = False

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def on_message(self, callback) -> None:
        self._callback = callback

    async def send(self, message: dict[str, object], channel: str | None = None) -> None:
        if not self.available:
            raise ChannelUnavailableError("desktop socket offline")
        self.sent_events.append({**message, "channel": channel or message.get("channel")})


class FakeMemoryClient:
    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = enabled
        self.started = False
        self.prompt_context_calls: list[dict[str, object]] = []
        self.search_calls: list[dict[str, object]] = []
        self.active_search_calls: list[dict[str, object]] = []
        self.write_calls: list[dict[str, object]] = []
        self.core_fact_write_calls: list[dict[str, object]] = []
        self.episode_calls: list[dict[str, object]] = []
        self.core_fact_requests: list[int] = []

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def health(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "status": "ok" if self.enabled else "disabled",
        }

    async def build_prompt_context(
        self,
        *,
        query: str,
        max_results: int,
        token_budget: int,
        core_fact_max_chars: int,
        kinds: tuple[str, ...],
        include_diagnostics: bool = False,
    ) -> MemoryPromptContext:
        self.prompt_context_calls.append(
            {
                "query": query,
                "max_results": max_results,
                "token_budget": token_budget,
                "core_fact_max_chars": core_fact_max_chars,
                "kinds": kinds,
                "include_diagnostics": include_diagnostics,
            }
        )
        if not self.enabled:
            return MemoryPromptContext()
        return MemoryPromptContext(
            core_facts_rendered="- User prefers concise technical answers.",
            recall_items=[
                {
                    "memory_id": "mem_task_1",
                    "kind": "task_summary",
                    "title": "Memory integration work",
                    "content": "We are integrating cosmic-memory into Gateway and should keep the runtime HTTP boundary internal-only.",
                }
            ],
            total_token_count=42,
            rendered=(
                "Relevant long-term memory context for this request.\n"
                "Always-on core facts:\n"
                "- User prefers concise technical answers.\n\n"
                "Retrieved long-term memories:\n"
                "1. [task_summary] Memory integration work\n"
                "We are integrating cosmic-memory into Gateway and should keep the runtime HTTP boundary internal-only."
            ),
        )

    async def passive_search(self, payload: dict[str, object]) -> dict[str, object]:
        self.search_calls.append(payload)
        return {
            "items": [
                {
                    "memory_id": "mem_search_1",
                    "kind": "task_summary",
                    "title": "Search hit",
                    "content": "Gateway should use passive recall.",
                    "score": 0.91,
                }
            ],
            "total_token_count": 32,
        }

    async def active_search(self, payload: dict[str, object]) -> dict[str, object]:
        self.active_search_calls.append(payload)
        return {
            "items": [],
            "entities": [],
            "relations": [],
            "episodes": [],
            "search_plan": [],
        }

    async def write_memory(self, payload: dict[str, object]) -> dict[str, object]:
        self.write_calls.append(payload)
        return {"memory_id": "mem_write_1"}

    async def write_core_fact(self, payload: dict[str, object]) -> dict[str, object]:
        self.core_fact_write_calls.append(payload)
        return {"memory_id": "mem_core_1"}

    async def ingest_episode(self, payload: dict[str, object]) -> dict[str, object]:
        self.episode_calls.append(payload)
        return {
            "record": {"memory_id": "mem_episode_1"},
            "observation_count": len(payload.get("observations", [])) if isinstance(payload, dict) else 0,
            "graph_episode_id": "ep_1",
        }

    async def get_core_fact_block(self, *, max_chars: int = 1500) -> dict[str, object]:
        self.core_fact_requests.append(max_chars)
        return {"items": [], "rendered": "- User prefers concise technical answers."}


class FakeRedis:
    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.groups: dict[tuple[str, str], dict[str, object]] = {}
        self._counter = 0

    async def xgroup_create(self, stream: str, group: str, id: str = "0", mkstream: bool = True) -> None:
        if mkstream:
            self.streams.setdefault(stream, [])
        self.groups.setdefault((stream, group), {"delivered": set(), "acked": set()})

    async def xadd(self, stream: str, fields: dict[str, str]) -> str:
        self._counter += 1
        message_id = f"{self._counter}-0"
        self.streams.setdefault(stream, []).append((message_id, dict(fields)))
        return message_id

    async def xreadgroup(
        self,
        *,
        groupname: str,
        consumername: str,
        streams: dict[str, str],
        count: int | None = None,
        block: int | None = None,
    ) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        del consumername
        results: list[tuple[str, list[tuple[str, dict[str, str]]]]] = []
        limit = count or 10**9
        for stream, start in streams.items():
            state = self.groups.setdefault((stream, groupname), {"delivered": set(), "acked": set()})
            delivered = state["delivered"]
            acked = state["acked"]
            messages: list[tuple[str, dict[str, str]]] = []
            for message_id, payload in self.streams.get(stream, []):
                if start == ">":
                    if message_id in delivered:
                        continue
                    delivered.add(message_id)
                    messages.append((message_id, payload))
                elif start == "0":
                    if message_id in delivered and message_id not in acked:
                        messages.append((message_id, payload))
                else:
                    raise AssertionError(f"Unsupported xreadgroup start value: {start}")
                if len(messages) >= limit:
                    break
            if messages:
                results.append((stream, messages))
        if not results and block:
            await asyncio.sleep(min(block / 1000.0, 0.02))
        return results

    async def xack(self, stream: str, group: str, message_id: str) -> int:
        state = self.groups.setdefault((stream, group), {"delivered": set(), "acked": set()})
        state["acked"].add(message_id)
        return 1

    async def aclose(self) -> None:
        return


def build_runtime(tmp_path, *, route: str = "haiku") -> GatewayRuntime:
    runtime = GatewayRuntime(
        GatewayConfig(
            local_api_token="test-token",
            internal_token="internal-token",
            signing_secret="signing-secret",
            model_router_url="http://127.0.0.1:9999",
            orchestrator_url="http://127.0.0.1:8743",
            enable_whatsapp=False,
            sessions_db_path=tmp_path / "sessions.db",
            routing_audit_db_path=tmp_path / "routing_audit.db",
            artifacts_db_path=tmp_path / "artifacts.db",
            delivery_queue_db_path=tmp_path / "delivery_queue.db",
            scheduler_db_path=tmp_path / "scheduler.db",
        )
    )

    async def fake_start() -> None:
        return

    async def fake_stop() -> None:
        return

    async def fake_classify(
        *,
        query: str,
        conversation_context,
        memory_context: str | None = None,
        max_completion_tokens: int = 430,
    ) -> dict:
        return {
            "route": route,
            "needs_latest": False,
            "needs_citations": False,
            "is_task": route == "opus",
            "is_continuation": route == "opus",
            "confidence": 0.91,
            "signals": ["test"],
        }

    async def fake_classify_with_metadata(
        *,
        query: str,
        conversation_context,
        memory_context: str | None = None,
        max_completion_tokens: int = 430,
    ) -> dict:
        return {
            "classification": await fake_classify(
                query=query,
                conversation_context=conversation_context,
                memory_context=memory_context,
                max_completion_tokens=max_completion_tokens,
            ),
            "metrics": {"rtt_ms": 18.5},
            "classifier_model": "openai/gpt-oss-20b",
            "raw_classifier_output": '{"route":"%s"}' % route,
            "http2_enabled": True,
        }

    runtime.model_router.start = fake_start
    runtime.model_router.stop = fake_stop
    runtime.model_router.classify = fake_classify
    runtime.model_router.classify_with_metadata = fake_classify_with_metadata
    runtime.haiku_adapter = FakeDirectAdapter("haiku")
    runtime.perplexity_adapter = FakeDirectAdapter("perplexity")
    runtime.orchestrator = FakeOrchestratorClient()
    runtime.memory_client = FakeMemoryClient(enabled=False)
    return runtime


@pytest.mark.asyncio
async def test_runtime_uses_shared_daily_session_across_channels(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    await runtime.start()
    try:
        desktop_result = await runtime.process_incoming_user_message(
            {
                "content": "first desktop message",
                "channel": "desktop:desk_a",
                "metadata": {"platform": "desktop"},
            }
        )
        whatsapp_result = await runtime.process_incoming_user_message(
            {
                "content": "follow up from whatsapp",
                "channel": "whatsapp:+15551234567",
                "metadata": {"platform": "whatsapp"},
            }
        )

        assert desktop_result["session_id"] == whatsapp_result["session_id"]
        assert desktop_result["session_id"].startswith("sess_")

        history_tail = runtime.session_store.get_history_tail(desktop_result["session_id"])
        assert [item["channel"] for item in history_tail] == [
            "desktop:desk_a",
            "whatsapp:+15551234567",
        ]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_non_text_inbound_persists_artifacts_and_passes_them_to_opus(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="opus")
    await runtime.start()
    try:
        result = await runtime.process_incoming_user_message(
            {
                "content": "[image]",
                "channel": "whatsapp:+12153079021",
                "metadata": {
                    "platform": "whatsapp",
                    "message_id": "wamid_img_1",
                    "message_type": "image",
                    "attachments": [
                        {
                            "id": "att_1",
                            "kind": "image",
                            "mime_type": "image/jpeg",
                            "filename": "photo.jpg",
                            "caption": "look at this",
                            "size_bytes": 183920,
                            "width": 1280,
                            "height": 720,
                            "sha256": "abc123",
                            "bridge_media_ref": "wamid_img_1:att_1",
                            "download_url": "http://127.0.0.1:8091/media/wamid_img_1/att_1",
                        }
                    ],
                },
            }
        )

        assert result["route"] == "opus"
        assert len(result["input_artifacts"]) == 1
        assert result["input_artifacts"][0]["kind"] == "image"
        assert result["input_artifacts"][0]["bridge_media_ref"] == "wamid_img_1:att_1"

        stored_artifacts = runtime.artifact_store.list_for_request(result["request_id"])
        assert len(stored_artifacts) == 1
        assert stored_artifacts[0]["mime"] == "image/jpeg"
        assert stored_artifacts[0]["source_message_id"] == "wamid_img_1"

        task = runtime._build_orchestrator_task(  # noqa: SLF001 - direct unit seam
            request_record=result,
            session_id=result["session_id"],
            request_id=result["request_id"],
            channel=result["channel"],
        )
        assert len(task.input_artifacts) == 1
        assert task.input_artifacts[0]["caption"] == "look at this"

        history_tail = runtime.session_store.get_history_tail(result["session_id"])
        assert history_tail[-1]["metadata"]["message_type"] == "image"
        assert history_tail[-1]["metadata"]["attachments"][0]["bridge_media_ref"] == "wamid_img_1:att_1"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_adds_memory_context_to_direct_and_orchestrator_paths(tmp_path) -> None:
    direct_runtime = build_runtime(tmp_path / "direct", route="haiku")
    direct_runtime.memory_client = FakeMemoryClient(enabled=True)
    await direct_runtime.start()
    try:
        direct_result = await direct_runtime.process_incoming_user_message(
            {
                "content": "What do you remember about the memory integration?",
                "channel": "desktop:desk_a",
                "metadata": {"platform": "desktop"},
            }
        )
        assert "Relevant long-term memory context" in direct_result["memory_context"]

        await direct_runtime.fulfill_processed_message(direct_result)
        assert "Memory integration work" in (
            direct_runtime.haiku_adapter.last_memory_context or ""
        )
    finally:
        await direct_runtime.stop()

    opus_runtime = build_runtime(tmp_path / "opus", route="opus")
    opus_runtime.memory_client = FakeMemoryClient(enabled=True)
    await opus_runtime.start()
    try:
        opus_result = await opus_runtime.process_incoming_user_message(
            {
                "content": "Continue the integration plan.",
                "channel": "desktop:desk_b",
                "metadata": {"platform": "desktop"},
            }
        )
        task = opus_runtime._build_orchestrator_task(  # noqa: SLF001 - intentional unit seam
            request_record=opus_result,
            session_id=opus_result["session_id"],
            request_id=opus_result["request_id"],
            channel=opus_result["channel"],
        )
        assert "Relevant long-term memory context" in str(task.input.get("memory_context") or "")
    finally:
        await opus_runtime.stop()


@pytest.mark.asyncio
async def test_runtime_ingests_completed_turn_as_episode(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="haiku")
    fake_memory = FakeMemoryClient(enabled=True)
    whatsapp_adapter = FakeWhatsAppChannelAdapter()
    runtime.memory_client = fake_memory
    runtime.registry.register(whatsapp_adapter)
    await whatsapp_adapter.start()
    await runtime.start()
    try:
        result = await runtime.process_incoming_user_message(
            {
                "content": "Remember this exchange.",
                "channel": "whatsapp:+12153079021",
                "metadata": {"platform": "whatsapp"},
            }
        )
        await runtime.fulfill_processed_message(result)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert len(fake_memory.episode_calls) == 1
        episode_payload = fake_memory.episode_calls[0]
        observations = episode_payload["observations"]
        assert observations[0]["role"] == "user"
        assert observations[0]["content"] == "Remember this exchange."
        assert observations[1]["role"] == "assistant"
        assert observations[1]["content"] == "Hello from fake adapter"
        assert episode_payload["kind"] == "transcript"
        assert episode_payload["episode_type"] == "conversation_turn"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_only_ingests_episode_after_delivered_response(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="haiku")
    fake_memory = FakeMemoryClient(enabled=True)
    flaky_channel = FlakyWhatsAppChannelAdapter()
    runtime.memory_client = fake_memory
    runtime.registry.register(flaky_channel)
    await flaky_channel.start()
    await runtime.start()
    try:
        result = await runtime.process_incoming_user_message(
            {
                "content": "Remember this only after delivery.",
                "channel": "whatsapp:+12153079021",
                "metadata": {"platform": "whatsapp"},
            }
        )
        await runtime.fulfill_processed_message(result)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert fake_memory.episode_calls == []

        flaky_channel.fail_response_complete = False
        runtime.notify_channel_active("whatsapp:+12153079021")

        deadline = time.time() + 3.0
        while time.time() < deadline:
            if fake_memory.episode_calls:
                break
            await asyncio.sleep(0.05)

        assert len(fake_memory.episode_calls) == 1
        assert fake_memory.episode_calls[0]["metadata"]["request_id"] == result["request_id"]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_rollover_writes_session_summary_to_memory(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="haiku")
    fake_memory = FakeMemoryClient(enabled=True)
    runtime.memory_client = fake_memory
    await runtime.start()
    try:
        old_session_id = "sess_20000101"
        runtime.session_store.append_message(
            old_session_id,
            role="user",
            content="We decided to ship the memory service.",
            channel="desktop:desk_a",
            metadata={"platform": "desktop"},
        )
        runtime.session_store.append_message(
            old_session_id,
            role="assistant",
            content="I will remember that and follow up tomorrow.",
            route="haiku",
            channel="desktop:desk_a",
            metadata={"platform": "desktop"},
        )

        await runtime._finalize_rollover_sessions(  # noqa: SLF001 - targeted rollover seam
            current_session_id=runtime._current_session_id()
        )

        transcript_path = runtime.config.session_transcript_dir / f"{old_session_id}.md"
        assert transcript_path.exists()
        assert len(fake_memory.write_calls) == 1
        summary_payload = fake_memory.write_calls[0]
        assert summary_payload["kind"] == "session_summary"
        assert summary_payload["metadata"]["session_id"] == old_session_id
        assert old_session_id not in {
            item["session_id"]
            for item in runtime.session_store.list_rollover_candidates(
                current_session_id=runtime._current_session_id()
            )
        }
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_active_working_set_only_keeps_unresolved_open_loops(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="haiku")
    await runtime.start()
    try:
        session_id = runtime._current_session_id()
        request_id = "req_open_loop_1"
        channel = "desktop:desk_a"
        assistant_text = "Can you clarify which repository you want me to use?"
        runtime.session_store.append_message(
            session_id,
            role="user",
            content="Please continue with the migration.",
            channel=channel,
            metadata={"platform": "desktop", "request_id": request_id},
        )
        assistant_message_id = runtime.session_store.append_message(
            session_id,
            role="assistant",
            content=assistant_text,
            route="haiku",
            awaiting_reply=True,
            channel=channel,
            metadata={"platform": "desktop", "request_id": request_id},
        )
        runtime.session_store.upsert_turn_ledger_entry(
            {
                "turn_id": "turn_req_open_loop_1",
                "request_id": request_id,
                "session_id": session_id,
                "channel": channel,
                "route": "haiku",
                "started_at": utcnow_iso(),
                "completed_at": utcnow_iso(),
                "user_goal": "Continue the migration.",
                "user_message_excerpt": "Please continue with the migration.",
                "assistant_outcome": assistant_text,
                "compact_line": "Continue the migration via haiku -> clarification requested",
                "open_loops": [assistant_text],
                "metadata": {"awaiting_reply": True},
            }
        )

        working_set = runtime._refresh_active_working_set(session_id)  # noqa: SLF001 - targeted unit seam
        assert assistant_text in working_set["open_loops"]

        runtime.session_store.clear_awaiting_reply(assistant_message_id)
        refreshed = runtime._refresh_active_working_set(session_id)  # noqa: SLF001 - targeted unit seam
        assert assistant_text not in refreshed["open_loops"]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_session_compaction_only_summarizes_newly_eligible_turns(tmp_path, monkeypatch) -> None:
    runtime = build_runtime(tmp_path, route="haiku")
    summary_prompts: list[str] = []

    async def fake_generate_text(*, system_prompt: str, messages, max_tokens: int) -> tuple[str, dict[str, object], str]:
        summary_prompts.append(str(messages[0]["content"]))
        return (
            "## Goal\n- Preserve the migration context.\n",
            {"output_tokens": 42},
            "end_turn",
        )

    runtime.haiku_adapter.generate_text = fake_generate_text
    monkeypatch.setattr("gateway.runtime.COMPACTION_RECENT_WINDOW_MESSAGES", 2)
    monkeypatch.setattr("gateway.runtime.COMPACTION_RAW_MESSAGE_CHAR_LIMIT", 2000)
    monkeypatch.setattr(runtime, "_compaction_trigger_threshold_tokens", lambda: 1)
    monkeypatch.setattr(runtime, "_conversation_context_budget_tokens", lambda: 100_000)

    def add_turn(index: int) -> None:
        request_id = f"req_compaction_{index}"
        channel = "desktop:desk_a"
        user_text = f"Question {index}: explain the migration plan in detail."
        assistant_text = f"Answer {index}: here is the migration guidance."
        runtime.session_store.append_message(
            session_id,
            role="user",
            content=user_text,
            channel=channel,
            metadata={"platform": "desktop", "request_id": request_id},
        )
        runtime.session_store.append_message(
            session_id,
            role="assistant",
            content=assistant_text,
            route="haiku",
            channel=channel,
            metadata={"platform": "desktop", "request_id": request_id},
        )
        runtime.session_store.upsert_turn_ledger_entry(
            {
                "turn_id": f"turn_{request_id}",
                "request_id": request_id,
                "session_id": session_id,
                "channel": channel,
                "route": "haiku",
                "started_at": utcnow_iso(),
                "completed_at": utcnow_iso(),
                "user_goal": user_text,
                "user_message_excerpt": user_text,
                "assistant_outcome": assistant_text,
                "compact_line": f"{user_text} via haiku -> {assistant_text}",
                "accomplished": [assistant_text],
                "metadata": {},
            }
        )
        time.sleep(0.01)

    await runtime.start()
    try:
        session_id = runtime._current_session_id()
        add_turn(1)
        add_turn(2)
        add_turn(3)

        await runtime._maybe_compact_session(session_id)  # noqa: SLF001 - targeted unit seam
        assert len(summary_prompts) == 1
        first_packet = runtime.get_session_state(session_id)["compaction_packet"]
        assert first_packet["compacted_until_completed_at"]

        await runtime._maybe_compact_session(session_id)  # noqa: SLF001 - targeted unit seam
        assert len(summary_prompts) == 1

        add_turn(4)
        await runtime._maybe_compact_session(session_id)  # noqa: SLF001 - targeted unit seam
        assert len(summary_prompts) == 2
        assert "Question 3" in summary_prompts[1]
        assert "Question 1" not in summary_prompts[1]
    finally:
        await runtime.stop()


@pytest.fixture
def test_client(tmp_path):
    runtime = build_runtime(tmp_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway_runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(channel_router)

    with TestClient(app) as client:
        yield client


def test_internal_memory_routes_proxy_to_memory_service(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    runtime.memory_client = FakeMemoryClient(enabled=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway_runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(channel_router)
    from gateway.memory_routes import router as memory_router

    app.include_router(memory_router)

    with TestClient(app) as client:
        search_response = client.post(
            "/internal/memory/search",
            headers={"X-Internal-Token": "internal-token"},
            json={
                "query": "memory integration",
                "kinds": ["task_summary"],
                "max_results": 4,
            },
        )
        assert search_response.status_code == 200
        payload = search_response.json()
        assert payload["items"][0]["memory_id"] == "mem_search_1"
        assert runtime.memory_client.search_calls[0]["query"] == "memory integration"

        core_fact_response = client.get(
            "/internal/memory/core-facts",
            headers={"X-Internal-Token": "internal-token"},
            params={"max_chars": 900},
        )
        assert core_fact_response.status_code == 200
        assert runtime.memory_client.core_fact_requests == [900]


def test_internal_session_routes_expose_state_turns_and_revisit(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="haiku")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway_runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(channel_router)
    from gateway.memory_routes import router as memory_router

    app.include_router(memory_router)

    with TestClient(app) as client:
        session_id = runtime._current_session_id()
        runtime.session_store.update_session_metadata(
            session_id,
            {
                "active_working_set": {
                    "session_id": session_id,
                    "goal": "Keep the migration plan moving.",
                    "active_workstreams": ["Finish memory integration"],
                    "recent_decisions": [],
                    "open_loops": ["Confirm rollout timing"],
                    "current_focus_entities": [],
                    "active_task_refs": ["task_123"],
                    "pending_artifact_pointers": [],
                    "user_preferences_in_play": ["Concise answers"],
                    "last_updated_at": utcnow_iso(),
                }
            },
        )
        runtime.session_store.append_message(
            session_id,
            role="user",
            content="Continue the migration.",
            channel="desktop:desk_a",
            metadata={"platform": "desktop", "request_id": "req_revisit_1"},
        )
        runtime.session_store.append_message(
            session_id,
            role="assistant",
            content="I will continue the migration and report back.",
            route="haiku",
            channel="desktop:desk_a",
            metadata={"platform": "desktop", "request_id": "req_revisit_1"},
        )
        runtime.session_store.upsert_turn_ledger_entry(
            {
                "turn_id": "turn_req_revisit_1",
                "request_id": "req_revisit_1",
                "session_id": session_id,
                "task_id": "task_123",
                "channel": "desktop:desk_a",
                "route": "haiku",
                "started_at": utcnow_iso(),
                "completed_at": utcnow_iso(),
                "user_goal": "Continue the migration.",
                "user_message_excerpt": "Continue the migration.",
                "assistant_outcome": "I will continue the migration and report back.",
                "compact_line": "Continue the migration via haiku -> I will continue the migration and report back.",
                "task_refs": ["task_123"],
                "metadata": {},
            }
        )
        runtime.session_store.upsert_task_notebook(
            "task_123",
            session_id,
            {
                "task_id": "task_123",
                "status": "active",
                "goal": "Finish the migration",
                "current_state": "Waiting on final verification",
                "open_questions": ["Confirm rollout timing"],
                "created_at": utcnow_iso(),
            },
        )

        state_response = client.get(
            f"/internal/session/state/{session_id}",
            headers={"X-Internal-Token": "internal-token"},
        )
        assert state_response.status_code == 200
        assert state_response.json()["active_working_set"]["goal"] == "Keep the migration plan moving."

        turns_response = client.get(
            f"/internal/session/turns/{session_id}",
            headers={"X-Internal-Token": "internal-token"},
            params={"limit": 5},
        )
        assert turns_response.status_code == 200
        assert turns_response.json()["turns"][0]["request_id"] == "req_revisit_1"

        notebook_response = client.get(
            "/internal/session/task-notebook/task_123",
            headers={"X-Internal-Token": "internal-token"},
        )
        assert notebook_response.status_code == 200
        assert notebook_response.json()["current_state"] == "Waiting on final verification"

        revisit_response = client.post(
            "/internal/session/revisit",
            headers={"X-Internal-Token": "internal-token"},
            json={
                "session_id": session_id,
                "task_id": "task_123",
                "request_id": "req_revisit_1",
                "turn_limit": 5,
                "raw_history_limit": 5,
            },
        )
        assert revisit_response.status_code == 200
        revisit_payload = revisit_response.json()
        assert revisit_payload["turn"]["request_id"] == "req_revisit_1"
        assert revisit_payload["task_notebook"]["task_id"] == "task_123"
        assert revisit_payload["raw_history"][-1]["content"] == "I will continue the migration and report back."


def test_desktop_websocket_supports_ping_query_and_resume(test_client: TestClient) -> None:
    with test_client.websocket_connect("/ws?token=test-token&device_id=desk_a1b2") as websocket:
        websocket.send_json({"type": "ping", "ts_unix_ms": 12345})
        pong = websocket.receive_json()
        assert pong["type"] == "pong"
        assert pong["ts_unix_ms"] == 12345

        websocket.send_json(
            {
                "type": "query",
                "request_id": "req_001",
                "content": "Hello from desktop",
                "conversation_context": [
                    {"role": "user", "content": "Prior context"},
                ],
            }
        )
        route_result = websocket.receive_json()
        assert route_result["type"] == "route_result"
        assert route_result["request_id"] == "req_001"
        assert route_result["route"] == "haiku"
        assert route_result["channel"] == "desktop:desk_a1b2"

        chunk = websocket.receive_json()
        assert chunk["type"] == "response.chunk"
        assert chunk["request_id"] == "req_001"
        assert chunk["session_id"] == route_result["session_id"]
        assert chunk["content"] == "Hello"
        assert chunk["done"] is False
        assert chunk["channel"] == "desktop:desk_a1b2"

        complete = websocket.receive_json()
        assert complete["type"] == "response.complete"
        assert complete["request_id"] == "req_001"
        assert complete["session_id"] == route_result["session_id"]
        assert complete["route"] == "haiku"
        assert complete["content"] == "Hello from fake adapter"

        websocket.send_json(
            {
                "type": "resume",
                "request_id": "resume_001",
                "session_id": route_result["session_id"],
                "known_task_ids": [],
            }
        )
        resume = websocket.receive_json()
        assert resume["type"] == "resume.ok"
        assert resume["request_id"] == "resume_001"
        assert resume["session_id"] == route_result["session_id"]
        assert resume["channel"] == "desktop:desk_a1b2"
        assert resume["user_timezone"] == "America/Chicago"
        assert resume["history_tail"][-1]["content"] == "Hello from fake adapter"


@pytest.mark.asyncio
async def test_runtime_uses_reported_desktop_timezone_for_session_rollover_and_cron(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    await runtime.start()
    try:
        await runtime.update_user_timezone("Asia/Kolkata", source="desktop")

        session_id = runtime._current_session_id(
            datetime(2026, 3, 12, 1, 0, tzinfo=timezone.utc)
        )
        assert session_id == "sess_20260312"

        profile = runtime.scheduler_store.get_profile()
        assert profile["user_timezone"] == "Asia/Kolkata"
        assert profile["timezone_source"] == "desktop"

        rollover_cron = runtime.scheduler_store.get_cron(SYSTEM_CRON_DAILY_ROLLOVER)
        assert rollover_cron is not None
        assert rollover_cron["timezone"] == "Asia/Kolkata"
        assert rollover_cron["cron_expr"] == "0 4 * * *"
    finally:
        await runtime.stop()


def test_desktop_resume_updates_scheduler_timezone_profile(test_client: TestClient) -> None:
    runtime = test_client.app.state.gateway_runtime

    with test_client.websocket_connect("/ws?token=test-token&device_id=desk_tz") as websocket:
        websocket.send_json(
            {
                "type": "resume",
                "request_id": "resume_tz_001",
                "timezone": "Asia/Kolkata",
                "session_id": "",
                "known_task_ids": [],
            }
        )
        resume = websocket.receive_json()

    assert resume["type"] == "resume.ok"
    assert resume["user_timezone"] == "Asia/Kolkata"
    assert runtime.scheduler_store.get_profile()["user_timezone"] == "Asia/Kolkata"


def test_channels_endpoint_lists_desktop(test_client: TestClient) -> None:
    response = test_client.get(
        "/channels",
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "channels": [
            {
                "platform": "desktop",
                "configured": True,
                "healthy": True,
                "last_error": None,
            }
        ]
    }


def test_sessions_endpoints_return_vm_history(test_client: TestClient) -> None:
    with test_client.websocket_connect("/ws?token=test-token&device_id=desk_history") as websocket:
        websocket.send_json(
            {
                "type": "query",
                "request_id": "req_history",
                "content": "Save this to shared history",
            }
        )
        route_result = websocket.receive_json()
        assert route_result["type"] == "route_result"
        websocket.receive_json()
        websocket.receive_json()

    sessions_response = test_client.get(
        "/sessions",
        headers={"Authorization": "Bearer test-token"},
    )
    assert sessions_response.status_code == 200
    sessions = sessions_response.json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["id"] == route_result["session_id"]

    history_response = test_client.get(
        f"/sessions/{route_result['session_id']}",
        headers={"Authorization": "Bearer test-token"},
    )
    assert history_response.status_code == 200
    history_payload = history_response.json()
    assert history_payload["session_id"] == route_result["session_id"]
    assert [item["role"] for item in history_payload["messages"]] == ["user", "assistant"]


def test_routing_audit_endpoint_returns_effective_route_details(test_client: TestClient) -> None:
    with test_client.websocket_connect("/ws?token=test-token&device_id=desk_audit") as websocket:
        websocket.send_json(
            {
                "type": "query",
                "request_id": "req_audit",
                "content": "what is usd to inr today?",
            }
        )
        websocket.receive_json()
        websocket.receive_json()
        websocket.receive_json()

    response = test_client.get(
        "/routing-audit?limit=5",
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["entries"]) == 1
    entry = payload["entries"][0]
    assert entry["request_id"] == "req_audit"
    assert entry["decision_source"] == "model_router"
    assert entry["final_route"] == "haiku"
    assert entry["classifier_route"] == "haiku"
    assert entry["classifier_model"] == "openai/gpt-oss-20b"
    assert entry["classifier_metrics"] == {"rtt_ms": 18.5}
    assert entry["query_text"] == "what is usd to inr today?"


def test_scheduler_endpoints_list_and_pause_resume_system_cron(test_client: TestClient) -> None:
    overview = test_client.get(
        "/scheduler/overview",
        headers={"Authorization": "Bearer test-token"},
    )
    assert overview.status_code == 200
    overview_payload = overview.json()
    assert overview_payload["profile"]["user_timezone"] == "America/Chicago"
    assert any(item["cron_id"] == SYSTEM_CRON_DAILY_ROLLOVER for item in overview_payload["crons"])

    cron_response = test_client.get(
        f"/scheduler/crons/{SYSTEM_CRON_DAILY_ROLLOVER}",
        headers={"Authorization": "Bearer test-token"},
    )
    assert cron_response.status_code == 200
    assert cron_response.json()["cron_id"] == SYSTEM_CRON_DAILY_ROLLOVER

    paused = test_client.post(
        f"/scheduler/crons/{SYSTEM_CRON_DAILY_ROLLOVER}/pause",
        headers={"Authorization": "Bearer test-token"},
        json={"reason": "testing"},
    )
    assert paused.status_code == 200
    assert paused.json()["paused"] is True
    assert paused.json()["pause_reason"] == "testing"

    resumed = test_client.post(
        f"/scheduler/crons/{SYSTEM_CRON_DAILY_ROLLOVER}/resume",
        headers={"Authorization": "Bearer test-token"},
    )
    assert resumed.status_code == 200
    assert resumed.json()["paused"] is False

    heartbeat = test_client.get(
        "/scheduler/heartbeat",
        headers={"Authorization": "Bearer test-token"},
    )
    assert heartbeat.status_code == 200
    assert heartbeat.json()["timezone"] == "America/Chicago"


def test_whatsapp_incoming_emits_route_result_before_async_fulfillment(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="haiku")
    whatsapp_adapter = FakeWhatsAppChannelAdapter()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway_runtime = runtime
        await runtime.start()
        runtime.registry.register(whatsapp_adapter)
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(channel_router)

    with TestClient(app) as client:
        response = client.post(
            "/internal/channels/whatsapp/incoming",
            headers={"X-Internal-Token": "internal-token"},
            json={
                "sender": {"phone": "+12153079021"},
                "text": "hello from whatsapp",
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "accepted"
        assert payload["route"] == "haiku"

        deadline = time.time() + 2.0
        while len(whatsapp_adapter.sent_events) < 3 and time.time() < deadline:
            time.sleep(0.01)

    assert [event["type"] for event in whatsapp_adapter.sent_events[:3]] == [
        "route_result",
        "response.chunk",
        "response.complete",
    ]
    assert whatsapp_adapter.sent_events[0]["request_id"] == payload["request_id"]


def test_telegram_webhook_emits_route_result_before_async_fulfillment(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="haiku")
    telegram_adapter = FakeTelegramChannelAdapter()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway_runtime = runtime
        await runtime.start()
        runtime.registry.register(telegram_adapter)
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(channel_router)

    with TestClient(app) as client:
        response = client.post(
            "/channels/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
            json={
                "update_id": 1,
                "message": {
                    "message_id": 42,
                    "chat": {"id": 12345, "type": "private"},
                    "from": {"id": 12345},
                    "text": "hello from telegram",
                },
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "accepted"

        deadline = time.time() + 2.0
        while len(telegram_adapter.sent_events) < 3 and time.time() < deadline:
            time.sleep(0.01)

        assert [event["type"] for event in telegram_adapter.sent_events[:3]] == [
            "route_result",
            "response.chunk",
            "response.complete",
        ]
        assert telegram_adapter.sent_events[0]["request_id"] == payload["request_id"]


def test_whatsapp_activation_sends_welcome_once_on_runtime_start(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="haiku")
    whatsapp_adapter = FakeWhatsAppChannelAdapter()
    runtime.registry.register(whatsapp_adapter)
    asyncio.run(runtime.start())
    try:
        welcome_events = [event for event in whatsapp_adapter.sent_events if event.get("type") == "channel.welcome"]
        assert len(welcome_events) == 1
        assert welcome_events[0]["channel"] == "whatsapp:+12153079021"
        assert welcome_events[0]["content"] == "COSMIC is connected on WhatsApp. You can message me here anytime."
    finally:
        asyncio.run(runtime.stop())


def test_telegram_activation_sends_welcome_once_on_runtime_start(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="haiku")
    telegram_adapter = FakeTelegramChannelAdapter()
    runtime.registry.register(telegram_adapter)
    asyncio.run(runtime.start())
    try:
        welcome_events = [event for event in telegram_adapter.sent_events if event.get("type") == "channel.welcome"]
        assert len(welcome_events) == 1
        assert welcome_events[0]["channel"] == "telegram:chat_12345"
        assert welcome_events[0]["content"] == "COSMIC is connected on Telegram. You can message me here anytime."
    finally:
        asyncio.run(runtime.stop())


@pytest.mark.asyncio
async def test_whatsapp_final_response_is_queued_and_retried_when_channel_send_fails(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="haiku")
    flaky_adapter = FlakyWhatsAppChannelAdapter()
    runtime.registry.register(flaky_adapter)
    await runtime.start()
    try:
        processed = await runtime.process_incoming_user_message(
            {
                "content": "hello from whatsapp",
                "channel": "whatsapp:+12153079021",
                "metadata": {"platform": "whatsapp"},
            }
        )
        runtime.start_request_fulfillment(processed)

        deadline = time.time() + 2.0
        while time.time() < deadline:
            if any(event.get("type") == "response.chunk" for event in flaky_adapter.sent_events):
                break
            await asyncio.sleep(0.01)

        assert any(event.get("type") == "response.chunk" for event in flaky_adapter.sent_events)
        assert not any(event.get("type") == "response.complete" for event in flaky_adapter.sent_events)
        assert runtime.delivery_queue_store.summary()["pending_count"] == 1

        flaky_adapter.fail_response_complete = False
        runtime.notify_channel_active("whatsapp:+12153079021")

        deadline = time.time() + 3.0
        while time.time() < deadline:
            if any(event.get("type") == "response.complete" for event in flaky_adapter.sent_events):
                break
            await asyncio.sleep(0.05)

        assert any(event.get("type") == "response.complete" for event in flaky_adapter.sent_events)
        assert runtime.delivery_queue_store.summary()["pending_count"] == 0
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_offline_desktop_task_input_is_held_until_channel_returns(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="haiku")
    desktop_adapter = FlakyDesktopChannelAdapter()
    runtime.registry.register(desktop_adapter)
    await runtime.start()
    try:
        event = {
            "type": "task.input_required",
            "request_id": "req_input_hold",
            "task_id": "tsk_input_hold",
            "session_id": "sess_20260310",
            "channel": "desktop:desk_hold",
            "question": "Pick one option.",
            "options": ["A", "B"],
        }

        await runtime.deliver_channel_event(event)
        assert runtime.delivery_queue_store.summary()["pending_count"] == 1
        assert desktop_adapter.sent_events == []

        desktop_adapter.available = True
        runtime.notify_channel_active("desktop:desk_hold")

        deadline = time.time() + 3.0
        while time.time() < deadline:
            if desktop_adapter.sent_events:
                break
            await asyncio.sleep(0.05)

        assert desktop_adapter.sent_events[0]["type"] == "task.input_required"
        assert runtime.delivery_queue_store.summary()["pending_count"] == 0
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_whatsapp_message_auto_submits_pending_task_input_reply(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="perplexity")
    runtime._redis = FakeRedis()
    await runtime.start()
    try:
        session_id = runtime._current_session_id()
        runtime.session_store.upsert_task_input_request(
            input_request_id="uir_whatsapp_1",
            task_id="tsk_waiting_whatsapp",
            session_id=session_id,
            channel="whatsapp:+12153079021",
            question="Which repository should I use?",
            options=["Cosmic-OS", "cosmic-memory"],
            agent="cosmic/orchestrator:1.0.0",
            metadata={},
            status="pending",
            created_at=utcnow_iso(),
        )

        result = await runtime.process_incoming_user_message(
            {
                "content": "Use Cosmic-OS.",
                "channel": "whatsapp:+12153079021",
                "metadata": {"platform": "whatsapp", "message_id": "wamid_reply_1"},
            }
        )

        assert result["dispatch_target"] == "redis"
        assert result["route"] == "task_input_reply"
        reply_stream = runtime._redis.streams[runtime.config.task_input_replies_stream]
        reply_payload = json.loads(reply_stream[0][1]["payload"])
        assert reply_payload["input_request_id"] == "uir_whatsapp_1"
        assert reply_payload["task_id"] == "tsk_waiting_whatsapp"
        assert reply_payload["content"] == "Use Cosmic-OS."

        stored = runtime.session_store.get_task_input_request("uir_whatsapp_1")
        assert stored is not None
        assert stored["status"] == "answered"
        assert stored["reply_content"] == "Use Cosmic-OS."
    finally:
        await runtime.stop()


def test_desktop_websocket_accepts_task_input_reply_messages(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="haiku")
    runtime._redis = FakeRedis()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway_runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(channel_router)

    with TestClient(app) as client:
        session_id = runtime._current_session_id()
        runtime.session_store.upsert_task_input_request(
            input_request_id="uir_desktop_1",
            task_id="tsk_waiting_desktop",
            session_id=session_id,
            channel="desktop:desk_input",
            question="Which environment should I target?",
            options=["staging", "production"],
            agent="cosmic/orchestrator:1.0.0",
            metadata={},
            status="pending",
            created_at=utcnow_iso(),
        )

        with client.websocket_connect("/ws?token=test-token&device_id=desk_input") as websocket:
            websocket.send_json(
                {
                    "type": "task.input_reply",
                    "request_id": "reply_req_1",
                    "input_request_id": "uir_desktop_1",
                    "task_id": "tsk_waiting_desktop",
                    "content": "Use staging.",
                }
            )
            accepted = websocket.receive_json()

        assert accepted["type"] == "task.input_reply.accepted"
        assert accepted["request_id"] == "reply_req_1"
        assert accepted["input_request_id"] == "uir_desktop_1"
        assert accepted["task_id"] == "tsk_waiting_desktop"

        reply_stream = runtime._redis.streams[runtime.config.task_input_replies_stream]
        reply_payload = json.loads(reply_stream[0][1]["payload"])
        assert reply_payload["channel"] == "desktop:desk_input"
        assert reply_payload["content"] == "Use staging."

        stored = runtime.session_store.get_task_input_request("uir_desktop_1")
        assert stored is not None
        assert stored["status"] == "answered"
        assert stored["reply_content"] == "Use staging."


def test_internal_telegram_media_route_uses_internal_token(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    telegram_adapter = FakeTelegramChannelAdapter()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway_runtime = runtime
        await runtime.start()
        runtime.registry.register(telegram_adapter)
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(channel_router)

    with TestClient(app) as client:
        response = client.get(
            "/internal/channels/telegram/media/file_123",
            headers={"X-Internal-Token": "internal-token"},
        )
        assert response.status_code == 200
        assert response.content == b"telegram-media"
        assert response.headers["content-type"].startswith("image/jpeg")


def test_desktop_websocket_streams_thin_opus_route(test_client: TestClient, tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="opus")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway_runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(channel_router)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?token=test-token&device_id=desk_opus") as websocket:
            websocket.send_json(
                {
                    "type": "query",
                    "request_id": "req_opus",
                    "content": "Plan this for me",
                }
            )
            route_result = websocket.receive_json()
            assert route_result["type"] == "route_result"
            assert route_result["route"] == "opus"

            created = websocket.receive_json()
            assert created["type"] == "task.created"
            assert created["route"] == "opus"

            thinking = websocket.receive_json()
            assert thinking["type"] == "response.thinking.chunk"
            assert thinking["content"] == "Let me think this through."

            chunk = websocket.receive_json()
            assert chunk["type"] == "response.chunk"
            assert chunk["content"] == "Thin Opus answer"

            complete = websocket.receive_json()
            assert complete["type"] == "response.complete"
            assert complete["route"] == "opus"
            assert complete["awaiting_reply"] is True

            completed = websocket.receive_json()
            assert completed["type"] == "task.completed"


def test_desktop_websocket_route_override_bypasses_classifier(test_client: TestClient, tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="opus")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway_runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(channel_router)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?token=test-token&device_id=desk_override") as websocket:
            websocket.send_json(
                {
                    "type": "query",
                    "request_id": "req_override",
                    "content": "Force Perplexity",
                    "route_override": "perplexity",
                }
            )
            route_result = websocket.receive_json()
            assert route_result["type"] == "route_result"
            assert route_result["route"] == "perplexity"
            assert route_result["classification"]["signals"] == ["manual_route_override"]

            chunk = websocket.receive_json()
            assert chunk["type"] == "response.chunk"

            complete = websocket.receive_json()
            assert complete["type"] == "response.complete"
            assert complete["route"] == "perplexity"

        response = client.get(
            "/routing-audit?limit=5",
            headers={"Authorization": "Bearer test-token"},
        )
        assert response.status_code == 200
        entry = response.json()["entries"][0]
        assert entry["request_id"] == "req_override"
        assert entry["decision_source"] == "manual_override"
        assert entry["route_override"] == "perplexity"
        assert entry["final_route"] == "perplexity"
        assert entry["sticky_hit"] is False


def test_desktop_websocket_cancel_stops_direct_stream(test_client: TestClient, tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="haiku")
    runtime.haiku_adapter = FakeCancellableDirectAdapter("haiku")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway_runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(channel_router)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?token=test-token&device_id=desk_cancel_direct") as websocket:
            websocket.send_json(
                {
                    "type": "query",
                    "request_id": "req_cancel_direct",
                    "content": "Start a long answer",
                }
            )
            route_result = websocket.receive_json()
            assert route_result["type"] == "route_result"
            chunk = websocket.receive_json()
            assert chunk["type"] == "response.chunk"
            assert chunk["content"] == "Partial answer"

            websocket.send_json(
                {
                    "type": "cancel",
                    "request_id": "cancel_direct_001",
                    "target_request_id": "req_cancel_direct",
                }
            )
            cancelled = websocket.receive_json()
            assert cancelled["type"] == "task.cancelled"
            assert cancelled["request_id"] == "req_cancel_direct"
            assert cancelled["task_id"] is None
            assert cancelled["route"] == "haiku"


def test_desktop_websocket_cancel_stops_opus_stream(test_client: TestClient, tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="opus")
    runtime.orchestrator = FakeCancellableOrchestratorClient()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway_runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(channel_router)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?token=test-token&device_id=desk_cancel_opus") as websocket:
            websocket.send_json(
                {
                    "type": "query",
                    "request_id": "req_cancel_opus",
                    "content": "Start an opus task",
                }
            )
            route_result = websocket.receive_json()
            assert route_result["type"] == "route_result"
            created = websocket.receive_json()
            assert created["type"] == "task.created"

            websocket.send_json(
                {
                    "type": "cancel",
                    "request_id": "cancel_opus_001",
                    "task_id": created["task_id"],
                    "target_request_id": "req_cancel_opus",
                }
            )
            cancelled = websocket.receive_json()
            assert cancelled["type"] == "task.cancelled"
            assert cancelled["task_id"] == created["task_id"]
            assert cancelled["request_id"] == "req_cancel_opus"
            assert cancelled["route"] == "opus"
