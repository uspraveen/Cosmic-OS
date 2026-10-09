"""Tests for the account_hint substitution notice.

The personal Gmail (uspraveenraj@gmail.com) sat needs_auth for weeks while
the learnchain account was the only active Google account, and every
gmail.search carrying an explicit hint silently resolved to learnchain:
"Found 0 Gmail messages" read as an answer about an inbox that was never
searched. When a hint names an account that exists but is unusable and a
different account is substituted, the resolved payload must say so.

Run with:  python -m pytest tests/test_account_hint_notice.py -q
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

from gateway.credentials.manager import CredentialManager
from gateway.credentials.store import CredentialStore


@pytest.fixture
def tmp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    try:
        os.unlink(db_path)
    except PermissionError:
        pass  # Windows file locking — cleaned up eventually


@pytest.fixture
def store(tmp_db):
    s = CredentialStore(tmp_db)
    yield s
    s.close()


def _active_account(store, email):
    """The one healthy Google account the resolver can fall back to."""
    account = store.create_account(provider="google", email=email, display_name="Learnchain")
    store.store_credential(
        account_id=account["account_id"],
        granted_scopes=["calendar"],
        access_token="tok_live",
        refresh_token="ref_live",
        expires_at_ts=time.time() + 3600,
    )
    return account


def _needs_auth_account(store, email):
    """A Google account whose grant went stale (refresh stopped Sep 16)."""
    account = store.create_account(provider="google", email=email, display_name="Praveen Raj U S")
    store.update_account(account["account_id"], status="needs_auth")
    return account


@pytest.mark.asyncio
async def test_hint_naming_dead_account_carries_substitution_notice(store):
    dead = _needs_auth_account(store, "uspraveenraj@gmail.com")
    live = _active_account(store, "usp@thelearnchain.com")
    mgr = CredentialManager(store)

    resolved = await mgr.resolve_credential(
        provider="google",
        required_scopes=["calendar"],
        account_hint="uspraveenraj@gmail.com",
        allow_primary_fallback=True,
        operation_mode="read",
    )

    assert resolved is not None
    assert resolved["account_id"] == live["account_id"]
    notice = resolved["account_notice"]
    assert notice, "the substitution must be surfaced, not silent"
    assert "uspraveenraj@gmail.com needs re-authentication" in notice
    assert "usp@thelearnchain.com" in notice


@pytest.mark.asyncio
async def test_generic_domain_hint_naming_dead_account_also_notices(store):
    """The exact hint from the Oct 2026 incident: 'gmail.com' matched only the
    dead account, and the resolver still answered from learnchain."""
    _needs_auth_account(store, "uspraveenraj@gmail.com")
    live = _active_account(store, "usp@thelearnchain.com")
    mgr = CredentialManager(store)

    resolved = await mgr.resolve_credential(
        provider="google",
        required_scopes=["calendar"],
        account_hint="gmail.com",
        allow_primary_fallback=True,
        operation_mode="read",
    )

    assert resolved["account_id"] == live["account_id"]
    assert resolved["account_notice"] and "needs re-authentication" in resolved["account_notice"]


@pytest.mark.asyncio
async def test_hint_matching_the_active_account_has_no_notice(store):
    dead = _needs_auth_account(store, "uspraveenraj@gmail.com")
    live = _active_account(store, "usp@thelearnchain.com")
    mgr = CredentialManager(store)

    resolved = await mgr.resolve_credential(
        provider="google",
        required_scopes=["calendar"],
        account_hint="usp@thelearnchain.com",
        allow_primary_fallback=True,
        operation_mode="read",
    )

    assert resolved["account_id"] == live["account_id"]
    assert resolved["account_notice"] is None


@pytest.mark.asyncio
async def test_no_hint_single_active_account_has_no_notice(store):
    _needs_auth_account(store, "uspraveenraj@gmail.com")
    live = _active_account(store, "usp@thelearnchain.com")
    mgr = CredentialManager(store)

    resolved = await mgr.resolve_credential(
        provider="google",
        required_scopes=["calendar"],
        allow_primary_fallback=True,
        operation_mode="read",
    )

    assert resolved["account_id"] == live["account_id"]
    assert resolved["account_notice"] is None
