from __future__ import annotations

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
    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def stream_task(self, task) -> object:
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

    runtime.model_router.start = fake_start
    runtime.model_router.stop = fake_stop
    runtime.model_router.classify = fake_classify
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
