from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from shutil import rmtree
from uuid import uuid4

import pytest

import gateway.runtime as gateway_runtime_module
from gateway.config import GatewayConfig
from gateway.runtime import GatewayRuntime

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
            sessions_db_path=root / "sessions.db",
            routing_audit_db_path=root / "routing_audit.db",
            artifacts_db_path=root / "artifacts.db",
            delivery_queue_db_path=root / "delivery_queue.db",
            scheduler_db_path=root / "scheduler.db",
            memory_write_audit_db_path=root / "memory_write_audit.db",
        )
    )


class FakeCosmicMailClient:
    mint_calls: list[tuple[str, str, str]] = []
    create_webhook_calls: list[tuple[str, dict[str, object]]] = []

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
        )
    ]
    assert result["status"] == "created"
    assert result["mailbox_id"] == "mbx_123"
    assert result["webhook_id"] == "wh_123"
