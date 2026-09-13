"""Tests for the password vault store and route-level policy logic."""

from __future__ import annotations

import base64
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

from gateway.credentials.encryption import Fernet  # noqa: E402
from gateway.vault.store import (  # noqa: E402
    POLICY_ALWAYS_ALLOW,
    POLICY_ALWAYS_ASK,
    POLICY_WINDOW,
    VaultStore,
    decrypt_entry_secrets,
    derive_site_domain,
    entry_is_expired,
    normalize_credential_kind,
    normalize_expires_at,
)


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
        "title": "GitHub",
        "site_url": "https://github.com/login",
        "username": "me@example.com",
        "password": "hunter2",
        "totp_seed": "",
        "notes": "",
    }
    item.update(overrides)
    return store.add_entry(item)


def test_derive_site_domain_strips_scheme_www_and_paths():
    assert derive_site_domain("https://www.github.com/login") == "github.com"
    assert derive_site_domain("http://api.github.com/x") == "api.github.com"
    assert derive_site_domain("github.com") == "github.com"
    assert derive_site_domain("") == ""


def test_add_entry_encrypts_secrets_at_rest(store):
    entry = _entry(store)
    raw = store.get_entry(entry["entry_id"])
    assert "hunter2" not in raw["password_encrypted"]
    assert raw["has_password"] and not raw["has_totp"]
    assert decrypt_entry_secrets(raw)["password"] == "hunter2"


def test_find_matches_exact_domain_then_subdomain_then_title(store):
    entry = _entry(store)
    assert [e["entry_id"] for e in store.find_entries("github.com")] == [entry["entry_id"]]
    assert [e["entry_id"] for e in store.find_entries("https://api.github.com/pr")] == [entry["entry_id"]]
    assert [e["entry_id"] for e in store.find_entries("git")] == [entry["entry_id"]]
    assert [e["entry_id"] for e in store.find_entries("entry_id:" + entry["entry_id"])] == []
    assert [e["entry_id"] for e in store.find_entries(entry["entry_id"])] == [entry["entry_id"]]


def test_find_returns_multiple_for_ambiguous_title(store):
    _entry(store, title="GitHub work")
    _entry(store, title="GitHub personal", site_url="https://github.org")
    matches = store.find_entries("github")
    assert len(matches) == 2


def test_policy_defaults_to_always_ask(store):
    entry = _entry(store)
    policy = store.get_policy(entry["entry_id"])
    assert policy["mode"] == POLICY_ALWAYS_ASK
    assert store.policy_allows_use(entry["entry_id"]) is False


def test_policy_always_allow_and_window_expiry(store):
    entry = _entry(store)
    store.set_policy(entry["entry_id"], POLICY_ALWAYS_ALLOW)
    assert store.policy_allows_use(entry["entry_id"]) is True

    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    store.set_policy(entry["entry_id"], POLICY_WINDOW, future)
    assert store.policy_allows_use(entry["entry_id"]) is True

    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    store.set_policy(entry["entry_id"], POLICY_WINDOW, past)
    assert store.policy_allows_use(entry["entry_id"]) is False

    store.set_policy(entry["entry_id"], POLICY_WINDOW, None)
    assert store.policy_allows_use(entry["entry_id"]) is False

    with pytest.raises(ValueError):
        store.set_policy(entry["entry_id"], "whenever")


def test_pending_use_request_is_consumed_once(store):
    entry = _entry(store)
    pending = store.create_pending(
        {"action": "use_entry", "entry_id": entry["entry_id"], "task_id": "t1"}
    )
    assert pending["status"] == "pending"
    assert store.take_approved_use(entry["entry_id"], "t1") is None

    store.mark_pending(pending["request_id"], "approved")
    taken = store.take_approved_use(entry["entry_id"], "t1")
    assert taken is not None and taken["request_id"] == pending["request_id"]
    assert store.take_approved_use(entry["entry_id"], "t1") is None
    assert store.get_pending(pending["request_id"])["status"] == "consumed"


def test_take_approved_use_is_scoped_to_task(store):
    entry = _entry(store)
    pending = store.create_pending(
        {"action": "use_entry", "entry_id": entry["entry_id"], "task_id": "t1"}
    )
    store.mark_pending(pending["request_id"], "approved")
    assert store.take_approved_use(entry["entry_id"], "other-task") is None


def test_agent_add_entry_flow_uses_preencrypted_payload(store):
    pending = store.create_pending(
        {
            "action": "add_entry",
            "payload": {
                "title": "Forum",
                "site_url": "https://forum.example.com",
                "username": "fresh",
                "password_encrypted": store.add_entry.__globals__["encrypt_token_str"]("agent-made"),
                "tags": ["agent"],
            },
        }
    )
    store.mark_pending(pending["request_id"], "approved")
    entry = store.add_entry(
        {
            "title": "Forum",
            "site_url": "https://forum.example.com",
            "username": "fresh",
            "password_encrypted": pending["payload"]["password_encrypted"],
            "source": "agent",
        }
    )
    assert entry["source"] == "agent"
    assert decrypt_entry_secrets(store.get_entry(entry["entry_id"]))["password"] == "agent-made"


def test_delete_entry_removes_policy(store):
    entry = _entry(store)
    store.set_policy(entry["entry_id"], POLICY_ALWAYS_ALLOW)
    assert store.delete_entry(entry["entry_id"]) is True
    assert store.get_entry(entry["entry_id"]) is None
    # The policy row is gone too — a recreated entry starts back at always_ask.
    assert store.get_policy(entry["entry_id"])["mode"] == POLICY_ALWAYS_ASK


def test_audit_is_append_only_and_ordered(store):
    entry = _entry(store)
    store.append_audit(entry["entry_id"], "orchestrator", "use", "task-a")
    store.append_audit(entry["entry_id"], "user", "view_password")
    rows = store.list_audit()
    actions = [row["action"] for row in rows]
    assert actions[0] == "view_password"  # newest first
    assert "use" in actions


def test_normalize_credential_kind_aliases_and_unknown():
    assert normalize_credential_kind("API-key") == "api_key"
    assert normalize_credential_kind("bearer") == "token"
    assert normalize_credential_kind("password") == "login"
    assert normalize_credential_kind("nope") == "login"
    assert normalize_expires_at("2027-03-12T15:00:00Z") == "2027-03-12"
    assert normalize_expires_at("") is None
    assert normalize_expires_at("not-a-date") is None
    assert entry_is_expired("1999-01-01") is True
    assert entry_is_expired("2999-01-01") is False
    assert entry_is_expired(None) is False


def test_add_entry_stores_kind_and_expiry(store):
    entry = _entry(store, credential_kind="api_key", expires_at="2027-06-01")
    assert entry["credential_kind"] == "api_key"
    assert entry["expires_at"] == "2027-06-01"
    assert entry["expired"] is False


def test_add_entry_defaults_kind_to_login(store):
    entry = _entry(store)
    assert entry["credential_kind"] == "login"
    assert entry["expires_at"] is None
    assert entry["expired"] is False


def test_update_entry_can_set_and_clear_expiry(store):
    entry = _entry(store, credential_kind="token")
    updated = store.update_entry(entry["entry_id"], {"expires_at": "2020-01-01", "credential_kind": "api_key"})
    assert updated["credential_kind"] == "api_key"
    assert updated["expires_at"] == "2020-01-01"
    assert updated["expired"] is True
    cleared = store.update_entry(entry["entry_id"], {"expires_at": ""})
    assert cleared["expires_at"] is None
    assert cleared["expired"] is False


def test_initialize_migrates_kind_and_expiry_columns(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", key)
    from gateway.credentials import encryption

    encryption._CIPHER = None
    db_path = tmp_path / "legacy-vault.db"
    import sqlite3

    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE vault_entries (
            entry_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            site_url TEXT NOT NULL DEFAULT '',
            site_domain TEXT NOT NULL DEFAULT '',
            username TEXT NOT NULL DEFAULT '',
            password_encrypted TEXT NOT NULL DEFAULT '',
            totp_seed_encrypted TEXT NOT NULL DEFAULT '',
            notes_encrypted TEXT NOT NULL DEFAULT '',
            tags_json TEXT NOT NULL DEFAULT '[]',
            source TEXT NOT NULL DEFAULT 'user',
            created_by_task_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    connection.commit()
    connection.close()

    vault = VaultStore(db_path)
    vault.initialize()
    entry = vault.add_entry({"title": "Legacy", "password": "x", "credential_kind": "api_key"})
    assert entry["credential_kind"] == "api_key"
    encryption._CIPHER = None
