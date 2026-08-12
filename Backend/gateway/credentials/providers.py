"""OAuth provider adapters. Each provider implements authorize, exchange, refresh, revoke."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass
class TokenResponse:
    access_token: str
    refresh_token: str | None = None
    expires_in: int = 3600
    scopes: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


class ProviderAdapter:
    """Base class for OAuth provider adapters."""

    provider: str = ""
    authorize_url: str = ""
    token_url: str = ""
    revoke_url: str | None = None
    userinfo_url: str = ""

    def get_authorize_params(
        self,
        scopes: list[str],
        state: str,
        code_challenge: str,
        redirect_uri: str,
        client_id: str,
    ) -> dict[str, str]:
        raise NotImplementedError

    async def exchange_code(
        self,
        code: str,
        code_verifier: str,
        redirect_uri: str,
        client_id: str,
        client_secret: str,
    ) -> TokenResponse:
        raise NotImplementedError

    async def refresh_token(
        self,
        refresh_token: str,
        client_id: str,
        client_secret: str,
    ) -> TokenResponse:
        raise NotImplementedError

    async def revoke_token(
        self, token: str, client_id: str, client_secret: str
    ) -> bool:
        raise NotImplementedError

    async def get_user_info(self, access_token: str) -> dict[str, Any]:
        raise NotImplementedError

    def normalize_profile(self, raw: dict[str, Any]) -> dict[str, str]:
        """Map a provider's userinfo payload onto the fields accounts store.

        Lives on the adapter because only the adapter knows its own field
        names. The manager used to read Google's names (`id`, `email`, `name`,
        `picture`, `hd`) inline, which silently produced a blank account for
        any provider that spells them differently - and a blank
        provider_account_id collides with every other blank one, so two
        accounts would overwrite each other.
        """
        return {
            "provider_account_id": str(raw.get("id") or ""),
            "email": str(raw.get("email") or ""),
            "display_name": str(raw.get("name") or ""),
            "avatar_url": str(raw.get("picture") or ""),
            "hosted_domain": str(raw.get("hd") or ""),
        }


class GoogleAdapter(ProviderAdapter):
    provider = "google"
    authorize_url = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url = "https://oauth2.googleapis.com/token"
    revoke_url = "https://oauth2.googleapis.com/revoke"
    userinfo_url = "https://www.googleapis.com/oauth2/v2/userinfo"

    def get_authorize_params(
        self,
        scopes: list[str],
        state: str,
        code_challenge: str,
        redirect_uri: str,
        client_id: str,
    ) -> dict[str, str]:
        return {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent select_account",
            "include_granted_scopes": "false",
        }

    async def exchange_code(
        self,
        code: str,
        code_verifier: str,
        redirect_uri: str,
        client_id: str,
        client_secret: str,
    ) -> TokenResponse:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                self.token_url,
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                    "code_verifier": code_verifier,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        return TokenResponse(
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token"),
            expires_in=int(data.get("expires_in", 3600)),
            scopes=(data.get("scope") or "").split(),
            raw=data,
        )

    async def refresh_token(
        self,
        refresh_token: str,
        client_id: str,
        client_secret: str,
    ) -> TokenResponse:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                self.token_url,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        return TokenResponse(
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token") or refresh_token,
            expires_in=int(data.get("expires_in", 3600)),
            scopes=(data.get("scope") or "").split(),
            raw=data,
        )

    async def revoke_token(
        self, token: str, client_id: str, client_secret: str
    ) -> bool:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                self.revoke_url,
                params={"token": token},
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
            return resp.status_code in (200, 400)

    async def get_user_info(self, access_token: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                self.userinfo_url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            resp.raise_for_status()
            return resp.json()


class GitHubAdapter(ProviderAdapter):
    """GitHub App user-to-server OAuth.

    Deliberately a GitHub App rather than an OAuth App. An OAuth App's `repo`
    scope is read/write to every repository the user owns, with no way to
    narrow it; a GitHub App is granted per-repository at install time, so the
    user chooses exactly what Cosmic can touch. That matters here because Alpha
    runs `cursor-agent --force --sandbox disabled` autonomously.

    Consequences of that choice which this class encodes:

    - No `scope` parameter on the authorize URL. Repository access comes from
      the installation, not from scopes, and GitHub ignores `scope` for Apps.
    - Tokens expire (8h) and refresh (6mo), if the App has expiring user tokens
      enabled. This is the recommended setting and it is what lets the existing
      refresh machinery keep the connection alive without the user re-consenting.
    - GitHub returns errors as HTTP 200 with an `error` key in the body, so a
      failed exchange must be detected from the payload, not the status code.
    """

    provider = "github"
    authorize_url = "https://github.com/login/oauth/authorize"
    token_url = "https://github.com/login/oauth/access_token"
    revoke_url = None
    userinfo_url = "https://api.github.com/user"

    def get_authorize_params(
        self,
        scopes: list[str],
        state: str,
        code_challenge: str,
        redirect_uri: str,
        client_id: str,
    ) -> dict[str, str]:
        # code_challenge is accepted for interface symmetry; GitHub Apps do not
        # support PKCE, and sending it would be silently ignored at best.
        del code_challenge
        return {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": " ".join(scopes),
        }

    @staticmethod
    def _token_response(data: dict[str, Any], fallback_refresh: str | None = None) -> TokenResponse:
        if data.get("error"):
            # 200 OK with an error body. Raising keeps this on the same failure
            # path as any other provider so classify_refresh_failure sees it.
            raise httpx.HTTPStatusError(
                f"GitHub OAuth error: {data.get('error')}: {data.get('error_description') or ''}".strip(),
                request=httpx.Request("POST", "https://github.com/login/oauth/access_token"),
                response=httpx.Response(400, json=data),
            )
        return TokenResponse(
            access_token=str(data.get("access_token") or ""),
            refresh_token=str(data.get("refresh_token") or "") or fallback_refresh,
            # Non-expiring App tokens omit expires_in; a long horizon keeps them
            # out of the refresh path instead of refreshing on every call.
            expires_in=int(data.get("expires_in") or 28800),
            scopes=(data.get("scope") or "").split(),
            raw=data,
        )

    async def exchange_code(
        self,
        code: str,
        code_verifier: str,
        redirect_uri: str,
        client_id: str,
        client_secret: str,
    ) -> TokenResponse:
        del code_verifier
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                self.token_url,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
        return self._token_response(data)

    async def refresh_token(
        self,
        refresh_token: str,
        client_id: str,
        client_secret: str,
    ) -> TokenResponse:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                self.token_url,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
        return self._token_response(data, fallback_refresh=refresh_token)

    async def revoke_token(
        self, token: str, client_id: str, client_secret: str
    ) -> bool:
        """Revoke by deleting the App authorisation.

        Uses HTTP Basic with the client id/secret, which is how GitHub
        authenticates this endpoint - a bearer token is rejected.
        """
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.request(
                "DELETE",
                f"https://api.github.com/applications/{client_id}/token",
                auth=(client_id, client_secret),
                json={"access_token": token},
                headers={"Accept": "application/vnd.github+json"},
            )
            # 204 deleted, 404 already gone - both mean "not authorised anymore".
            return resp.status_code in (204, 404)

    async def get_user_info(self, access_token: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                self.userinfo_url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            resp.raise_for_status()
            return resp.json()

    def normalize_profile(self, raw: dict[str, Any]) -> dict[str, str]:
        # GitHub spells every one of these differently from Google, which is
        # exactly why this mapping belongs on the adapter.
        return {
            "provider_account_id": str(raw.get("id") or ""),
            "email": str(raw.get("email") or ""),
            "display_name": str(raw.get("name") or raw.get("login") or ""),
            "avatar_url": str(raw.get("avatar_url") or ""),
            "hosted_domain": "",
        }


_REGISTRY: dict[str, ProviderAdapter] = {
    "google": GoogleAdapter(),
    "github": GitHubAdapter(),
}


def get_provider_adapter(provider: str) -> ProviderAdapter:
    adapter = _REGISTRY.get(provider)
    if adapter is None:
        raise ValueError(f"Unknown provider: {provider}")
    return adapter
