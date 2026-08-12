"""GitHub App user-to-server OAuth.

GitHub differs from Google in ways that fail silently rather than loudly, so
each one is pinned here: errors arrive as HTTP 200 with an error body, there is
no PKCE, repository access comes from the App installation rather than scopes,
and revocation authenticates with HTTP Basic rather than a bearer token.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BACKEND_ROOT))

from gateway.credentials.manager import (  # noqa: E402
    GITHUB_DEFAULT_SCOPES,
    CredentialManager,
)
from gateway.credentials.providers import (  # noqa: E402
    GitHubAdapter,
    get_provider_adapter,
)
from gateway.credentials.store import CredentialStore  # noqa: E402

CLIENT_ID = "Iv1.github-app-client-id"
CLIENT_SECRET = "github-app-client-secret"
REDIRECT = "http://127.0.0.1:8080/auth/callback/github"


@pytest.fixture
def adapter() -> GitHubAdapter:
    return GitHubAdapter()


def _mock_transport(handler):
    return httpx.MockTransport(handler)


class TestRegistration:
    def test_github_is_registered(self) -> None:
        assert isinstance(get_provider_adapter("github"), GitHubAdapter)

    def test_google_is_untouched(self) -> None:
        assert get_provider_adapter("google").provider == "google"


class TestAuthorizeUrl:
    def test_it_points_at_github(self, adapter: GitHubAdapter) -> None:
        assert adapter.authorize_url == "https://github.com/login/oauth/authorize"

    def test_it_carries_client_id_state_and_redirect(self, adapter: GitHubAdapter) -> None:
        params = adapter.get_authorize_params(
            scopes=["read:user"],
            state="abc123",
            code_challenge="ignored",
            redirect_uri=REDIRECT,
            client_id=CLIENT_ID,
        )
        assert params["client_id"] == CLIENT_ID
        assert params["state"] == "abc123"
        assert params["redirect_uri"] == REDIRECT

    def test_it_does_not_send_pkce(self, adapter: GitHubAdapter) -> None:
        """GitHub Apps do not support PKCE; sending it is at best ignored."""
        params = adapter.get_authorize_params(
            scopes=["read:user"],
            state="abc",
            code_challenge="challenge-value",
            redirect_uri=REDIRECT,
            client_id=CLIENT_ID,
        )
        assert "code_challenge" not in params
        assert "code_challenge_method" not in params
        assert "challenge-value" not in str(params)

    def test_default_scopes_do_not_request_repo_access(self) -> None:
        """The entire point of choosing a GitHub App: repository access is
        granted per-repo at install time, never by a blanket `repo` scope."""
        assert "repo" not in GITHUB_DEFAULT_SCOPES
        assert GITHUB_DEFAULT_SCOPES == ("read:user",)


class TestTokenExchange:
    @pytest.mark.asyncio
    async def test_a_successful_exchange_returns_both_tokens(
        self, adapter: GitHubAdapter, monkeypatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["Accept"] == "application/json"
            return httpx.Response(
                200,
                json={
                    "access_token": "ghu_access",
                    "refresh_token": "ghr_refresh",
                    "expires_in": 28800,
                    "scope": "read:user",
                },
            )

        _patch_client(monkeypatch, handler)
        token = await adapter.exchange_code(
            code="code", code_verifier="", redirect_uri=REDIRECT,
            client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
        )
        assert token.access_token == "ghu_access"
        assert token.refresh_token == "ghr_refresh"
        assert token.expires_in == 28800

    @pytest.mark.asyncio
    async def test_an_error_body_on_http_200_still_raises(
        self, adapter: GitHubAdapter, monkeypatch
    ) -> None:
        """GitHub returns OAuth failures as 200 with an error key. Treating that
        as success would store an empty access token as if it worked."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "error": "bad_verification_code",
                    "error_description": "The code passed is incorrect or expired.",
                },
            )

        _patch_client(monkeypatch, handler)
        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            await adapter.exchange_code(
                code="stale", code_verifier="", redirect_uri=REDIRECT,
                client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
            )
        assert "bad_verification_code" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_non_expiring_token_gets_a_long_horizon(
        self, adapter: GitHubAdapter, monkeypatch
    ) -> None:
        """Apps without expiring tokens omit expires_in. Defaulting to 0 would
        mark the credential expired and refresh it on every single call."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"access_token": "ghu_x", "scope": ""})

        _patch_client(monkeypatch, handler)
        token = await adapter.exchange_code(
            code="c", code_verifier="", redirect_uri=REDIRECT,
            client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
        )
        assert token.expires_in >= 28800


class TestRefresh:
    @pytest.mark.asyncio
    async def test_a_rotated_refresh_token_is_returned(
        self, adapter: GitHubAdapter, monkeypatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"access_token": "ghu_new", "refresh_token": "ghr_new", "expires_in": 28800},
            )

        _patch_client(monkeypatch, handler)
        token = await adapter.refresh_token("ghr_old", CLIENT_ID, CLIENT_SECRET)
        assert token.refresh_token == "ghr_new"

    @pytest.mark.asyncio
    async def test_the_old_refresh_token_is_kept_when_none_comes_back(
        self, adapter: GitHubAdapter, monkeypatch
    ) -> None:
        """Losing the refresh token here would silently end the connection."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"access_token": "ghu_new", "expires_in": 28800})

        _patch_client(monkeypatch, handler)
        token = await adapter.refresh_token("ghr_old", CLIENT_ID, CLIENT_SECRET)
        assert token.refresh_token == "ghr_old"

    @pytest.mark.asyncio
    async def test_a_dead_grant_raises_so_it_is_classified(
        self, adapter: GitHubAdapter, monkeypatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"error": "bad_refresh_token"})

        _patch_client(monkeypatch, handler)
        with pytest.raises(httpx.HTTPStatusError):
            await adapter.refresh_token("dead", CLIENT_ID, CLIENT_SECRET)


class TestProfile:
    def test_github_field_names_are_mapped(self, adapter: GitHubAdapter) -> None:
        identity = adapter.normalize_profile(
            {"id": 12345, "login": "uspraveen", "name": "Praveen Raj U S",
             "email": "usp@example.com", "avatar_url": "https://avatars/x.png"}
        )
        assert identity["provider_account_id"] == "12345"
        assert identity["display_name"] == "Praveen Raj U S"
        assert identity["email"] == "usp@example.com"
        assert identity["avatar_url"] == "https://avatars/x.png"

    def test_login_is_used_when_the_account_has_no_display_name(
        self, adapter: GitHubAdapter
    ) -> None:
        identity = adapter.normalize_profile({"id": 1, "login": "uspraveen"})
        assert identity["display_name"] == "uspraveen"

    def test_a_private_email_does_not_break_the_mapping(
        self, adapter: GitHubAdapter
    ) -> None:
        """GitHub returns null email when the user keeps it private."""
        identity = adapter.normalize_profile({"id": 1, "login": "u", "email": None})
        assert identity["email"] == ""
        assert identity["provider_account_id"] == "1"


class TestManagerWiring:
    @pytest.fixture
    def manager(self, tmp_path) -> CredentialManager:
        store = CredentialStore(db_path=tmp_path / "credentials.db")
        return CredentialManager(
            store,
            google_client_id="google-id",
            google_client_secret="google-secret",
            google_redirect_uri="http://localhost/google",
            github_client_id=CLIENT_ID,
            github_client_secret=CLIENT_SECRET,
            github_redirect_uri=REDIRECT,
        )

    def test_github_flow_uses_githubs_own_credentials(
        self, manager: CredentialManager
    ) -> None:
        """The bug this prevents: the manager used to pass Google's client id
        and secret to every provider, so connecting GitHub would have POSTed
        Google's client secret to github.com."""
        result = manager.start_oauth_flow(provider="github")
        params = parse_qs(urlparse(result["authorize_url"]).query)
        assert params["client_id"] == [CLIENT_ID]
        assert params["redirect_uri"] == [REDIRECT]
        assert "google" not in result["authorize_url"]

    def test_google_flow_still_uses_googles_credentials(
        self, manager: CredentialManager
    ) -> None:
        result = manager.start_oauth_flow(provider="google")
        params = parse_qs(urlparse(result["authorize_url"]).query)
        assert params["client_id"] == ["google-id"]

    def test_unconfigured_github_refuses_to_start(self, tmp_path) -> None:
        store = CredentialStore(db_path=tmp_path / "credentials.db")
        bare = CredentialManager(store, google_client_id="g", google_client_secret="s")
        assert bare.github_configured is False
        with pytest.raises(ValueError):
            bare.start_oauth_flow(provider="github")

    def test_an_unknown_provider_never_borrows_another_ones_secret(
        self, manager: CredentialManager
    ) -> None:
        with pytest.raises(ValueError):
            manager._oauth_client("gitlab")

    def test_provider_configured_reports_per_provider(
        self, manager: CredentialManager
    ) -> None:
        assert manager.provider_configured("google") is True
        assert manager.provider_configured("github") is True
        assert manager.provider_configured("gitlab") is False


def _patch_client(monkeypatch, handler) -> None:
    """Route every httpx.AsyncClient in the adapter through a mock transport."""
    real_init = httpx.AsyncClient.__init__

    def patched(self, *args, **kwargs):
        kwargs["transport"] = _mock_transport(handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)


class TestScopeGating:
    """A GitHub App user token has no scopes, and that is not a failure.

    Google's rule treats an empty granted-scope set as "no access", so applying
    it to GitHub rejects every healthy credential - the account shows as
    connected while `git push` silently gets no token at all.
    """

    def test_github_resolves_with_no_scopes(self) -> None:
        from gateway.credentials.manager import provider_scopes_satisfy

        assert provider_scopes_satisfy("github", [], []) is True
        assert provider_scopes_satisfy("github", [], ["anything"]) is True

    def test_google_rules_are_completely_unchanged(self) -> None:
        from gateway.credentials.manager import (
            google_scopes_satisfy,
            provider_scopes_satisfy,
        )

        cases = [
            ([], []),
            ([], ["https://www.googleapis.com/auth/gmail.modify"]),
            (["https://www.googleapis.com/auth/gmail.modify"], ["https://www.googleapis.com/auth/gmail.modify"]),
            (["https://www.googleapis.com/auth/drive"], ["https://www.googleapis.com/auth/drive.readonly"]),
            (["https://www.googleapis.com/auth/calendar"], ["https://www.googleapis.com/auth/gmail.modify"]),
        ]
        for granted, required in cases:
            assert provider_scopes_satisfy("google", granted, required) == google_scopes_satisfy(
                granted, required
            )

    def test_an_unknown_provider_keeps_the_strict_rule(self) -> None:
        """Only GitHub is exempt; nothing else silently loosens."""
        from gateway.credentials.manager import provider_scopes_satisfy

        assert provider_scopes_satisfy("gitlab", [], ["read"]) is False
