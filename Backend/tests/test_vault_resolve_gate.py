"""Route-level tests for the /internal/vault resolve policy gate.

A well-formed credential_ref is not authorization: resolve only releases the
secret when the entry policy allows use or a fresh user-approved grant is
consumed. This is what stops a model from bypassing always-ask entries with a
self-constructed ref.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

from gateway.credentials.encryption import Fernet  # noqa: E402
from gateway.vault.routes import ResolveRequest, internal_resolve  # noqa: E402
from gateway.vault.store import POLICY_ALWAYS_ALLOW, VaultStore  # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", key)
    from gateway.credentials import encryption

    encryption._CIPHER = None  # reset cached ephemeral cipher
    vault = VaultStore(tmp_path / "vault.db")
    vault.initialize()
    yield vault
    encryption._CIPHER = None


def _entry(store: VaultStore, **overrides):
    item = {
        "title": "Gmail",
        "site_url": "https://gmail.com",
        "username": "me@example.com",
        "password": "hunter2",
        "totp_seed": "",
        "notes": "",
    }
    item.update(overrides)
    return store.add_entry(item)


def _request(store: VaultStore):
    runtime = SimpleNamespace(
        vault_store=store,
        config=SimpleNamespace(internal_token=""),
    )
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(gateway_runtime=runtime)),
        headers={},
    )


def _resolve(store: VaultStore, credential_ref: str, task_id: str = "task-1"):
    return asyncio.run(
        internal_resolve(ResolveRequest(credential_ref=credential_ref, task_id=task_id), _request(store))
    )


def test_resolve_rejects_malformed_ref_with_actionable_detail(store):
    with pytest.raises(HTTPException) as excinfo:
        _resolve(store, "vault_d9e9c26828c64042")  # underscore, not a real entry id
    assert excinfo.value.status_code == 400
    assert "vault_lookup" in str(excinfo.value.detail)


def test_resolve_rejects_empty_ref(store):
    with pytest.raises(HTTPException) as excinfo:
        _resolve(store, "")
    assert excinfo.value.status_code == 400


def test_resolve_404_for_unknown_entry(store):
    with pytest.raises(HTTPException) as excinfo:
        _resolve(store, "vault:vault_missing123")
    assert excinfo.value.status_code == 404
    assert "vault_list_sites" in str(excinfo.value.detail)


def test_resolve_denies_wellformed_ref_without_approval(store):
    """The core gate: a guessed-but-wellformed ref must not bypass always-ask."""
    entry = _entry(store)
    with pytest.raises(HTTPException) as excinfo:
        _resolve(store, f"vault:{entry['entry_id']}")
    assert excinfo.value.status_code == 403
    assert "vault_lookup" in str(excinfo.value.detail)
    denied = [row for row in store.list_audit() if row["action"] == "resolve_denied"]
    assert denied, "expected a resolve_denied audit row"


def test_resolve_normalizes_bare_entry_id_and_still_gates(store):
    entry = _entry(store)
    with pytest.raises(HTTPException) as excinfo:
        _resolve(store, entry["entry_id"])  # bare id, no prefix
    assert excinfo.value.status_code == 403


def test_resolve_succeeds_with_approved_grant_and_consumes_it(store):
    entry = _entry(store)
    pending = store.create_pending(
        {"action": "use_entry", "entry_id": entry["entry_id"], "task_id": "task-1"}
    )
    store.mark_pending(pending["request_id"], "approved")
    resolved = _resolve(store, f"vault:{entry['entry_id']}", task_id="task-1")
    assert resolved["password"] == "hunter2"
    assert resolved["username"] == "me@example.com"
    # Allow once = one resolve; the next attempt is denied.
    with pytest.raises(HTTPException) as excinfo:
        _resolve(store, f"vault:{entry['entry_id']}", task_id="task-1")
    assert excinfo.value.status_code == 403


def test_resolve_succeeds_under_always_allow_policy(store):
    entry = _entry(store)
    store.set_policy(entry["entry_id"], POLICY_ALWAYS_ALLOW)
    resolved = _resolve(store, f"vault:{entry['entry_id']}")
    assert resolved["password"] == "hunter2"


def test_resolve_succeeds_with_grant_minted_by_provide_flow(store):
    entry = _entry(store)
    store.record_approved_use(entry["entry_id"], "task-browser", "sess-1")
    resolved = _resolve(store, f"vault:{entry['entry_id']}", task_id="task-browser")
    assert resolved["password"] == "hunter2"
