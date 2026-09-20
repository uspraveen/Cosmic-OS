"""Tests for vault policy semantics: per-agent defaults, window expiry, and
always-allow — the grant vocabulary the approval cards and settings page share."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from gateway.vault.store import (
    POLICY_ALWAYS_ALLOW,
    POLICY_ALWAYS_ASK,
    POLICY_WINDOW,
    VaultStore,
)


def _store(tmp_path: Path) -> VaultStore:
    store = VaultStore(tmp_path / "vault.db")
    store.initialize()
    return store


def _add_entry(store: VaultStore, entry_id: str) -> str:
    store.add_entry(
        {
            "entry_id": entry_id,
            "title": f"Entry {entry_id}",
            "site_url": "https://example.com",
            "username": "u",
            "password_encrypted": "x",
            "tags": [],
        }
    )
    return entry_id


def test_policy_defaults_to_always_ask_per_agent(tmp_path):
    store = _store(tmp_path)
    entry_id = _add_entry(store, "vault_test1")
    # No policy rows at all: every agent falls back to ask.
    assert store.policy_allows_use(entry_id, "cosmic/orchestrator:1.0.0") is False
    assert store.policy_allows_use(entry_id, "cosmic/alpha-agent:1.0.0") is False
    policy = store.get_policy(entry_id, "cosmic/alpha-agent:1.0.0")
    assert policy["mode"] == POLICY_ALWAYS_ASK


def test_window_policy_expires(tmp_path):
    store = _store(tmp_path)
    entry_id = _add_entry(store, "vault_test2")
    future = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat().replace(
        "+00:00", "Z"
    )
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat().replace(
        "+00:00", "Z"
    )
    store.set_policy(entry_id, POLICY_WINDOW, future)
    assert store.policy_allows_use(entry_id) is True

    # The same row, after the window lapses: the grant is gone even though the
    # stored mode still says 'window' — which is why the UI must derive the
    # effective state instead of rendering the stored mode.
    store.set_policy(entry_id, POLICY_WINDOW, past)
    assert store.policy_allows_use(entry_id) is False
    assert store.get_policy(entry_id)["mode"] == POLICY_WINDOW


def test_window_policy_is_per_agent(tmp_path):
    store = _store(tmp_path)
    entry_id = _add_entry(store, "vault_test3")
    future = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat().replace(
        "+00:00", "Z"
    )
    store.set_policy(entry_id, POLICY_WINDOW, future, agent_id="cosmic/orchestrator:1.0.0")
    assert store.policy_allows_use(entry_id, "cosmic/orchestrator:1.0.0") is True
    # A different agent has no grant: the delegated agent's check must not
    # inherit the orchestrator's approval.
    assert store.policy_allows_use(entry_id, "cosmic/alpha-agent:1.0.0") is False


def test_always_allow_policy(tmp_path):
    store = _store(tmp_path)
    entry_id = _add_entry(store, "vault_test4")
    store.set_policy(entry_id, POLICY_ALWAYS_ALLOW)
    assert store.policy_allows_use(entry_id) is True
