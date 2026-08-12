"""Google's OAuth behaviour must survive making the manager multi-provider.

The adapter layer was already provider-generic, but CredentialManager was not:
start_oauth_flow and handle_oauth_callback passed Google's client id, secret and
redirect URI for *every* provider, and read Google's profile field names
directly. Adding GitHub means parameterising both.

This is live production auth for the user's Google accounts, so the Google half
is pinned first and in detail. Every assertion here describes behaviour that
existed before the refactor.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BACKEND_ROOT))

from gateway.credentials.manager import CredentialManager  # noqa: E402
from gateway.credentials.providers import (  # noqa: E402
    GoogleAdapter,
    get_provider_adapter,
)
from gateway.credentials.store import CredentialStore  # noqa: E402

GOOGLE_ID = "google-client-id.apps.googleusercontent.com"
GOOGLE_SECRET = "google-client-secret"
GOOGLE_REDIRECT = "http://127.0.0.1:8080/auth/callback/google"


@pytest.fixture
def manager(tmp_path) -> CredentialManager:
    store = CredentialStore(db_path=tmp_path / "credentials.db")
    return CredentialManager(
        store,
        google_client_id=GOOGLE_ID,
        google_client_secret=GOOGLE_SECRET,
        google_redirect_uri=GOOGLE_REDIRECT,
    )


class TestGoogleAuthorizeUrlIsUnchanged:
    """The exact URL the user's browser is sent to."""

    def _params(self, manager: CredentialManager) -> dict[str, list[str]]:
        result = manager.start_oauth_flow(provider="google")
        return parse_qs(urlparse(result["authorize_url"]).query)

    def test_it_points_at_google(self, manager: CredentialManager) -> None:
        result = manager.start_oauth_flow(provider="google")
        assert result["authorize_url"].startswith(
            "https://accounts.google.com/o/oauth2/v2/auth?"
        )

    def test_it_carries_googles_client_id_and_redirect(
        self, manager: CredentialManager
    ) -> None:
        params = self._params(manager)
        assert params["client_id"] == [GOOGLE_ID]
        assert params["redirect_uri"] == [GOOGLE_REDIRECT]

    def test_it_keeps_the_offline_consent_parameters(
        self, manager: CredentialManager
    ) -> None:
        """Dropping any of these silently costs the refresh token, which is the
        failure that disconnects accounts."""
        params = self._params(manager)
        assert params["access_type"] == ["offline"]
        assert params["prompt"] == ["consent select_account"]
        assert params["include_granted_scopes"] == ["false"]
        assert params["response_type"] == ["code"]

    def test_it_uses_pkce(self, manager: CredentialManager) -> None:
        params = self._params(manager)
        assert params["code_challenge_method"] == ["S256"]
        assert len(params["code_challenge"][0]) > 20

    def test_default_scopes_are_applied_when_none_are_requested(
        self, manager: CredentialManager
    ) -> None:
        from gateway.credentials.manager import GOOGLE_DEFAULT_SCOPES

        params = self._params(manager)
        assert params["scope"][0].split() == list(GOOGLE_DEFAULT_SCOPES)

    def test_requested_scopes_override_the_defaults(
        self, manager: CredentialManager
    ) -> None:
        result = manager.start_oauth_flow(
            provider="google", scopes=["https://www.googleapis.com/auth/drive.readonly"]
        )
        params = parse_qs(urlparse(result["authorize_url"]).query)
        assert params["scope"] == ["https://www.googleapis.com/auth/drive.readonly"]


class TestFlowState:
    def test_state_is_returned_and_registered(self, manager: CredentialManager) -> None:
        result = manager.start_oauth_flow(provider="google")
        assert result["state"]
        assert result["state"] in manager._pending_flows

    def test_each_flow_gets_a_distinct_state_and_verifier(
        self, manager: CredentialManager
    ) -> None:
        first = manager.start_oauth_flow(provider="google")
        second = manager.start_oauth_flow(provider="google")
        assert first["state"] != second["state"]
        assert (
            manager._pending_flows[first["state"]].code_verifier
            != manager._pending_flows[second["state"]].code_verifier
        )

    def test_metadata_is_carried_through_to_the_callback(
        self, manager: CredentialManager
    ) -> None:
        result = manager.start_oauth_flow(
            provider="google",
            metadata={"account_label": "Work", "selected_tools": ["gmail"]},
        )
        flow = manager._pending_flows[result["state"]]
        assert flow.metadata["account_label"] == "Work"
        assert flow.metadata["selected_tools"] == ["gmail"]


class TestConfigurationGuards:
    def test_unconfigured_google_refuses_to_start(self, tmp_path) -> None:
        store = CredentialStore(db_path=tmp_path / "credentials.db")
        bare = CredentialManager(store)
        assert bare.google_configured is False
        with pytest.raises(ValueError):
            bare.start_oauth_flow(provider="google")

    def test_google_configured_needs_both_id_and_secret(self, tmp_path) -> None:
        store = CredentialStore(db_path=tmp_path / "credentials.db")
        assert (
            CredentialManager(store, google_client_id=GOOGLE_ID).google_configured
            is False
        )
        assert (
            CredentialManager(
                store, google_client_secret=GOOGLE_SECRET
            ).google_configured
            is False
        )


class TestAdapterRegistry:
    def test_google_resolves_to_the_google_adapter(self) -> None:
        assert isinstance(get_provider_adapter("google"), GoogleAdapter)

    def test_an_unknown_provider_raises_rather_than_defaulting(self) -> None:
        """A provider that silently fell back to Google's adapter would send
        Google's client secret to somebody else's token endpoint."""
        with pytest.raises(ValueError):
            get_provider_adapter("definitely-not-a-provider")
