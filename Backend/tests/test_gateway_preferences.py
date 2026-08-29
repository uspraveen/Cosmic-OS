from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager
from contextlib import contextmanager
from pathlib import Path
from shutil import rmtree
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.channels.routes import router as channel_router
from gateway.config import GatewayConfig
from gateway.memory.client import MemoryPromptContext
from gateway.preferences.routes import router as preferences_router
from gateway.runtime import GatewayRuntime

_LOCAL_TMP_ROOT = Path(r"C:\Users\Praveen Raj U S\.codex\memories\gateway-preferences-tests")
_LOCAL_TMP_ROOT.mkdir(parents=True, exist_ok=True)


@contextmanager
def _runtime_root():
    root = _LOCAL_TMP_ROOT / f"gw-preferences-{uuid4().hex}"
    root.mkdir(parents=True, exist_ok=False)
    try:
        yield root
    finally:
        rmtree(root, ignore_errors=True)


class FakeMemoryClient:
    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = enabled

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

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
        del query, max_results, token_budget, core_fact_max_chars, kinds, include_diagnostics
        return MemoryPromptContext()


def build_runtime(tmp_path: Path, *, route: str = "haiku") -> GatewayRuntime:
    runtime = GatewayRuntime(
        GatewayConfig(
            local_api_token="test-token",
            internal_token="internal-token",
            signing_secret="signing-secret",
            model_router_url="http://127.0.0.1:9999",
            orchestrator_url="http://127.0.0.1:8743",
            enable_whatsapp=False,
            preferences_db_path=tmp_path / "preferences.db",
            sessions_db_path=tmp_path / "sessions.db",
            routing_audit_db_path=tmp_path / "routing_audit.db",
            artifacts_db_path=tmp_path / "artifacts.db",
            delivery_queue_db_path=tmp_path / "delivery_queue.db",
            scheduler_db_path=tmp_path / "scheduler.db",
            memory_write_audit_db_path=tmp_path / "memory_write_audit.db",
        )
    )

    async def fake_start() -> None:
        return

    async def fake_stop() -> None:
        return

    async def fake_initialize() -> None:
        return

    async def fake_classify(
        *,
        query: str,
        conversation_context,
        memory_context: str | None = None,
        max_completion_tokens: int = 430,
    ) -> dict[str, object]:
        del query, conversation_context, memory_context, max_completion_tokens
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
    ) -> dict[str, object]:
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
    runtime.orchestrator.start = fake_start
    runtime.orchestrator.stop = fake_stop
    runtime.memory_client = FakeMemoryClient(enabled=False)
    runtime.capability_wishlist_service.initialize = fake_initialize
    return runtime


def test_desktop_preferences_routes_patch_and_broadcast() -> None:
    with _runtime_root() as root:
        runtime = build_runtime(root)

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
        app.include_router(preferences_router)

        with TestClient(app) as client:
            with client.websocket_connect("/ws?token=test-token&device_id=desk_pref_1") as websocket:
                initial = client.get(
                    "/desktop/preferences",
                    headers={"Authorization": "Bearer test-token"},
                )
                assert initial.status_code == 200
                initial_payload = initial.json()
                assert initial_payload["visual_response_enhancement"]["enabled"] is True
                assert initial_payload["visual_response_enhancement"]["revision"] == 1
                assert initial_payload["cosmic_orchestrator_model"]["provider"] == "fireworks_glm"
                assert (
                    initial_payload["cosmic_orchestrator_model"]["model"]
                    == "accounts/fireworks/models/glm-5p3-flash"
                )

                updated = client.patch(
                    "/desktop/preferences",
                    headers={
                        "Authorization": "Bearer test-token",
                        "X-Device-Id": "desk_pref_1",
                    },
                    json={"visual_response_enhancement_enabled": False},
                )
                assert updated.status_code == 200
                updated_payload = updated.json()
                assert updated_payload["visual_response_enhancement"]["enabled"] is False
                assert updated_payload["visual_response_enhancement"]["revision"] == 2
                assert (
                    updated_payload["visual_response_enhancement"]["updated_source"]
                    == "desktop_settings"
                )
                assert (
                    updated_payload["visual_response_enhancement"]["updated_device_id"]
                    == "desk_pref_1"
                )
                assert updated_payload["cosmic_orchestrator_model"]["provider"] == "fireworks_glm"

                event = websocket.receive_json()
                assert event["type"] == "preferences.updated"
                assert (
                    event["preferences"]["visual_response_enhancement"]["enabled"]
                    is False
                )
                assert (
                    event["preferences"]["visual_response_enhancement"]["revision"]
                    == 2
                )
                assert (
                    event["preferences"]["cosmic_orchestrator_model"]["provider"]
                    == "fireworks_glm"
                )

                reloaded = client.get(
                    "/desktop/preferences",
                    headers={"Authorization": "Bearer test-token"},
                )
                assert reloaded.status_code == 200
                assert (
                    reloaded.json()["visual_response_enhancement"]["enabled"] is False
                )


def test_desktop_preferences_snapshot_falls_back_when_store_read_fails() -> None:
    with _runtime_root() as root:
        runtime = build_runtime(root)
        runtime.preference_store.get_visual_response_enhancement = lambda: (_ for _ in ()).throw(
            RuntimeError("preferences read failed")
        )

        snapshot = runtime.get_desktop_preferences_snapshot()

        assert snapshot["visual_response_enhancement"]["enabled"] is True
        assert snapshot["visual_response_enhancement"]["revision"] == 1
        assert (
            snapshot["visual_response_enhancement"]["updated_source"]
            == "runtime_fallback"
        )
        assert snapshot["visual_response_enhancement"]["updated_device_id"] is None
        assert snapshot["visual_response_enhancement"]["updated_at"]
        assert snapshot["cosmic_orchestrator_model"]["provider"] == "fireworks_glm"
        assert (
            snapshot["cosmic_orchestrator_model"]["model"]
            == "accounts/fireworks/models/glm-5p3-flash"
        )


def test_alpha_execution_provider_preference_defaults_and_updates() -> None:
    with _runtime_root() as root:
        runtime = build_runtime(root)
        runtime.preference_store.initialize()

        initial = runtime.preference_store.get_alpha_execution_provider()
        assert initial["preferred_harness"] == "opencode"
        assert initial["revision"] == 1

        updated = runtime.preference_store.set_alpha_execution_provider(
            "cursor",
            source="test",
            device_id="desk_alpha_1",
        )

        assert updated["preferred_harness"] == "cursor"
        assert updated["revision"] == 2
        assert updated["updated_source"] == "test"
        assert updated["updated_device_id"] == "desk_alpha_1"

        # An explicit user choice survives reads unchanged.
        reloaded = runtime.preference_store.get_alpha_execution_provider()
        assert reloaded["preferred_harness"] == "cursor"


def test_alpha_execution_provider_migrates_untouched_codex_seed_to_opencode() -> None:
    """VMs provisioned before OpenCode keep a factory-seeded `codex` row.

    That value was never chosen by anyone: migrate it to the new default on
    read. A row updated by any non-system source is user intent and stays.
    """
    with _runtime_root() as root:
        runtime = build_runtime(root)
        runtime.preference_store.initialize()
        connection = sqlite3.connect(runtime.preference_store.db_path)
        try:
            connection.execute(
                """
                UPDATE app_preferences
                SET value_json = '{"preferred_harness": "codex"}',
                    updated_source = 'system_default'
                WHERE key = 'alpha_execution_provider'
                """
            )
            connection.commit()
        finally:
            connection.close()

        migrated = runtime.preference_store.get_alpha_execution_provider()
        assert migrated["preferred_harness"] == "opencode"

        # Once the user (or an external system) picks something, it holds.
        runtime.preference_store.set_alpha_execution_provider(
            "codex",
            source="desktop",
            device_id="desk_migration_1",
        )
        pinned = runtime.preference_store.get_alpha_execution_provider()
        assert pinned["preferred_harness"] == "codex"


def test_cosmic_orchestrator_model_preference_defaults_and_updates() -> None:
    with _runtime_root() as root:
        runtime = build_runtime(root)
        runtime.preference_store.initialize()

        initial = runtime.preference_store.get_cosmic_orchestrator_model()
        assert initial["provider"] == "fireworks_glm"
        assert initial["model"] == "accounts/fireworks/models/glm-5p3-flash"
        assert initial["revision"] == 1

        updated = runtime.preference_store.set_cosmic_orchestrator_model(
            "smarter",
            source="test",
            device_id="desk_kimi_1",
        )

        assert updated["provider"] == "fireworks_kimi"
        assert updated["model"] == "accounts/fireworks/models/kimi-k2p6"
        assert updated["revision"] == 2
        assert updated["updated_source"] == "test"
        assert updated["updated_device_id"] == "desk_kimi_1"

        updated_glm = runtime.preference_store.set_cosmic_orchestrator_model(
            "glm",
            source="test",
            device_id="desk_glm_1",
        )

        assert updated_glm["provider"] == "fireworks_glm"
        assert updated_glm["model"] == "accounts/fireworks/models/glm-5p3"
        assert updated_glm["revision"] == 3
        assert updated_glm["updated_source"] == "test"
        assert updated_glm["updated_device_id"] == "desk_glm_1"


def test_cosmic_orchestrator_model_legacy_glm_5p2_row_migrates_to_default() -> None:
    """A stored GLM 5.2 selection predates the catalog; it must not be honored."""
    with _runtime_root() as root:
        runtime = build_runtime(root)
        runtime.preference_store.initialize()

        with sqlite3.connect(runtime.preference_store.db_path) as connection:
            connection.execute(
                "UPDATE app_preferences SET value_json = ? WHERE key = ?",
                (
                    json.dumps(
                        {
                            "provider": "fireworks_glm",
                            "model": "accounts/fireworks/models/glm-5p2",
                        }
                    ),
                    "cosmic_orchestrator_model",
                ),
            )
            connection.commit()

        migrated = runtime.preference_store.get_cosmic_orchestrator_model()
        assert migrated["provider"] == "fireworks_glm"
        assert migrated["model"] == "accounts/fireworks/models/glm-5p3-flash"


@pytest.mark.asyncio
async def test_process_incoming_message_pins_gateway_preferences_metadata() -> None:
    with _runtime_root() as root:
        runtime = build_runtime(root, route="opus")
        await runtime.start()
        try:
            snapshot = await runtime.save_visual_response_enhancement_preference(
                enabled=False,
                source="test",
                device_id="desk_pref_2",
            )
            assert snapshot["visual_response_enhancement"]["enabled"] is False

            result = await runtime.process_incoming_user_message(
                {
                    "content": "show me the result",
                    "channel": "desktop:desk_pref_2",
                    "metadata": {"platform": "desktop"},
                }
            )

            assert result["visual_response_enhancement_enabled"] is False
            assert (
                result["gateway_preferences"]["visual_response_enhancement"]["enabled"]
                is False
            )
            assert (
                result["gateway_preferences"]["cosmic_orchestrator_model"]["provider"]
                == "fireworks_glm"
            )

            history = runtime.session_store.get_history(result["session_id"])
            assert len(history) == 1
            metadata = history[0]["metadata"]
            assert metadata["visual_response_enhancement_enabled"] is False
            assert (
                metadata["gateway_preferences"]["visual_response_enhancement"]["enabled"]
                is False
            )

            task = runtime._build_orchestrator_task(
                request_record=result,
                session_id=result["session_id"],
                request_id=result["request_id"],
                channel=result["channel"],
            )
            assert task.input["visual_response_enhancement_enabled"] is False
            assert task.input["gateway_preferences"]["cosmic_orchestrator_model"]["provider"] == "fireworks_glm"
            assert task.input["cosmic_orchestrator_model"]["provider"] == "fireworks_glm"
        finally:
            await runtime.stop()
