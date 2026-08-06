"""Tests for token-refresh failure classification and account self-heal.

Background: every connected Google account was being killed by failures that
had nothing to do with the OAuth grant. One account sat in needs_auth for a
week carrying `Token refresh failed: [Errno 24] Too many open files` - the
gateway had run out of file descriptors and never even reached Google, but the
account was marked dead and, because resolve_credential only considers active
accounts, nothing ever retried it.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

from gateway.credentials.manager import CredentialManager, classify_refresh_failure
from gateway.credentials.providers import TokenResponse
from gateway.credentials.store import CredentialStore


@pytest.fixture
def store():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    s = CredentialStore(db_path)
    yield s
    s.close()
    try:
        os.unlink(db_path)
    except PermissionError:
        pass


def _connected_account(store, *, status: str = "active"):
    account = store.create_account(
        provider="google",
        email="user@example.com",
        display_name="User",
        account_label="Google account",
        is_primary=True,
    )
    store.store_credential(
        account_id=account["account_id"],
        granted_scopes=["https://www.googleapis.com/auth/gmail.modify"],
        access_token="tok_expired",
        refresh_token="ref_valid",
        expires_at_ts=time.time() - 60,
    )
    if status != "active":
        store.update_account(account["account_id"], status=status)
    return account


def _http_error(status_code: int, payload: dict | str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://oauth2.googleapis.com/token")
    if isinstance(payload, dict):
        response = httpx.Response(status_code, json=payload, request=request)
    else:
        response = httpx.Response(status_code, text=payload, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


# ── Classification ───────────────────────────────────────────────────────────


def test_revoked_grant_is_fatal() -> None:
    assert classify_refresh_failure(_http_error(400, {"error": "invalid_grant"})) == "fatal"
    assert classify_refresh_failure(_http_error(401, {"error": "invalid_client"})) == "fatal"


def test_local_and_network_failures_are_never_fatal() -> None:
    """The exact class of failure that killed the real account: nothing here
    reached Google, so none of it says the refresh token is bad."""
    assert classify_refresh_failure(OSError(24, "Too many open files")) == "transient"
    assert classify_refresh_failure(httpx.ConnectTimeout("timed out")) == "transient"
    assert classify_refresh_failure(httpx.ReadTimeout("timed out")) == "transient"
    assert classify_refresh_failure(_http_error(503, {"error": "backendError"})) == "transient"
    assert classify_refresh_failure(_http_error(429, {"error": "rateLimitExceeded"})) == "transient"
    assert classify_refresh_failure(_http_error(500, "upstream boom")) == "transient"
    assert classify_refresh_failure(None) == "transient"


def test_ambiguous_4xx_keeps_historical_fatal_behaviour() -> None:
    """A 400/401 with no parseable OAuth error is how Google reports a genuine
    grant failure, so it must stay fatal - the fix narrows what counts as
    fatal, it does not stop real revocations from being caught."""
    assert classify_refresh_failure(_http_error(400, "<html>nope</html>")) == "fatal"


# ── Refresh behaviour ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_transient_refresh_failure_leaves_the_account_active(store, monkeypatch) -> None:
    account = _connected_account(store)
    mgr = CredentialManager(store)

    async def always_emfile(*args, **kwargs):
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(mgr, "_google_client_id", "cid", raising=False)
    monkeypatch.setattr(
        "gateway.credentials.providers.GoogleAdapter.refresh_token", always_emfile
    )
    monkeypatch.setattr("asyncio.sleep", _noop_sleep)

    with pytest.raises(OSError):
        await mgr.resolve_credential(
            provider="google",
            required_scopes=["https://www.googleapis.com/auth/gmail.modify"],
            allow_primary_fallback=True,
            operation_mode="read",
        )

    refreshed = store.get_account(account["account_id"])
    assert refreshed["status"] == "active", "a local fd error must not condemn the grant"
    assert refreshed["_metadata"]["last_refresh_failure_kind"] == "transient"
    # The user-facing reconnect banner reads last_auth_error - it must stay clean.
    assert not refreshed["_metadata"].get("last_auth_error")


@pytest.mark.asyncio
async def test_revoked_grant_still_marks_needs_auth(store, monkeypatch) -> None:
    """Guards the other direction: narrowing 'fatal' must not stop a genuinely
    revoked grant from asking the user to reconnect."""
    account = _connected_account(store)
    mgr = CredentialManager(store)

    async def revoked(*args, **kwargs):
        raise _http_error(400, {"error": "invalid_grant"})

    monkeypatch.setattr(
        "gateway.credentials.providers.GoogleAdapter.refresh_token", revoked
    )

    with pytest.raises(httpx.HTTPStatusError):
        await mgr.resolve_credential(
            provider="google",
            required_scopes=["https://www.googleapis.com/auth/gmail.modify"],
            allow_primary_fallback=True,
            operation_mode="read",
        )

    refreshed = store.get_account(account["account_id"])
    assert refreshed["status"] == "needs_auth"
    assert "invalid_grant" in refreshed["_metadata"]["last_auth_error"]


async def _noop_sleep(_seconds):
    return None


@pytest.mark.asyncio
async def test_transient_failure_is_retried_before_giving_up(store, monkeypatch) -> None:
    _connected_account(store)
    mgr = CredentialManager(store)
    attempts = {"n": 0}

    async def flaky(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectTimeout("blip")
        return TokenResponse(
            access_token="tok_new",
            refresh_token="ref_valid",
            expires_in=3600,
            scopes=["https://www.googleapis.com/auth/gmail.modify"],
        )

    monkeypatch.setattr(
        "gateway.credentials.providers.GoogleAdapter.refresh_token", flaky
    )
    monkeypatch.setattr("asyncio.sleep", _noop_sleep)

    resolved = await mgr.resolve_credential(
        provider="google",
        required_scopes=["https://www.googleapis.com/auth/gmail.modify"],
        allow_primary_fallback=True,
        operation_mode="read",
    )

    assert attempts["n"] == 2, "a single blip should be retried, not surfaced"
    assert resolved is not None
    assert resolved["access_token"] == "tok_new"


@pytest.mark.asyncio
async def test_fatal_failure_is_not_retried(store, monkeypatch) -> None:
    """Retrying a revoked grant just burns latency on every request."""
    _connected_account(store)
    mgr = CredentialManager(store)
    attempts = {"n": 0}

    async def revoked(*args, **kwargs):
        attempts["n"] += 1
        raise _http_error(400, {"error": "invalid_grant"})

    monkeypatch.setattr(
        "gateway.credentials.providers.GoogleAdapter.refresh_token", revoked
    )

    with pytest.raises(httpx.HTTPStatusError):
        await mgr.resolve_credential(
            provider="google",
            required_scopes=["https://www.googleapis.com/auth/gmail.modify"],
            allow_primary_fallback=True,
            operation_mode="read",
        )
    assert attempts["n"] == 1


# ── Self-heal ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_needs_auth_account_recovers_when_the_grant_is_actually_fine(
    store, monkeypatch
) -> None:
    """The stuck-account scenario end to end: an account wrongly condemned by
    a transient failure comes back on its own, with no user reconnect."""
    account = _connected_account(store, status="needs_auth")
    mgr = CredentialManager(store)

    async def healthy(*args, **kwargs):
        return TokenResponse(
            access_token="tok_new",
            refresh_token="ref_valid",
            expires_in=3600,
            scopes=["https://www.googleapis.com/auth/gmail.modify"],
        )

    monkeypatch.setattr(
        "gateway.credentials.providers.GoogleAdapter.refresh_token", healthy
    )

    assert await mgr.attempt_account_recovery(account["account_id"]) is True
    assert store.get_account(account["account_id"])["status"] == "active"


@pytest.mark.asyncio
async def test_recovery_leaves_a_genuinely_revoked_account_condemned(
    store, monkeypatch
) -> None:
    account = _connected_account(store, status="needs_auth")
    mgr = CredentialManager(store)

    async def revoked(*args, **kwargs):
        raise _http_error(400, {"error": "invalid_grant"})

    monkeypatch.setattr(
        "gateway.credentials.providers.GoogleAdapter.refresh_token", revoked
    )

    assert await mgr.attempt_account_recovery(account["account_id"]) is False
    assert store.get_account(account["account_id"])["status"] == "needs_auth"


@pytest.mark.asyncio
async def test_recovery_is_throttled_so_probes_cannot_stampede(store, monkeypatch) -> None:
    """The health probe runs constantly. Without a cooldown every probe would
    hammer Google's token endpoint for an account that is genuinely revoked."""
    account = _connected_account(store, status="needs_auth")
    mgr = CredentialManager(store)
    attempts = {"n": 0}

    async def revoked(*args, **kwargs):
        attempts["n"] += 1
        raise _http_error(400, {"error": "invalid_grant"})

    monkeypatch.setattr(
        "gateway.credentials.providers.GoogleAdapter.refresh_token", revoked
    )

    for _ in range(5):
        await mgr.attempt_account_recovery(account["account_id"])

    assert attempts["n"] == 1


@pytest.mark.asyncio
async def test_recovery_ignores_healthy_accounts(store) -> None:
    account = _connected_account(store, status="active")
    mgr = CredentialManager(store)
    assert await mgr.attempt_account_recovery(account["account_id"]) is False
