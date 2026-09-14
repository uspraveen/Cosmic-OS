"""Scope denials must not masquerade as dead credentials.

A 401 means the token is dead — reconnect is the right advice. A 403
"Resource not accessible" means the token is alive but the operation sits
outside the repo-scoped App-installation grant; reconnecting cannot widen
that grant. The sentry incident (2026-09) sent the user chasing a reconnect
because both surfaced as "GitHub rejected the credential". These tests pin
the split at the client and at the health probe that users actually read.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BACKEND_ROOT))

from gateway.credentials.github_client import (  # noqa: E402
    GitHubApiClient,
    GitHubScopeError,
)
from gateway.credentials.manager import CredentialManager  # noqa: E402
from gateway.credentials.store import CredentialStore  # noqa: E402


def _client_responding(status: int, body: str) -> GitHubApiClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body)

    return GitHubApiClient(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


@pytest.mark.asyncio
async def test_403_resource_not_accessible_is_a_scope_error() -> None:
    client = _client_responding(
        403, '{"message":"Resource not accessible by integration"}'
    )
    with pytest.raises(GitHubScopeError) as excinfo:
        await client.get_authenticated_user("token")
    assert "repo-scoped" in str(excinfo.value)


@pytest.mark.asyncio
async def test_401_remains_a_dead_credential_error() -> None:
    client = _client_responding(401, '{"message":"Bad credentials"}')
    with pytest.raises(PermissionError) as excinfo:
        await client.get_authenticated_user("token")
    assert not isinstance(excinfo.value, GitHubScopeError)
    assert "rejected the credential" in str(excinfo.value)


@pytest.mark.asyncio
async def test_403_rate_limit_is_not_a_scope_error() -> None:
    client = _client_responding(403, '{"message":"API rate limit exceeded"}')
    with pytest.raises(PermissionError) as excinfo:
        await client.get_authenticated_user("token")
    assert not isinstance(excinfo.value, GitHubScopeError)


def _seed_github_account(store: CredentialStore, installation_id: str) -> str:
    account_id = store.create_account(provider="github")["account_id"]
    store.update_account(
        account_id, metadata_patch={"github_installation_id": installation_id}
    )
    store.store_credential(
        account_id=account_id,
        granted_scopes=[],
        access_token="stored-token",
        refresh_token="refresh-token",
        expires_at_ts=9999999999.0,
    )
    return account_id


class _ScopeDeniedClient:
    async def get_authenticated_user(self, token: str) -> dict:
        del token
        raise GitHubScopeError(
            "GitHub denied the operation (403): the COSMIC connector is "
            "repo-scoped to the connected repositories, and this operation "
            "is outside that grant."
        )


class _DeadCredentialClient:
    async def get_authenticated_user(self, token: str) -> dict:
        del token
        raise PermissionError(
            "GitHub rejected the credential (status=401): Bad credentials"
        )


def test_health_probe_reports_scope_denial_without_reconnect(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "credentials.db")
    account_id = _seed_github_account(store, installation_id="42")
    store.upsert_github_repositories(
        account_id=account_id,
        installation_id="42",
        repos=[
            {
                "id": 1,
                "full_name": "uspraveen/uspraveen.github.io",
                "clone_url": "https://github.com/uspraveen/uspraveen.github.io.git",
            }
        ],
    )
    mgr = CredentialManager(store=store, github_api_client=_ScopeDeniedClient())

    report = asyncio.run(mgr.probe_github_account_health(account_id))

    assert report["status"] == "scope_denied"
    assert report["needs_reconnect"] is False
    assert "repo-scoped" in report["error"]
    assert "uspraveen/uspraveen.github.io" in report["error"]


def test_health_probe_sends_dead_credentials_to_reconnect(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "credentials.db")
    account_id = _seed_github_account(store, installation_id="42")
    mgr = CredentialManager(store=store, github_api_client=_DeadCredentialClient())

    report = asyncio.run(mgr.probe_github_account_health(account_id))

    assert report["status"] == "reauth_required"
    assert report["needs_reconnect"] is True
