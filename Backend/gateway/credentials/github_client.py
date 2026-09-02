"""GitHub REST API client for repository enumeration.

COSMIC deliberately uses a GitHub App rather than an OAuth App so repository
access is granted per repository at install time. That decision shapes this
client in two ways:

- Repository enumeration is a user-to-server call. ``GET /user/installations``
  and ``GET /user/installations/{id}/repositories`` both accept the plain user
  access token the OAuth exchange already stored, so no App private key or JWT
  minting is needed here. The Gateway never holds the App private key.
- Tokens expire and refresh through the shared credential machinery; callers
  resolve a fresh token via CredentialManager and pass it in, so this client
  stays stateless and never sees a refresh token.

Pagination is bounded: a pathological account cannot stall the gateway's
event loop on an endless repo list.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"

_API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


class GitHubApiError(RuntimeError):
    """A GitHub API call failed in a way the caller should surface."""


class GitHubApiClient:
    """Small stateless client for the two enumeration calls Alpha needs."""

    def __init__(self, *, client: httpx.AsyncClient | None = None, timeout_sec: float = 30.0) -> None:
        self._client = client or httpx.AsyncClient(timeout=timeout_sec, http2=True)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_authenticated_user(self, access_token: str) -> dict[str, Any]:
        """Identity behind this token — the cheapest live liveness check.

        A token can look valid in the store yet be revoked or expired on
        GitHub's side; only an authenticated call settles it. ``GET /user``
        is one read-only round trip with no side effects.
        """
        return await self._get_json(access_token, "/user")

    async def list_user_installations(self, access_token: str) -> list[dict[str, Any]]:
        """Installations of GitHub Apps visible to the authenticated user."""
        payload = await self._get_json(access_token, "/user/installations")
        installations = payload.get("installations")
        if not isinstance(installations, list):
            return []
        return [item for item in installations if isinstance(item, dict)]

    async def list_installation_repositories(
        self,
        access_token: str,
        installation_id: str | int,
        *,
        max_pages: int = 10,
    ) -> list[dict[str, Any]]:
        """Repositories of one installation the authenticated user can access.

        Returns raw GitHub repository objects (``id``, ``full_name``,
        ``clone_url``, ``permissions``, ...). Empty installation id yields an
        empty list rather than an error: a connect without an installation has
        nothing to enumerate and that is not a failure.
        """
        normalized = str(installation_id or "").strip()
        if not normalized:
            return []
        collected: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for page in range(1, max(1, int(max_pages)) + 1):
            payload = await self._get_json(
                access_token,
                f"/user/installations/{installation_id}/repositories",
                params={"per_page": 100, "page": page},
            )
            repositories = payload.get("repositories")
            if not isinstance(repositories, list):
                break
            for item in repositories:
                if isinstance(item, dict) and item.get("id") is not None:
                    key = str(item.get("id"))
                    if key not in seen_ids:
                        seen_ids.add(key)
                        collected.append(item)
            if len(repositories) < 100:
                break
        return collected

    async def _get_json(
        self,
        access_token: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not access_token:
            raise GitHubApiError("A GitHub access token is required for repository enumeration.")
        response = await self._client.get(
            f"{GITHUB_API_BASE}{path}",
            headers={"Authorization": f"Bearer {access_token}", **_API_HEADERS},
            params=params or None,
        )
        if response.status_code in (401, 403):
            # The credential is dead or the installation was revoked/suspended.
            # PermissionError maps onto the reconnect path upstream.
            raise PermissionError(
                f"GitHub rejected the credential (status={response.status_code}): {response.text[:200]}"
            )
        if response.status_code == 404:
            raise KeyError(f"GitHub installation not found: {path}")
        if response.status_code >= 400:
            raise GitHubApiError(
                f"GitHub API error (status={response.status_code}): {response.text[:200]}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise GitHubApiError("GitHub API returned a non-object payload.")
        return payload
