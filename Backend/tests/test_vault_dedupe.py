"""Vault same-login guard tests.

One (site, username, kind) triple is one login: a second save against the
same triple must refresh the existing entry, never file a duplicate beside
it. The Oct 2026 HackerNews pair (identical title and username, different
passwords, no way to tell which worked) is what the old unconditional
insert produced. These tests pin the store key, the username lookup tier,
and every save path that used to duplicate.

Run with:  python -m pytest tests/test_vault_dedupe.py -q
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from gateway.vault.routes import CreateEntryRequest, create_entry, internal_lookup, LookupRequest
from gateway.vault.store import VaultStore, decrypt_entry_secrets
from test_gateway_desktop_ws import build_runtime


SITE_URL = "https://news.ycombinator.com/login"
USERNAME = "usp@alumni.upenn.edu"


def _store(tmp_path) -> VaultStore:
    store = VaultStore(tmp_path / "vault_dedupe.db")
    store.initialize()
    return store


def _route_request(runtime):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(gateway_runtime=runtime)),
        headers={"X-Local-Token": "test-token"},
    )


# ── store: the duplicate key ─────────────────────────────────────────────────


def test_find_matching_entry_requires_site_and_username(tmp_path):
    store = _store(tmp_path)
    store.add_entry({"title": "HackerNews", "site_url": SITE_URL, "username": USERNAME, "password": "pw"})
    # A site-less or username-less save has no key, so it can never duplicate.
    assert store.find_matching_entry(SITE_URL, "") is None
    assert store.find_matching_entry("", USERNAME) is None


def test_find_matching_entry_keys_on_site_username_kind(tmp_path):
    store = _store(tmp_path)
    store.add_entry({"title": "HackerNews", "site_url": SITE_URL, "username": USERNAME, "password": "pw"})
    # URL with path and trailing junk normalizes to the same domain + username.
    assert store.find_matching_entry(SITE_URL, USERNAME) is not None
    assert store.find_matching_entry("http://news.ycombinator.com/other", USERNAME.upper()) is not None
    # A different username is a different account, never a duplicate.
    assert store.find_matching_entry(SITE_URL, "praveenrajus") is None
    # A different kind on the same site + username is a different credential.
    assert store.find_matching_entry(SITE_URL, USERNAME, "api_key") is None


def test_find_entries_resolves_username_query(tmp_path):
    store = _store(tmp_path)
    entry = store.add_entry({"title": "HackerNews", "site_url": SITE_URL, "username": USERNAME, "password": "pw"})
    store.add_entry({"title": "GH token", "site_url": "https://github.com", "username": "token-label",
                     "password": "k", "credential_kind": "token"})
    matches = store.find_entries(USERNAME)
    assert [e["entry_id"] for e in matches] == [entry["entry_id"]]
    # A word sitting in a token entry's username column must not become a
    # wildcard across the vault.
    assert [e["entry_id"] for e in store.find_entries("token-label")] == []


def test_update_entry_accepts_preencrypted_fields(tmp_path):
    store = _store(tmp_path)
    entry = store.add_entry({"title": "HackerNews", "site_url": SITE_URL, "username": USERNAME, "password": "old"})
    from gateway.credentials.encryption import encrypt_token_str

    updated = store.update_entry(entry["entry_id"], {
        "password_encrypted": encrypt_token_str("fresh-secret"),
        # Encrypted wins over its plaintext twin; the SET clause must not
        # receive the same column twice.
        "password": "ignored-plaintext",
    })
    assert decrypt_entry_secrets(updated)["password"] == "fresh-secret"
    assert decrypt_entry_secrets(store.get_entry(entry["entry_id"]))["password"] == "fresh-secret"


# ── approval path: update, never duplicate ───────────────────────────────────


def _add_entry_pending(runtime, *, username: str = USERNAME, password: str = "brand-new-secret"):
    from gateway.credentials.encryption import encrypt_token_str

    return runtime.vault_store.create_pending({
        "action": "add_entry",
        "task_id": "tsk_dedupe",
        "session_id": "sess_dedupe",
        "payload": {
            "title": "HackerNews",
            "site_url": SITE_URL,
            "username": username,
            "password_encrypted": encrypt_token_str(password),
            "totp_seed_encrypted": "",
            "notes_encrypted": "",
            "tags": [],
            "credential_kind": "login",
            "expires_at": None,
        },
    })


@pytest.mark.asyncio
async def test_approved_add_entry_updates_existing_instead_of_duplicating(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime.vault_store = VaultStore(tmp_path / "vault.db")
    runtime._continue_turn_after_vault = AsyncMock()
    await runtime.start()
    try:
        original = runtime.vault_store.add_entry({
            "title": "HackerNews", "site_url": SITE_URL, "username": USERNAME, "password": "stale",
        })
        pending = _add_entry_pending(runtime)
        await runtime.approve_vault_request(pending["request_id"], grant="once")

        entries = [e for e in runtime.vault_store.list_entries() if e["site_domain"] == "news.ycombinator.com"]
        assert len(entries) == 1, "the same login must never be filed twice"
        assert entries[0]["entry_id"] == original["entry_id"]
        assert decrypt_entry_secrets(entries[0])["password"] == "brand-new-secret"
        # The single-use grant rides the entry the run will actually use.
        assert runtime.vault_store.take_approved_use(original["entry_id"], "tsk_dedupe") is not None
        audits = runtime.vault_store.list_audit(entry_id=original["entry_id"])
        assert any(row["action"] == "update" for row in audits)
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_approved_add_entry_still_creates_a_distinct_account(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime.vault_store = VaultStore(tmp_path / "vault.db")
    runtime._continue_turn_after_vault = AsyncMock()
    await runtime.start()
    try:
        runtime.vault_store.add_entry({
            "title": "HackerNews", "site_url": SITE_URL, "username": USERNAME, "password": "pw",
        })
        pending = _add_entry_pending(runtime, username="praveenrajus")
        await runtime.approve_vault_request(pending["request_id"], grant="once")

        entries = [e for e in runtime.vault_store.list_entries() if e["site_domain"] == "news.ycombinator.com"]
        assert len(entries) == 2, "a different username is a second account, not a duplicate"
        assert {e["username"] for e in entries} == {USERNAME, "praveenrajus"}
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_provided_browser_credentials_update_the_existing_login(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime.vault_store = VaultStore(tmp_path / "vault.db")
    await runtime.start()
    try:
        original = runtime.vault_store.add_entry({
            "title": "HackerNews", "site_url": SITE_URL, "username": USERNAME, "password": "old",
        })
        pending = runtime.vault_store.create_pending({
            "action": "browser_credential_request",
            "task_id": "tsk_dedupe",
            "session_id": "sess_dedupe",
            "payload": {"title": "news.ycombinator.com", "site": SITE_URL, "site_url": SITE_URL, "site_domain": "news.ycombinator.com"},
        })
        await runtime.provide_browser_credentials(
            pending["request_id"], username=USERNAME, password="card-secret"
        )
        entries = [e for e in runtime.vault_store.list_entries() if e["site_domain"] == "news.ycombinator.com"]
        assert len(entries) == 1
        assert entries[0]["entry_id"] == original["entry_id"]
        assert decrypt_entry_secrets(entries[0])["password"] == "card-secret"
        audits = runtime.vault_store.list_audit(entry_id=original["entry_id"])
        assert any(row["action"] == "browser_provide_updated" for row in audits)
    finally:
        await runtime.stop()


# ── manual create route: ask instead of silently duplicating ─────────────────


@pytest.mark.asyncio
async def test_create_route_conflicts_on_same_login(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime.vault_store = VaultStore(tmp_path / "vault.db")
    await runtime.start()
    try:
        original = runtime.vault_store.add_entry({
            "title": "HackerNews", "site_url": SITE_URL, "username": USERNAME, "password": "pw",
        })
        body = CreateEntryRequest(title="HackerNews copy", site_url=SITE_URL, username=USERNAME, password="pw2")
        with pytest.raises(HTTPException) as err:
            await create_entry(body, _route_request(runtime))
        assert err.value.status_code == 409
        assert err.value.detail["existing_entry"]["entry_id"] == original["entry_id"]

        # The deliberate escape hatch keeps both.
        body = CreateEntryRequest(title="HackerNews copy", site_url=SITE_URL, username=USERNAME,
                                  password="pw2", allow_duplicate=True)
        result = await create_entry(body, _route_request(runtime))
        assert result["entry"]["entry_id"] != original["entry_id"]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_lookup_reports_actionable_candidates(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime.vault_store = VaultStore(tmp_path / "vault.db")
    await runtime.start()
    try:
        runtime.vault_store.add_entry({"title": "HackerNews", "site_url": SITE_URL, "username": USERNAME, "password": "pw"})
        other = runtime.vault_store.add_entry({"title": "HackerNews 2", "site_url": SITE_URL, "username": "praveenrajus", "password": "pw"})
        with pytest.raises(HTTPException) as err:
            await internal_lookup(
                LookupRequest(query="news.ycombinator.com", task_id="tsk_x", session_id="sess_x"),
                SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(gateway_runtime=runtime)),
                                headers={"X-Internal-Token": "internal-token"}),
            )
        candidates = err.value.detail["candidates"]
        assert {c["entry_id"] for c in candidates} == {e["entry_id"] for e in (runtime.vault_store.find_entries(SITE_URL))}
        assert all("username" in c and "updated_at" in c for c in candidates)
        # A username query disambiguates without the user in the loop: it
        # lands on the one matching entry and proceeds to its normal
        # always-ask approval pause for THAT entry.
        resolved = await internal_lookup(
            LookupRequest(query="praveenrajus", task_id="tsk_x", session_id="sess_x"),
            SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(gateway_runtime=runtime)),
                            headers={"X-Internal-Token": "internal-token"}),
        )
        assert resolved["status"] == "permission_required"
        assert resolved["entry_id"] == other["entry_id"]
    finally:
        await runtime.stop()
