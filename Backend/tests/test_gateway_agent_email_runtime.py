from __future__ import annotations

import asyncio

from contextlib import contextmanager
from pathlib import Path
from shutil import rmtree
from uuid import uuid4

import pytest

import gateway.channels.agent_email as agent_email_module
import gateway.runtime as gateway_runtime_module
from gateway.channels.agent_email import AgentEmailAdapter
from gateway.config import GatewayConfig
from gateway.runtime import ActiveRequest, GatewayRuntime

_LOCAL_TMP_ROOT = Path(r"C:\Users\Praveen Raj U S\.codex\memories\gateway-agent-email-tests")
_LOCAL_TMP_ROOT.mkdir(parents=True, exist_ok=True)


@contextmanager
def _runtime_root():
    root = _LOCAL_TMP_ROOT / f"gw-agent-email-{uuid4().hex}"
    root.mkdir(parents=True, exist_ok=False)
    try:
        yield root
    finally:
        rmtree(root, ignore_errors=True)


def _build_runtime(root: Path) -> GatewayRuntime:
    return GatewayRuntime(
        GatewayConfig(
            local_api_token="test-token",
            internal_token="internal-token",
            signing_secret="signing-secret",
            model_router_url="http://127.0.0.1:9999",
            orchestrator_url="http://127.0.0.1:8743",
            enable_whatsapp=False,
            preferences_db_path=root / "preferences.db",
            sessions_db_path=root / "sessions.db",
            routing_audit_db_path=root / "routing_audit.db",
            artifacts_db_path=root / "artifacts.db",
            delivery_queue_db_path=root / "delivery_queue.db",
            scheduler_db_path=root / "scheduler.db",
            memory_write_audit_db_path=root / "memory_write_audit.db",
            agent_email_integrations_db_path=root / "agent_email_integrations.db",
        )
    )


class FakeCosmicMailClient:
    mint_calls: list[tuple[str, str, str]] = []
    create_webhook_calls: list[tuple[str, dict[str, object]]] = []
    replace_trusted_recipients_calls: list[tuple[str, str, list[str]]] = []

    def __init__(
        self,
        *,
        base_url: str,
        api_token: str,
        timeout_sec: float = 20.0,
        client=None,
    ) -> None:
        del timeout_sec, client
        self.base_url = base_url
        self.api_token = api_token

    async def aclose(self) -> None:
        return

    async def get_auth_context(self) -> dict[str, object]:
        if self.api_token == "cm_admin_token":
            return {"is_admin": True, "organization_id": None}
        if self.api_token == "cm_org_token":
            return {"is_admin": False, "organization_id": "org_123"}
        raise AssertionError(f"unexpected token {self.api_token}")

    async def list_mailboxes(self, *, page: int = 1, per_page: int = 200) -> list[dict[str, object]]:
        del page, per_page
        return [
            {
                "id": "mbx_123",
                "address": "cosmic@example.com",
                "status": "active",
                "organization_id": "org_123",
            }
        ]

    async def resolve_mailbox(
        self,
        *,
        mailbox_id: str | None = None,
        mailbox_address: str | None = None,
    ) -> dict[str, object]:
        del mailbox_id
        assert mailbox_address == "cosmic@example.com"
        return {
            "id": "mbx_123",
            "address": "cosmic@example.com",
            "status": "active",
            "organization_id": "org_123",
        }

    async def create_organization_api_key(
        self,
        organization_id: str,
        *,
        name: str,
    ) -> dict[str, object]:
        self.__class__.mint_calls.append((self.api_token, organization_id, name))
        return {
            "api_key": {"id": "key_123", "organization_id": organization_id},
            "plaintext_key": "cm_org_token",
        }

    async def list_webhooks(self) -> list[dict[str, object]]:
        return []

    async def create_webhook(self, payload: dict[str, object]) -> dict[str, object]:
        self.__class__.create_webhook_calls.append((self.api_token, dict(payload)))
        return {
            "id": "wh_123",
            "mailbox_id": payload.get("mailbox_id"),
            "event_type": payload.get("event_type"),
            "url": payload.get("url"),
            "is_active": True,
        }

    async def update_webhook(self, webhook_id: str, payload: dict[str, object]) -> dict[str, object]:
        raise AssertionError(f"unexpected update_webhook {webhook_id} {payload}")

    async def delete_webhook(self, webhook_id: str) -> None:
        raise AssertionError(f"unexpected delete_webhook {webhook_id}")

    async def replace_trusted_recipients(
        self,
        organization_id: str,
        emails: list[str],
        *,
        note: str | None = None,
    ) -> list[dict[str, object]]:
        del note
        self.__class__.replace_trusted_recipients_calls.append(
            (self.api_token, organization_id, list(emails))
        )
        return [{"id": f"tr_{i}", "organization_id": organization_id, "email": e} for i, e in enumerate(emails)]


@pytest.mark.asyncio
async def test_sync_agent_email_webhook_mints_org_key_from_admin_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeCosmicMailClient.mint_calls = []
    FakeCosmicMailClient.create_webhook_calls = []
    monkeypatch.setattr(gateway_runtime_module, "CosmicMailClient", FakeCosmicMailClient)

    with _runtime_root() as root:
        runtime = _build_runtime(root)
        runtime.config.public_base_url = "https://gateway.example.com"
        runtime.agent_email_integration_store.initialize()
        runtime.agent_email_integration_store.save_primary(
            base_url="https://console.thelearnchain.com",
            api_token="cm_admin_token",
            primary_mailbox_address="cosmic@example.com",
            updated_at="2026-03-27T23:30:00Z",
        )

        result = await runtime.sync_agent_email_webhook()
        stored = runtime.agent_email_integration_store.get_primary()

    assert stored is not None
    assert stored.api_token == "cm_org_token"
    assert stored.primary_mailbox_address == "cosmic@example.com"
    assert FakeCosmicMailClient.mint_calls == [
        ("cm_admin_token", "org_123", "COSMIC Gateway Agent Email")
    ]
    assert FakeCosmicMailClient.create_webhook_calls == [
        (
            "cm_org_token",
            {
                "mailbox_id": "mbx_123",
                "event_type": "message.received",
                "url": "https://gateway.example.com/internal/channels/agent-email/incoming",
                "secret": None,
            },
        ),
        (
            "cm_org_token",
            {
                "mailbox_id": "mbx_123",
                "event_type": "approval.created",
                "url": "https://gateway.example.com/internal/channels/agent-email/incoming",
                "secret": None,
            },
        ),
    ]
    assert result["status"] == "created"
    assert result["mailbox_id"] == "mbx_123"
    assert result["webhook_id"] == "wh_123"
    assert result["event_types"] == ["message.received", "approval.created"]


@pytest.mark.asyncio
async def test_get_agent_email_desktop_config_returns_unavailable_when_unset() -> None:
    with _runtime_root() as root:
        runtime = _build_runtime(root)
        runtime.agent_email_integration_store.initialize()
        result = await runtime.get_agent_email_desktop_config()
    assert result == {"available": False}


@pytest.mark.asyncio
async def test_get_agent_email_desktop_config_returns_stored_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway_runtime_module, "CosmicMailClient", FakeCosmicMailClient)
    monkeypatch.setattr(agent_email_module, "CosmicMailClient", FakeCosmicMailClient)

    with _runtime_root() as root:
        runtime = _build_runtime(root)
        runtime.agent_email_integration_store.initialize()
        runtime.agent_email_integration_store.save_primary(
            base_url="https://console.thelearnchain.com",
            api_token="cm_org_token",
            primary_mailbox_address="cosmic@example.com",
            updated_at="2026-05-10T00:00:00Z",
        )
        await runtime.reconcile_agent_email_adapter()

        result = await runtime.get_agent_email_desktop_config()

    assert result == {
        "available": True,
        "base_url": "https://console.thelearnchain.com",
        "api_token": "cm_org_token",
        "primary_mailbox_address": "cosmic@example.com",
        "organization_id": "org_123",
    }


@pytest.mark.asyncio
async def test_save_agent_email_trusted_senders_pushes_to_cosmic_mail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeCosmicMailClient.replace_trusted_recipients_calls = []
    monkeypatch.setattr(gateway_runtime_module, "CosmicMailClient", FakeCosmicMailClient)
    monkeypatch.setattr(agent_email_module, "CosmicMailClient", FakeCosmicMailClient)

    with _runtime_root() as root:
        runtime = _build_runtime(root)
        runtime.agent_email_integration_store.initialize()
        runtime.agent_email_integration_store.save_primary(
            base_url="https://console.thelearnchain.com",
            api_token="cm_org_token",
            primary_mailbox_address="cosmic@example.com",
            updated_at="2026-05-09T00:00:00Z",
        )
        await runtime.reconcile_agent_email_adapter()

        await runtime.save_agent_email_trusted_senders(
            ["Owner@Example.com", "second@example.com"]
        )

    # reconcile pushes the existing (empty) list, then save_trusted_senders pushes the
    # normalized form (lowercased + de-duped) — matches what's persisted locally.
    assert FakeCosmicMailClient.replace_trusted_recipients_calls == [
        ("cm_org_token", "org_123", []),
        (
            "cm_org_token",
            "org_123",
            ["owner@example.com", "second@example.com"],
        ),
    ]


@pytest.mark.asyncio
async def test_save_agent_email_trusted_senders_swallows_remote_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeCosmicMailClient.replace_trusted_recipients_calls = []

    class _BoomClient(FakeCosmicMailClient):
        async def replace_trusted_recipients(self, organization_id, emails, *, note=None):
            del organization_id, emails, note
            raise RuntimeError("cosmic-mail unreachable")

    monkeypatch.setattr(gateway_runtime_module, "CosmicMailClient", _BoomClient)
    monkeypatch.setattr(agent_email_module, "CosmicMailClient", _BoomClient)

    with _runtime_root() as root:
        runtime = _build_runtime(root)
        runtime.agent_email_integration_store.initialize()
        runtime.agent_email_integration_store.save_primary(
            base_url="https://console.thelearnchain.com",
            api_token="cm_org_token",
            primary_mailbox_address="cosmic@example.com",
            updated_at="2026-05-09T00:00:00Z",
        )
        await runtime.reconcile_agent_email_adapter()

        # Must NOT raise — local persistence is the source of truth and the
        # downstream push is best-effort.
        status = await runtime.save_agent_email_trusted_senders(["x@example.com"])
        stored = runtime.agent_email_integration_store.get_primary()

    assert stored is not None
    assert list(stored.trusted_senders) == ["x@example.com"]
    assert status["trusted_senders"] == ["x@example.com"]


@pytest.mark.asyncio
async def test_start_request_fulfillment_allows_concurrent_agent_email_requests_on_same_mailbox() -> None:
    with _runtime_root() as root:
        runtime = _build_runtime(root)

        async def fake_run_request_fulfillment(state, request_record) -> None:
            return

        runtime._run_request_fulfillment = fake_run_request_fulfillment  # type: ignore[method-assign]
        runtime.active_requests["req_existing"] = ActiveRequest(
            request_id="req_existing",
            session_id="email-thread:cosmic@example.com:thr_existing",
            channel="agent-email:cosmic@example.com",
            route="opus",
        )

        runtime.start_request_fulfillment(
            {
                "request_id": "req_email_2",
                "session_id": "email-thread:cosmic@example.com:thr_new",
                "channel": "agent-email:cosmic@example.com",
                "route": "opus",
                "query": "Check the latest reply.",
            }
        )

        created = runtime.active_requests["req_email_2"]
        assert created.channel == "agent-email:cosmic@example.com"
        assert created.session_id == "email-thread:cosmic@example.com:thr_new"
        if created.worker is not None:
            await created.worker

        runtime.active_requests.clear()

        runtime.active_requests["req_desktop_existing"] = ActiveRequest(
            request_id="req_desktop_existing",
            session_id="sess_20260327",
            channel="desktop:test-device",
            route="opus",
        )
        with pytest.raises(ValueError, match="foreground task is already active"):
            runtime.start_request_fulfillment(
                {
                    "request_id": "req_desktop_2",
                    "session_id": "sess_20260327",
                    "channel": "desktop:test-device",
                    "route": "opus",
                    "query": "second desktop request",
                }
            )


@pytest.mark.asyncio
async def test_reserve_agent_email_inbound_ignores_duplicate_persisted_message() -> None:
    session_id = AgentEmailAdapter.build_thread_session_id(
        mailbox_address="cosmic@example.com",
        thread_id="thr_123",
    )
    with _runtime_root() as root:
        runtime = _build_runtime(root)
        await runtime.start()
        try:
            runtime.session_store.append_message(
                session_id,
                role="user",
                content="Email subject: Hello",
                channel="agent-email:cosmic@example.com",
                metadata={
                    "platform": "agent-email",
                    "request_id": "req_existing",
                    "message_id": "msg_123",
                    "internet_message_id": "<internet_msg_123@example.com>",
                },
            )

            reserved_keys, duplicate = runtime.reserve_agent_email_inbound(
                {
                    "session_id": session_id,
                    "channel": "agent-email:cosmic@example.com",
                    "metadata": {
                        "platform": "agent-email",
                        "message_id": "msg_123",
                        "internet_message_id": "<internet_msg_123@example.com>",
                    },
                }
            )
        finally:
            await runtime.stop()

    assert reserved_keys == []
    assert duplicate is not None
    assert duplicate["status"] == "duplicate"
    assert duplicate["request_id"] == "req_existing"


def test_should_preprocess_agent_email_when_connected_even_if_flag_disabled() -> None:
    with _runtime_root() as root:
        runtime = _build_runtime(root)
        runtime.agent_email_integration_store.initialize()
        runtime.agent_email_integration_store.save_primary(
            base_url="https://console.thelearnchain.com",
            api_token="cm_org_token",
            primary_mailbox_address="cosmic@example.com",
            updated_at="2026-03-28T01:55:00Z",
        )
        runtime._redis = object()

        should_preprocess = runtime._should_preprocess_email_inbound(
            {
                "route": "opus",
                "channel": "agent-email:cosmic@example.com",
                "message": {
                    "metadata": {
                        "thread_id": "thr_123",
                        "message_id": "msg_123",
                    }
                },
            }
        )

    assert should_preprocess is True


def test_build_email_inbound_orchestrator_query_includes_matched_instruction_details() -> None:
    with _runtime_root() as root:
        runtime = _build_runtime(root)

        query = runtime._build_email_inbound_orchestrator_query(
            original_content="Email subject: Q3 question",
            process_output={
                "summary": "External sender asked about the Q3 deck.",
                "subject": "Q3 question",
                "from_address": "arun@example.com",
                "trusted_sender": False,
                "sender_role": "external",
                "matched_instructions": [
                    {
                        "instruction_id": "eminst_q3",
                        "label": "Watch for Q3 email",
                        "raw_user_instruction": "Watch for anything mentioning Q3 in email.",
                        "behavior": {
                            "mode": "notify_only",
                            "completion_mode": "perpetual",
                        },
                    }
                ],
                "instruction_match_reason": "The inbound subject explicitly mentions Q3.",
                "attachments": [],
            },
        )

    assert "Matched standing instruction(s):" in query
    assert "User instruction: Watch for anything mentioning Q3 in email." in query
    assert "Match reason: The inbound subject explicitly mentions Q3." in query


@pytest.mark.asyncio
async def test_maybe_schedule_delivered_email_instruction_update_dispatches_callback_for_sent_email() -> None:
    calls: list[tuple[list[str], str | None, str | None]] = []
    with _runtime_root() as root:
        runtime = _build_runtime(root)
        runtime._redis = object()

        async def fake_record_email_instruction_delivery(*, event, instruction_ids):
            calls.append(
                (
                    list(instruction_ids),
                    event.get("thread_id"),
                    event.get("message_id"),
                )
            )

        runtime._record_email_instruction_delivery = fake_record_email_instruction_delivery  # type: ignore[method-assign]

        await runtime._maybe_schedule_delivered_email_instruction_update(
            {
                "type": "response.complete",
                "channel": "agent-email:cosmic@example.com",
                "thread_id": "thr_123",
                "message_id": "msg_123",
                "matched_instruction_ids": ["eminst_1", "eminst_2"],
                "email_auto_reply_sent": False,
            },
            delivery_status="sent",
        )
        if runtime._background_tasks:
            await asyncio.gather(*list(runtime._background_tasks), return_exceptions=True)

    assert calls == [(["eminst_1", "eminst_2"], "thr_123", "msg_123")]
