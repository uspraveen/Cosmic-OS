from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.channels.routes import router as channel_router
from gateway.config import GatewayConfig
from gateway.runtime import GatewayRuntime


class FakeDirectAdapter:
    def __init__(self, route: str) -> None:
        self.route = route

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
    ) -> None:
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
    ) -> None:
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


class FakeTelegramChannelAdapter:
    platform = "telegram"

    def __init__(self) -> None:
        self.sent_events: list[dict[str, object]] = []

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
        return {"status": "connected"}

    async def sync_webhook(self) -> dict[str, object]:
        return {"url": "https://example.com/channels/telegram/webhook"}

    async def delete_webhook(self, *, drop_pending_updates: bool = False) -> dict[str, object]:
        return {"url": "", "drop_pending_updates": drop_pending_updates}

    async def send_test_message(self, *, chat_id: int, message: str) -> dict[str, object]:
        return {"chat_id": chat_id, "text": message}

    async def download_file(self, file_id: str) -> tuple[bytes, str | None]:
        return (b"telegram-media", "image/jpeg")


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
        )
    )

    async def fake_start() -> None:
        return

    async def fake_stop() -> None:
        return

    async def fake_classify(*, query: str, conversation_context, max_completion_tokens: int = 430) -> dict:
        return {
            "route": route,
            "needs_latest": False,
            "needs_citations": False,
            "is_task": route == "opus",
            "is_continuation": route == "opus",
            "confidence": 0.91,
            "signals": ["test"],
        }

    async def fake_classify_with_metadata(*, query: str, conversation_context, max_completion_tokens: int = 430) -> dict:
        return {
            "classification": await fake_classify(
                query=query,
                conversation_context=conversation_context,
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
        assert chunk == {
            "type": "response.chunk",
            "request_id": "req_001",
            "session_id": route_result["session_id"],
            "content": "Hello",
            "done": False,
        }

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
        assert resume["history_tail"][-1]["content"] == "Hello from fake adapter"


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
