"""Notion public-connection OAuth.

Notion differs from Google and GitHub in ways that fail silently when misread,
so each one is pinned here: capabilities are fixed on the connection (no scope
parameter, empty scope lists), the token endpoint wants HTTP Basic auth with a
JSON body, tokens may never expire (no refresh token, and `expires_in` absent),
and the workspace identity rides the token response rather than /users/me.

Every async test is wrapped in asyncio.run() on purpose: the VM's venv has no
pytest-asyncio, so deploy verification must be able to run this file as-is.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BACKEND_ROOT))

from gateway.credentials.manager import (  # noqa: E402
    CredentialManager,
    provider_scopes_satisfy,
)
from gateway.credentials.providers import (  # noqa: E402
    NotionAdapter,
    get_provider_adapter,
)
from gateway.credentials.store import CredentialStore  # noqa: E402

CLIENT_ID = "3eed872b-notion-client-id"
CLIENT_SECRET = "secret_notion-client-secret"
REDIRECT = "http://localhost:8087/"


@pytest.fixture
def adapter() -> NotionAdapter:
    return NotionAdapter()


@pytest.fixture
def manager(tmp_path) -> CredentialManager:
    store = CredentialStore(db_path=tmp_path / "credentials.db")
    return CredentialManager(
        store,
        notion_client_id=CLIENT_ID,
        notion_client_secret=CLIENT_SECRET,
        notion_redirect_uri=REDIRECT,
    )


def _mock_transport(handler):
    return httpx.MockTransport(handler)


def _patch_client(monkeypatch, handler) -> None:
    """Route every httpx.AsyncClient in the adapter through a mock transport."""
    real_init = httpx.AsyncClient.__init__

    def patched(self, *args, **kwargs):
        kwargs["transport"] = _mock_transport(handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)


def run(coro):
    return asyncio.run(coro)


class TestRegistration:
    def test_notion_is_registered(self) -> None:
        assert isinstance(get_provider_adapter("notion"), NotionAdapter)

    def test_google_and_github_are_untouched(self) -> None:
        assert get_provider_adapter("google").provider == "google"
        assert get_provider_adapter("github").provider == "github"


class TestAuthorizeUrl:
    def test_it_points_at_notion(self, adapter: NotionAdapter) -> None:
        assert adapter.authorize_url == "https://api.notion.com/v1/oauth/authorize"

    def test_it_carries_client_id_state_and_redirect(self, adapter: NotionAdapter) -> None:
        params = adapter.get_authorize_params(
            scopes=[],
            state="abc123",
            code_challenge="ignored",
            redirect_uri=REDIRECT,
            client_id=CLIENT_ID,
        )
        assert params["client_id"] == CLIENT_ID
        assert params["state"] == "abc123"
        assert params["redirect_uri"] == REDIRECT
        assert params["response_type"] == "code"
        assert params["owner"] == "user"

    def test_it_requests_no_scopes(self, adapter: NotionAdapter) -> None:
        """Capabilities are configured on the connection in Notion's dashboard;
        asking for scopes at authorize time would at best be ignored."""
        params = adapter.get_authorize_params(
            scopes=["whatever"],
            state="abc",
            code_challenge="challenge-value",
            redirect_uri=REDIRECT,
            client_id=CLIENT_ID,
        )
        assert "scope" not in params

    def test_it_does_not_send_pkce(self, adapter: NotionAdapter) -> None:
        params = adapter.get_authorize_params(
            scopes=[],
            state="abc",
            code_challenge="challenge-value",
            redirect_uri=REDIRECT,
            client_id=CLIENT_ID,
        )
        assert "code_challenge" not in params
        assert "code_challenge_method" not in params

    def test_the_manager_builds_the_full_authorize_url(self, manager: CredentialManager) -> None:
        result = manager.start_oauth_flow(provider="notion")
        assert result["authorize_url"].startswith(
            "https://api.notion.com/v1/oauth/authorize?"
        )
        params = parse_qs(urlparse(result["authorize_url"]).query)
        assert params["client_id"] == [CLIENT_ID]
        assert params["redirect_uri"] == [REDIRECT]
        assert result["state"] in manager._pending_flows


class TestConfigurationGuards:
    def test_unconfigured_notion_refuses_to_start(self, tmp_path) -> None:
        store = CredentialStore(db_path=tmp_path / "credentials.db")
        bare = CredentialManager(store)
        assert bare.notion_configured is False
        with pytest.raises(ValueError):
            bare.start_oauth_flow(provider="notion")

    def test_notion_configured_needs_both_id_and_secret(self, tmp_path) -> None:
        store = CredentialStore(db_path=tmp_path / "credentials.db")
        assert CredentialManager(store, notion_client_id=CLIENT_ID).notion_configured is False
        assert (
            CredentialManager(store, notion_client_secret=CLIENT_SECRET).notion_configured
            is False
        )


class TestTokenExchange:
    def test_exchange_uses_basic_auth_and_a_json_body(self, adapter: NotionAdapter, monkeypatch) -> None:
        seen: dict[str, httpx.Request] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["request"] = request
            return httpx.Response(
                200,
                json={"access_token": "ntn_access", "bot_id": "bot-1"},
            )

        _patch_client(monkeypatch, handler)
        token = run(
            adapter.exchange_code(
                code="the-code",
                code_verifier="",
                redirect_uri=REDIRECT,
                client_id=CLIENT_ID,
                client_secret=CLIENT_SECRET,
            )
        )
        request = seen["request"]
        assert request.headers["Authorization"].startswith("Basic ")
        import base64

        expected = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        assert request.headers["Authorization"] == f"Basic {expected}"
        body = json_body(request)
        assert body["grant_type"] == "authorization_code"
        assert body["code"] == "the-code"
        assert body["redirect_uri"] == REDIRECT
        assert token.access_token == "ntn_access"
        assert token.scopes == []

    def test_a_non_expiring_token_gets_a_long_horizon(
        self, adapter: NotionAdapter, monkeypatch
    ) -> None:
        """Notion tokens do not expire and omit expires_in. Defaulting to 0
        would push every resolve through a refresh round trip for a provider
        that may not even hand out refresh tokens."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"access_token": "ntn_x"})

        _patch_client(monkeypatch, handler)
        token = run(
            adapter.exchange_code(
                code="c", code_verifier="", redirect_uri=REDIRECT,
                client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
            )
        )
        assert token.expires_in >= 28800

    def test_an_error_body_never_becomes_a_token(
        self, adapter: NotionAdapter, monkeypatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"error": "invalid_grant"})

        _patch_client(monkeypatch, handler)
        with pytest.raises(httpx.HTTPStatusError):
            run(
                adapter.exchange_code(
                    code="stale", code_verifier="", redirect_uri=REDIRECT,
                    client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                )
            )


class TestRefresh:
    def test_refresh_uses_the_refresh_grant(self, adapter: NotionAdapter, monkeypatch) -> None:
        seen: dict[str, httpx.Request] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["request"] = request
            return httpx.Response(
                200,
                json={"access_token": "ntn_new", "refresh_token": "ntn_rotated"},
            )

        _patch_client(monkeypatch, handler)
        token = run(adapter.refresh_token("ntn_old", CLIENT_ID, CLIENT_SECRET))
        body = json_body(seen["request"])
        assert body["grant_type"] == "refresh_token"
        assert body["refresh_token"] == "ntn_old"
        assert token.access_token == "ntn_new"
        assert token.refresh_token == "ntn_rotated"

    def test_an_unchanged_refresh_token_is_kept(self, adapter: NotionAdapter, monkeypatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"access_token": "ntn_new"})

        _patch_client(monkeypatch, handler)
        token = run(adapter.refresh_token("ntn_old", CLIENT_ID, CLIENT_SECRET))
        assert token.refresh_token == "ntn_old"


class TestProfile:
    def test_a_bot_response_uses_the_workspace_identity(self, adapter: NotionAdapter) -> None:
        raw = {
            "object": "user",
            "id": "bot-user-id",
            "name": "Cosmic",
            "bot": {
                "owner": {
                    "type": "user",
                    "user": {
                        "person": {"email": "praveen@example.com"},
                        "name": "Praveen",
                    },
                },
                "workspace_name": "Praveen's Notion",
                "workspace_icon": "https://example.com/icon.png",
            },
        }
        identity = adapter.normalize_profile(raw)
        assert identity["provider_account_id"] == "bot-user-id"
        assert identity["display_name"] == "Praveen's Notion"
        assert identity["email"] == "praveen@example.com"
        assert identity["avatar_url"] == "https://example.com/icon.png"

    def test_a_person_response_stands_alone(self, adapter: NotionAdapter) -> None:
        raw = {
            "object": "user",
            "id": "user-id",
            "name": "Praveen Raj U S",
            "person": {"email": "praveen@example.com"},
            "avatar_url": "https://example.com/avatar.png",
        }
        identity = adapter.normalize_profile(raw)
        assert identity["provider_account_id"] == "user-id"
        assert identity["display_name"] == "Praveen Raj U S"
        assert identity["email"] == "praveen@example.com"

    def test_workspace_facts_ride_the_token_response(self, adapter: NotionAdapter) -> None:
        """User-scoped connections: /users/me returns the person, and only the
        token response names the workspace. Without enrichment the account
        would be labelled with a person's name instead of the workspace."""
        profile = {"id": "user-id", "name": "Praveen Raj U S"}
        token_raw = {
            "access_token": "ntn_x",
            "workspace_name": "Praveen's Notion",
            "workspace_icon": "https://example.com/icon.png",
            "owner": {
                "type": "user",
                "user": {"person": {"email": "praveen@example.com"}},
            },
        }
        identity = adapter.normalize_profile(
            adapter.enrich_profile_from_token(profile, token_raw)
        )
        assert identity["display_name"] == "Praveen's Notion"
        assert identity["email"] == "praveen@example.com"

    def test_emoji_workspace_icons_are_not_treated_as_urls(self, adapter: NotionAdapter) -> None:
        raw = {
            "object": "user",
            "id": "bot-user-id",
            "bot": {"workspace_name": "W", "workspace_icon": "🧩"},
        }
        assert adapter.normalize_profile(raw)["avatar_url"] == ""

    def test_the_enrich_hook_is_a_noop_for_other_providers(self) -> None:
        google = get_provider_adapter("google")
        profile = {"id": "x"}
        assert google.enrich_profile_from_token(profile, {"workspace_name": "W"}) == profile


class TestScopeGating:
    """Notion grants capabilities on the connection, not scope strings - the
    empty granted list must read as "full capability", never as "no access"."""

    def test_notion_resolves_with_no_scopes(self) -> None:
        assert provider_scopes_satisfy("notion", [], []) is True
        assert provider_scopes_satisfy("notion", [], ["anything"]) is True

    def test_google_still_demands_scopes(self) -> None:
        assert provider_scopes_satisfy("google", [], []) is False


class TestResolveWithoutRefreshToken:
    """A non-expiring credential ships without a refresh token. resolve used
    to demand one unconditionally, so a healthy Notion grant could never be
    resolved - the account showed connected while every call got no token."""

    def _store_account(self, store: CredentialStore, provider: str, refresh_token: str) -> None:
        account = store.create_account(
            provider=provider,
            provider_account_id="p-1",
            email="user@example.com",
            display_name="Workspace",
            account_label="Workspace",
            metadata={},
        )
        far_future = time.time() + 30 * 86400
        store.store_credential(
            account_id=account["account_id"],
            granted_scopes=[],
            access_token="ntn_stored",
            refresh_token=refresh_token,
            expires_at_ts=far_future,
        )

    def test_notion_resolves_a_refreshless_credential(self, tmp_path) -> None:
        store = CredentialStore(db_path=tmp_path / "credentials.db")
        self._store_account(store, "notion", refresh_token="")
        manager = CredentialManager(
            store,
            notion_client_id=CLIENT_ID,
            notion_client_secret=CLIENT_SECRET,
            notion_redirect_uri=REDIRECT,
        )
        resolved = run(
            manager.resolve_credential(provider="notion", required_scopes=[])
        )
        assert resolved is not None
        assert resolved["access_token"] == "ntn_stored"

    def test_google_still_requires_a_refresh_token(self, tmp_path) -> None:
        store = CredentialStore(db_path=tmp_path / "credentials.db")
        self._store_account(store, "google", refresh_token="")
        manager = CredentialManager(store)
        resolved = run(
            manager.resolve_credential(provider="google", required_scopes=[])
        )
        assert resolved is None

    def test_an_expired_refreshless_notion_token_does_not_resolve(self, tmp_path) -> None:
        store = CredentialStore(db_path=tmp_path / "credentials.db")
        account = store.create_account(
            provider="notion",
            provider_account_id="p-1",
            email="user@example.com",
            display_name="Workspace",
            account_label="Workspace",
            metadata={},
        )
        store.store_credential(
            account_id=account["account_id"],
            granted_scopes=[],
            access_token="ntn_stale",
            refresh_token="",
            expires_at_ts=time.time() - 60,
        )
        manager = CredentialManager(
            store,
            notion_client_id=CLIENT_ID,
            notion_client_secret=CLIENT_SECRET,
            notion_redirect_uri=REDIRECT,
        )
        resolved = run(
            manager.resolve_credential(provider="notion", required_scopes=[])
        )
        assert resolved is None


class TestConnectFlow:
    def test_the_full_connect_stores_a_workspace_labelled_account(
        self, manager: CredentialManager, monkeypatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/oauth/token":
                return httpx.Response(
                    200,
                    json={
                        "access_token": "ntn_access",
                        "bot_id": "bot-1",
                        "workspace_name": "Praveen's Notion",
                        "workspace_icon": "https://example.com/icon.png",
                        "owner": {
                            "type": "user",
                            "user": {"person": {"email": "praveen@example.com"}},
                        },
                    },
                )
            if request.url.path == "/v1/users/me":
                return httpx.Response(
                    200,
                    json={
                        "object": "user",
                        "id": "bot-user-id",
                        "name": "Cosmic",
                        "bot": {"workspace_name": "Praveen's Notion"},
                    },
                )
            return httpx.Response(404)

        _patch_client(monkeypatch, handler)
        started = manager.start_oauth_flow(provider="notion")
        account = run(
            manager.handle_oauth_callback(
                code="the-code", state=started["state"]
            )
        )
        assert account["display_name"] == "Praveen's Notion"
        assert account["email"] == "praveen@example.com"
        listing = manager.list_accounts("notion")
        assert len(listing) == 1
        assert listing[0]["status"] == "active"
        assert listing[0]["account_display_label"] == "Praveen's Notion"


def json_body(request: httpx.Request) -> dict:
    import json

    return json.loads(request.content.decode("utf-8"))
