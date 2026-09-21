"""Manager enumeration + webhook classification/signature tests.

Complements test_github_repositories.py (store contract). The manager is
exercised against a fake GitHub API client; webhook events are classified and
the shared-secret scheme is verified.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
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


class _HealthGitHubClient:
    """Fake API client whose ``GET /user`` behavior is scripted per test."""

    def __init__(self, *, user: dict | None = None, error: Exception | None = None) -> None:
        self._user = dict(user or {"login": "tester"})
        self._error = error

    async def get_authenticated_user(self, token: str) -> dict:
        assert token
        if self._error is not None:
            raise self._error
        return dict(self._user)


def test_probe_reports_healthy_and_stamps_login(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "credentials.db")
    account_id = store.create_account(provider="github")["account_id"]
    _seed_credential(store, account_id, installation_id="42")
    mgr = CredentialManager(
        store=store, github_api_client=_HealthGitHubClient(user={"login": "uspraveen"})
    )

    result = asyncio.run(mgr.probe_github_account_health(account_id))

    assert result["status"] == "healthy"
    assert result["needs_reconnect"] is False
    assert result["login"] == "uspraveen"
    account = store.get_account(account_id)
    assert account["status"] == "active"
    assert account["_metadata"]["github_login"] == "uspraveen"


def test_probe_reports_reauth_when_github_rejects_credential(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "credentials.db")
    account_id = store.create_account(provider="github")["account_id"]
    _seed_credential(store, account_id, installation_id="42")
    mgr = CredentialManager(
        store=store,
        github_api_client=_HealthGitHubClient(
            error=PermissionError("GitHub rejected the credential (status=401)")
        ),
    )

    result = asyncio.run(mgr.probe_github_account_health(account_id))

    assert result["status"] == "reauth_required"
    assert result["needs_reconnect"] is True
    assert "401" in result["error"]
    assert store.get_account(account_id)["status"] == "needs_auth"


def test_probe_reports_provider_error_without_condemning_account(tmp_path: Path) -> None:
    """A GitHub outage is not a revoked grant; the account must stay active."""
    store = CredentialStore(tmp_path / "credentials.db")
    account_id = store.create_account(provider="github")["account_id"]
    _seed_credential(store, account_id, installation_id="42")
    mgr = CredentialManager(
        store=store,
        github_api_client=_HealthGitHubClient(error=RuntimeError("GitHub API error (status=500)")),
    )

    result = asyncio.run(mgr.probe_github_account_health(account_id))

    assert result["status"] == "provider_error"
    assert result["needs_reconnect"] is False
    assert store.get_account(account_id)["status"] == "active"


def test_probe_unknown_account_is_not_a_crash(tmp_path: Path) -> None:
    mgr = CredentialManager(CredentialStore(tmp_path / "credentials.db"))

    result = asyncio.run(mgr.probe_github_account_health("missing"))

    assert result["status"] == "unknown"
    assert result["error"]


def test_sync_captures_installation_login_and_permissions(tmp_path: Path) -> None:
    """The settings panel's connected-as / grant chips come from this metadata."""

    class MetadataClient:
        async def list_user_installations(self, token: str) -> list[dict]:
            return [
                {
                    "id": 555,
                    "app_slug": "cosmic-dev",
                    "account": {"login": "uspraveen"},
                    "permissions": {
                        "contents": "write",
                        "pull_requests": "write",
                        "metadata": "read",
                    },
                }
            ]

        async def list_installation_repositories(self, token, installation_id, *, max_pages=10):
            del token, max_pages
            return []

    store = CredentialStore(tmp_path / "credentials.db")
    account_id = store.create_account(provider="github")["account_id"]
    _seed_credential(store, account_id, installation_id="555")
    mgr = CredentialManager(store=store, github_api_client=MetadataClient())

    summary = asyncio.run(mgr.sync_github_repositories(account_id))

    assert summary["synced"] is True
    metadata = store.get_account(account_id)["_metadata"]
    assert metadata["github_login"] == "uspraveen"
    assert metadata["github_permissions"]["contents"] == "write"
    assert metadata["github_permissions"]["pull_requests"] == "write"


def test_sync_survives_a_metadata_lookup_failure(tmp_path: Path) -> None:
    """The grant chips are cosmetic; enumeration must not depend on them."""

    class BrokenMetadataClient:
        async def list_user_installations(self, token: str) -> list[dict]:
            raise RuntimeError("GitHub API error (status=502)")

        async def list_installation_repositories(self, token, installation_id, *, max_pages=10):
            del token, max_pages
            return [
                {"id": 1, "full_name": "acme/site", "clone_url": "https://github.com/acme/site.git"}
            ]

    store = CredentialStore(tmp_path / "credentials.db")
    account_id = store.create_account(provider="github")["account_id"]
    _seed_credential(store, account_id, installation_id="42")
    mgr = CredentialManager(store=store, github_api_client=BrokenMetadataClient())

    summary = asyncio.run(mgr.sync_github_repositories(account_id))

    assert summary["synced"] is True
    assert summary["repo_count"] == 1


# ── Webhook-free registry freshness ──────────────────────────────────────────


def _age_registry(store: CredentialStore, account_id: str, *, seconds_ago: float) -> None:
    store.update_account(
        account_id,
        metadata_patch={"github_repos_synced_at": time.time() - seconds_ago},
    )


def test_never_synced_registry_counts_as_stale(tmp_path: Path) -> None:
    """A fresh connect with an empty registry must refresh on first read."""
    store = CredentialStore(tmp_path / "credentials.db")
    account_id = store.create_account(provider="github")["account_id"]
    _seed_credential(store, account_id, installation_id="42")
    fake = _FakeGitHubClient(repositories=[])
    mgr = CredentialManager(store=store, github_api_client=fake)

    asyncio.run(mgr.ensure_github_registry_fresh(blocking=True))

    assert fake.repo_calls == ["42"]


def test_fresh_registry_skips_revalidation(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "credentials.db")
    account_id = store.create_account(provider="github")["account_id"]
    _seed_credential(store, account_id, installation_id="42")
    fake = _FakeGitHubClient(repositories=[])
    mgr = CredentialManager(store=store, github_api_client=fake)

    asyncio.run(mgr.sync_github_repositories(account_id))
    asyncio.run(mgr.ensure_github_registry_fresh(blocking=True))

    assert fake.repo_calls == ["42"]


def test_stale_registry_refreshes_on_read(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "credentials.db")
    account_id = store.create_account(provider="github")["account_id"]
    _seed_credential(store, account_id, installation_id="42")
    fake = _FakeGitHubClient(repositories=[])
    mgr = CredentialManager(store=store, github_api_client=fake)

    asyncio.run(mgr.sync_github_repositories(account_id))
    _age_registry(store, account_id, seconds_ago=3600)
    asyncio.run(mgr.ensure_github_registry_fresh(blocking=True))

    assert fake.repo_calls == ["42", "42"]


def test_min_refresh_interval_guards_against_retry_storm(tmp_path: Path) -> None:
    """A persistently failing sync must not retry on every single read."""
    store = CredentialStore(tmp_path / "credentials.db")
    account_id = store.create_account(provider="github")["account_id"]
    _seed_credential(store, account_id, installation_id="42")

    class FailingClient:
        async def list_user_installations(self, token: str) -> list[dict]:
            return [{"id": 42, "app_slug": "cosmic-dev"}]

        async def list_installation_repositories(self, token, installation_id, *, max_pages=10):
            del token, max_pages
            raise RuntimeError("GitHub API error (status=502)")

    fake_calls: list[str] = []

    class CountingClient(FailingClient):
        async def list_installation_repositories(self, token, installation_id, *, max_pages=10):
            del token, max_pages
            fake_calls.append(str(installation_id))
            raise RuntimeError("GitHub API error (status=502)")

    mgr = CredentialManager(store=store, github_api_client=CountingClient())
    _age_registry(store, account_id, seconds_ago=3600)

    asyncio.run(mgr.ensure_github_registry_fresh(blocking=True))
    asyncio.run(mgr.ensure_github_registry_fresh(blocking=True))

    assert len(fake_calls) == 1
    # A GitHub outage is not a revoked grant; the account stays active.
    assert store.get_account(account_id)["status"] == "active"


def test_sync_permission_error_condemns_account(tmp_path: Path) -> None:
    """GitHub rejecting the token is definitive: the account needs reconnect."""
    store = CredentialStore(tmp_path / "credentials.db")
    account_id = store.create_account(provider="github")["account_id"]
    _seed_credential(store, account_id, installation_id="42")

    class RejectedClient:
        async def list_user_installations(self, token: str) -> list[dict]:
            return [{"id": 42, "app_slug": "cosmic-dev"}]

        async def list_installation_repositories(self, token, installation_id, *, max_pages=10):
            del token, max_pages
            raise PermissionError("GitHub rejected the credential (status=401)")

    mgr = CredentialManager(store=store, github_api_client=RejectedClient())

    summary = asyncio.run(mgr.sync_github_repositories(account_id))

    assert summary["synced"] is False
    assert "401" in str(summary.get("error") or "")
    account = store.get_account(account_id)
    assert account["status"] == "needs_auth"
    assert "401" in str(account["_metadata"].get("last_auth_error") or "")


# ── Commit identity: pushes land as the connected user ──────────────────────


def test_git_identity_uses_noreply_email_and_display_name(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "credentials.db")
    account_id = store.create_account(provider="github", display_name="Praveen Raj")["account_id"]
    store.update_account(
        account_id,
        metadata_patch={"github_login": "uspraveen", "github_user_id": "12345"},
    )
    mgr = CredentialManager(store=store)

    identity = mgr.github_git_identity(account_id)

    assert identity == {
        "name": "Praveen Raj",
        "email": "12345+uspraveen@users.noreply.github.com",
        "login": "uspraveen",
    }


def test_git_identity_falls_back_to_login(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "credentials.db")
    account_id = store.create_account(provider="github")["account_id"]
    store.update_account(account_id, metadata_patch={"github_login": "uspraveen"})
    mgr = CredentialManager(store=store)

    identity = mgr.github_git_identity(account_id)

    assert identity == {
        "name": "uspraveen",
        "email": "uspraveen@users.noreply.github.com",
        "login": "uspraveen",
    }


def test_git_identity_is_none_without_a_login(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "credentials.db")
    account_id = store.create_account(provider="github")["account_id"]
    mgr = CredentialManager(store=store)

    assert mgr.github_git_identity(account_id) is None
    assert mgr.github_git_identity("missing") is None


def test_probe_stamps_the_numeric_user_id(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "credentials.db")
    account_id = store.create_account(provider="github")["account_id"]
    _seed_credential(store, account_id, installation_id="42")
    mgr = CredentialManager(
        store=store,
        github_api_client=_HealthGitHubClient(user={"login": "uspraveen", "id": 12345}),
    )

    asyncio.run(mgr.probe_github_account_health(account_id))

    metadata = store.get_account(account_id)["_metadata"]
    assert metadata["github_login"] == "uspraveen"
    assert metadata["github_user_id"] == "12345"


def test_public_repo_payload_carries_identity_when_given() -> None:
    from gateway.credentials.routes import _public_github_repo

    repo = {"repo_row_id": "ghr_1", "full_name": "uspraveen/site", "status": "active"}

    with_identity = _public_github_repo(
        repo, {"name": "Praveen Raj", "email": "1+uspraveen@users.noreply.github.com"}
    )
    assert with_identity["git_author_name"] == "Praveen Raj"
    assert with_identity["git_author_email"] == "1+uspraveen@users.noreply.github.com"

    without_identity = _public_github_repo(repo)
    assert "git_author_name" not in without_identity
    assert "git_author_email" not in without_identity


def test_hint_matching_matches_by_github_login(tmp_path: Path) -> None:
    """A checkout pinned via credential.username must resolve that account."""
    store = CredentialStore(tmp_path / "credentials.db")
    personal = store.create_account(provider="github", display_name="Personal")["account_id"]
    work = store.create_account(provider="github", display_name="Work")["account_id"]
    store.update_account(personal, metadata_patch={"github_login": "uspraveen"})
    store.update_account(work, metadata_patch={"github_login": "uspraveen-work"})
    for account_id in (personal, work):
        store.store_credential(
            account_id=account_id,
            granted_scopes=[],
            access_token=f"token-{account_id}",
            refresh_token="refresh",
            expires_at_ts=9999999999.0,
        )
    store.update_account(work, is_primary=True)
    mgr = CredentialManager(store=store)

    matches = mgr.account_hint_candidates(provider="github", account_hint="uspraveen-work")

    assert [item["account_id"] for item in matches] == [work]


def test_git_identity_carries_the_login_for_push_pinning(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "credentials.db")
    account_id = store.create_account(provider="github", display_name="Praveen Raj")["account_id"]
    store.update_account(
        account_id,
        metadata_patch={"github_login": "uspraveen", "github_user_id": "12345"},
    )
    mgr = CredentialManager(store=store)

    identity = mgr.github_git_identity(account_id)

    assert identity["login"] == "uspraveen"


def test_public_repo_payload_carries_login_for_pinning() -> None:
    from gateway.credentials.routes import _public_github_repo

    payload = _public_github_repo(
        {"repo_row_id": "ghr_1", "full_name": "uspraveen/site"},
        {"name": "Praveen Raj", "email": "1+uspraveen@users.noreply.github.com", "login": "uspraveen"},
    )
    assert payload["git_author_login"] == "uspraveen"


# ── Stale installation recovery + disconnect honesty ────────────────────────


def test_sync_recovers_when_stored_installation_was_reinstalled(tmp_path: Path) -> None:
    """An uninstall + reinstall mints a new installation id; the stale stored
    one must be forgotten and rediscovered, not failed on forever."""
    store = CredentialStore(tmp_path / "credentials.db")
    account_id = store.create_account(provider="github")["account_id"]
    _seed_credential(store, account_id, installation_id="42")

    class ReinstalledClient:
        def __init__(self) -> None:
            self.repo_calls: list[str] = []

        async def list_user_installations(self, token: str) -> list[dict]:
            del token
            # The reinstall minted a new installation id.
            return [{"id": 99, "app_slug": "cosmic-dev"}]

        async def list_installation_repositories(self, token, installation_id, *, max_pages=10):
            del token, max_pages
            self.repo_calls.append(str(installation_id))
            if str(installation_id) == "42":
                raise KeyError("GitHub installation not found: /user/installations/42/repositories")
            return [
                {
                    "id": 1,
                    "full_name": "acme/site",
                    "clone_url": "https://github.com/acme/site.git",
                }
            ]

    fake = ReinstalledClient()
    mgr = CredentialManager(store=store, github_api_client=fake)

    summary = asyncio.run(mgr.sync_github_repositories(account_id))

    assert summary["synced"] is True
    assert summary["installation_id"] == "99"
    assert fake.repo_calls == ["42", "99"]
    metadata = store.get_account(account_id)["_metadata"]
    assert metadata["github_installation_id"] == "99"
    assert [row["full_name"] for row in mgr.list_github_repositories()] == ["acme/site"]


def test_sync_still_reports_missing_when_no_installation_exists(tmp_path: Path) -> None:
    """Rediscovery finding nothing keeps the honest installation_missing
    result — and the stale id is not left behind to poison the next sync."""
    store = CredentialStore(tmp_path / "credentials.db")
    account_id = store.create_account(provider="github")["account_id"]
    _seed_credential(store, account_id, installation_id="42")

    class UninstalledClient:
        async def list_user_installations(self, token: str) -> list[dict]:
            del token
            return []  # the app was uninstalled from the GitHub side

        async def list_installation_repositories(self, token, installation_id, *, max_pages=10):
            del token, max_pages
            raise KeyError("GitHub installation not found: /user/installations/42/repositories")

    mgr = CredentialManager(store=store, github_api_client=UninstalledClient())

    summary = asyncio.run(mgr.sync_github_repositories(account_id))

    assert summary["synced"] is False
    assert summary["reason"] == "installation_missing"
    assert store.get_account(account_id)["_metadata"].get("github_installation_id") == ""


def test_github_connect_gate_treats_revoked_as_disconnected() -> None:
    """Only an explicit user disconnect re-opens the install page with the
    repository picker; a needs_auth reconnect stays on the light path."""
    from gateway.credentials.routes import _github_already_connected

    assert _github_already_connected([]) is False
    assert _github_already_connected([{"status": "active"}]) is True
    assert _github_already_connected([{"status": "needs_auth"}]) is True
    assert _github_already_connected([{"status": "revoked"}]) is False
    assert _github_already_connected([{"status": "revoked"}, {"status": "active"}]) is True


def test_settings_account_list_hides_only_revoked_github_accounts() -> None:
    from gateway.credentials.routes import _visible_settings_accounts

    accounts = [
        {"account_id": "a1", "status": "active"},
        {"account_id": "a2", "status": "needs_auth"},
        {"account_id": "a3", "status": "revoked"},
    ]

    github_visible = _visible_settings_accounts("github", accounts)
    assert [item["account_id"] for item in github_visible] == ["a1", "a2"]

    # Google's panel keeps rendering removed accounts (per-card actions).
    assert _visible_settings_accounts("google", accounts) == accounts


def test_disconnect_revokes_repository_rows_and_reconnect_restores(
    tmp_path: Path, monkeypatch
) -> None:
    """The full deleted-then-reconnected journey: disconnect flips the account
    to revoked and its repo rows with it; reconnecting and syncing marks the
    granted rows active again."""
    from gateway.credentials import manager as manager_module
    from gateway.credentials.routes import _github_already_connected

    class _NoopAdapter:
        provider = "github"

        async def revoke_token(self, token: str, client_id: str, client_secret: str) -> bool:
            del token, client_id, client_secret
            return True

    monkeypatch.setattr(
        manager_module, "get_provider_adapter", lambda _provider: _NoopAdapter()
    )

    store = CredentialStore(tmp_path / "credentials.db")
    account_id = store.create_account(provider="github")["account_id"]
    _seed_credential(store, account_id, installation_id="42")
    store.upsert_github_repositories(
        account_id=account_id,
        installation_id="42",
        repos=[
            {
                "id": 1,
                "full_name": "acme/site",
                "clone_url": "https://github.com/acme/site.git",
            }
        ],
    )
    fake = _FakeGitHubClient(
        repositories=[
            {
                "id": 1,
                "full_name": "acme/site",
                "clone_url": "https://github.com/acme/site.git",
            }
        ]
    )
    mgr = CredentialManager(store=store, github_api_client=fake)

    account = asyncio.run(mgr.disconnect_account(account_id))
    assert account["status"] == "revoked"
    rows = {
        item["github_repo_id"]: item["status"]
        for item in mgr.list_github_repositories(statuses=["all"])
    }
    assert rows == {"1": "revoked"}
    # A deleted account no longer short-circuits the connect flow: the next
    # connect goes back through the install page and its repo picker.
    assert _github_already_connected(mgr.list_accounts("github")) is False

    # Reconnect: the callback reactivates the row, stores a fresh credential
    # (the disconnect revoked the old one), then the sync re-grants.
    store.update_account(account_id, status="active")
    store.store_credential(
        account_id=account_id,
        granted_scopes=[],
        access_token="reconnected-token",
        refresh_token="refresh-token-2",
        expires_at_ts=9999999999.0,
    )
    summary = asyncio.run(mgr.sync_github_repositories(account_id))
    assert summary["synced"] is True
    rows = {
        item["github_repo_id"]: item["status"]
        for item in mgr.list_github_repositories(statuses=["all"])
    }
    assert rows == {"1": "active"}
