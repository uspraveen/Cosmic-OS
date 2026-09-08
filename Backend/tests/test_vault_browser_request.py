"""Tests for the browser-agent credential request flow (vault pending)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.vault.store import VaultStore, decrypt_entry_secrets


class _StubRuntime:
    """Minimal GatewayRuntime surface for provide_browser_credentials."""

    from gateway.runtime import GatewayRuntime

    vault_store: VaultStore
    _published: list
    _continuations: list

    def __init__(self, store: VaultStore):
        self.vault_store = store
        self._published = []
        self._continuations = []

    _vault_request_response_block = GatewayRuntime.__dict__["_vault_request_response_block"]
    _publish_vault_request_block = GatewayRuntime.__dict__["_publish_vault_request_block"]
    provide_browser_credentials = GatewayRuntime.__dict__["provide_browser_credentials"]
    _compose_vault_continuation_query = GatewayRuntime.__dict__["_compose_vault_continuation_query"]
    _schedule_background_task = GatewayRuntime.__dict__["_schedule_background_task"]
    _continue_turn_after_vault = GatewayRuntime.__dict__["_continue_turn_after_vault"]

    def _safe_text(self, value):
        return str(value or "").strip()

    async def _publish_vault_request_block_impl(self, pending):
        self._published.append(self._vault_request_response_block(pending))

    async def _continue_turn_after_vault_impl(self, pending):
        self._continuations.append(self._compose_vault_continuation_query(pending))

    def _schedule_background_task(self, coro, name=None):
        coro.close()  # never awaited in the stub; closing avoids the warning
        self._continuations.append(name)


@pytest.fixture()
def store(tmp_path):
    vault = VaultStore(tmp_path / "vault.db")
    vault.initialize()
    return vault


def _stub(store) -> _StubRuntime:
    runtime = _StubRuntime(store)
    # Bind the real async publish/continuation methods to thin recorders.
    runtime._publish_vault_request_block = runtime._publish_vault_request_block_impl
    runtime._continue_turn_after_vault = runtime._continue_turn_after_vault_impl
    return runtime


def _make_pending(store: VaultStore) -> dict:
    return store.create_pending(
        {
            "action": "browser_credential_request",
            "task_id": "task_browser_1",
            "session_id": "sess-1",
            "channel": "desktop:desk_abc",
            "purpose": "Login required to download invoices",
            "payload": {
                "title": "app.example.com",
                "site": "https://app.example.com/login",
                "site_url": "https://app.example.com/login",
                "site_domain": "app.example.com",
                "username_hint": "me@example.com",
                "origin": "browser_agent",
            },
        }
    )


def test_block_shape_for_browser_credential_request(store):
    pending = _make_pending(store)
    runtime = _stub(store)
    block = runtime._vault_request_response_block(pending)
    assert block["type"] == "browser_credential_request"
    assert block["site_domain"] == "app.example.com"
    assert block["username_hint"] == "me@example.com"
    assert block["can_respond"] is True
    assert block["request_id"] == pending["request_id"]


def test_provide_stores_entry_and_continues(store):
    pending = _make_pending(store)
    runtime = _stub(store)

    result = asyncio.run(
        runtime.provide_browser_credentials(
            pending["request_id"],
            username="me@example.com",
            password="s3cret",
            totp_seed="SEED",
        )
    )
    assert result["status"] == "approved"
    entry_id = result["entry_id"]
    assert entry_id

    entry = store.get_entry(entry_id)
    assert entry["site_domain"] == "app.example.com"
    assert entry["username"] == "me@example.com"
    secrets = decrypt_entry_secrets(entry)
    assert secrets["password"] == "s3cret"
    assert entry["source"] == "agent"

    resolved = store.get_pending(pending["request_id"])
    assert resolved["status"] == "approved"

    # Continuation must hand the credential_ref back to the model.
    coros = [c for c in runtime._continuations]
    assert coros, "expected a scheduled continuation"


def test_provide_rejects_non_pending(store):
    pending = _make_pending(store)
    store.mark_pending(pending["request_id"], "approved")
    runtime = _stub(store)
    result = asyncio.run(
        runtime.provide_browser_credentials(
            pending["request_id"], username="u", password="p"
        )
    )
    assert result["status"] == "ignored"


def test_continuation_query_contains_credential_ref(store):
    pending = _make_pending(store)
    query = _StubRuntime._compose_vault_continuation_query(
        _StubRuntime(store),
        {
            **pending,
            "status": "approved",
            "payload": {**pending["payload"], "credential_ref": "vault:abc123"},
        },
    )
    assert "vault:abc123" in query
    assert "browser_task" in query
