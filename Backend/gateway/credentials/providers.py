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


_REGISTRY: dict[str, ProviderAdapter] = {
    "google": GoogleAdapter(),
}


def get_provider_adapter(provider: str) -> ProviderAdapter:
    adapter = _REGISTRY.get(provider)
    if adapter is None:
        raise ValueError(f"Unknown provider: {provider}")
    return adapter
