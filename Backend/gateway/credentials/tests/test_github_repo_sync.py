"""Manager enumeration + webhook classification/signature tests.

Complements test_github_repositories.py (store contract). The manager is
exercised against a fake GitHub API client; webhook events are classified and
the shared-secret scheme is verified.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from pathlib import Path

from gateway.credentials.manager import CredentialManager
from gateway.credentials.store import CredentialStore
from gateway.github_routes import _verify_github_signature, classify_github_event


class _FakeGitHubClient:
    def __init__(self, repositories=None) -> None:
        self.repo_calls: list[str] = []
        self._repositories = list(repositories or [])

    async def list_user_installations(self, token: str) -> list[dict]:
        assert token
        return [{"id": 555, "app_slug": "cosmic-dev"}]

    async def list_installation_repositories(self, token, installation_id, *, max_pages=10):
        del token, max_pages
        self.repo_calls.append(str(installation_id))
        return list(self._repositories)


def _seed_credential(store: CredentialStore, account_id: str, *, installation_id: str) -> None:
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


def test_manager_sync_enumerates_installation_repositories(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "credentials.db")
    account_id = store.create_account(provider="github")["account_id"]
    _seed_credential(store, account_id, installation_id="42")

    fake = _FakeGitHubClient(
        repositories=[
            {
                "id": 1,
                "full_name": "acme/site",
                "clone_url": "https://github.com/acme/site.git",
                "default_branch": "main",
                "permissions": {"push": True},
            },
            {"id": 2, "full_name": "acme/old", "clone_url": "https://github.com/acme/old.git"},
        ]
    )
    mgr = CredentialManager(store=store, github_api_client=fake)
    summary = asyncio.run(mgr.sync_github_repositories(account_id))

    assert summary["synced"] is True
    assert summary["installation_id"] == "42"
    assert summary["repo_count"] == 2
    assert summary["added"] == 2
    assert fake.repo_calls == ["42"]
    rows = {item["full_name"]: item["can_push"] for item in mgr.list_github_repositories()}
    assert rows["acme/site"] is True
    assert rows["acme/old"] is False


def test_sync_without_active_account_reports_reason(tmp_path: Path) -> None:
    mgr = CredentialManager(CredentialStore(tmp_path / "credentials.db"))
    summary = asyncio.run(mgr.sync_github_repositories())

    assert summary["synced"] is False
    assert summary["reason"] == "no_active_github_account"


def test_sync_reconciles_repos_removed_from_grant(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "credentials.db")
    account_id = store.create_account(provider="github")["account_id"]
    _seed_credential(store, account_id, installation_id="77")
    store.upsert_github_repositories(
        account_id=account_id,
        installation_id="77",
        repos=[
            {"id": 2, "full_name": "acme/still-allowed", "clone_url": "https://github.com/acme/still-allowed.git"},
            {"id": 3, "full_name": "acme/dropped", "clone_url": "https://github.com/acme/dropped.git"},
        ],
    )

    class ShrinkingClient:
        async def list_user_installations(self, token: str) -> list[dict]:
            return [{"id": 1, "app_slug": "cosmic-dev"}]

        async def list_installation_repositories(self, token, installation_id, *, max_pages=10):
            del token, max_pages
            return [{"id": 2, "full_name": "acme/still-allowed"}]

    mgr = CredentialManager(store=store, github_api_client=ShrinkingClient())
    summary = asyncio.run(mgr.sync_github_repositories(account_id))

    assert summary["synced"] is True
    assert summary["repo_count"] == 1
    rows = {
        item["github_repo_id"]: item["status"]
        for item in mgr.list_github_repositories(statuses=["all"])
    }
    assert rows == {"2": "active", "3": "access_removed"}


def test_webhook_classification_routes_smallest_safe_action() -> None:
    assert classify_github_event("ping", {"zen": "ok"})["action"] == "ignore"

    removed = classify_github_event(
        "installation_repositories",
        {
            "action": "removed",
            "installation": {"id": 7},
            "repositories_removed": [{"id": 3, "full_name": "o/gone"}],
        },
    )
    assert removed["action"] == "resync"
    assert removed["repositories_removed"][0]["full_name"] == "o/gone"

    resync = classify_github_event("installation", {"action": "created", "installation": {"id": 1}})
    assert resync["action"] == "resync"
    assert classify_github_event(
        "installation", {"action": "deleted", "installation": {"id": 1}}
    )["action"] == "revoke"
    assert classify_github_event(
        "repository", {"action": "renamed", "repository": {"id": 4}}
    )["action"] == "resync_all"
    assert classify_github_event("push", {"action": "published"})["action"] == "ignore"


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def test_signature_verification_matches_github_scheme() -> None:
    body = b'{"zen": "Keep it simple."}'
    good = _sign("secret", body)

    assert _verify_github_signature("secret", body, good)
    assert not _verify_github_signature("secret", b"tampered", good)
    assert not _verify_github_signature("secret", body, "sha256=deadbeef")
    assert _verify_github_signature("", b"{}", "")
