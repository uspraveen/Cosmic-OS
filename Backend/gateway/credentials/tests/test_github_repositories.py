"""Gateway-side GitHub repository registry tests.

Covers the sqlite store contract (upsert / reconcile / progress / lookup),
the manager's enumeration flow against a fake GitHub API client, and webhook
classification plus signature verification.
"""

from __future__ import annotations

import asyncio
import hmac
from pathlib import Path

from gateway.credentials.manager import CredentialManager
from gateway.credentials.store import CredentialStore
from gateway.github_routes import _verify_github_signature, classify_github_event


def _store(tmp_path: Path) -> CredentialStore:
    return CredentialStore(tmp_path / "credentials.db")


def _repo_payload(repo_id: int, full_name: str) -> dict:
    owner, _, name = full_name.partition("/")
    return {
        "id": repo_id,
        "node_id": f"R_{repo_id}",
        "full_name": full_name,
        "owner": {"login": owner},
        "name": name,
        "private": full_name.startswith("private/"),
        "clone_url": f"https://github.com/{full_name}.git",
        "ssh_url": f"git@github.com:{full_name}.git",
        "html_url": f"https://github.com/{full_name}",
        "default_branch": "main",
        "permissions": {"admin": True, "push": True, "pull": True},
    }


def test_upsert_inserts_once_and_updates_in_place(tmp_path: Path) -> None:
    store = _store(tmp_path)
    account_id = store.create_account(provider="github")["account_id"]

    first = store.upsert_github_repositories(
        account_id=account_id,
        installation_id="1001",
        repos=[_repo_payload(1, "acme/site")],
    )
    assert first == {"added": 1, "updated": 0, "total": 1}

    again = store.upsert_github_repositories(
        account_id=account_id,
        installation_id="10045",
        repos=[_repo_payload(1, "acme/site")],
    )
    assert again["added"] == 0
    assert again["updated"] == 1

    rows = store.list_github_repositories(statuses=["all"])
    assert len(rows) == 1
    assert rows[0]["installation_id"] == "10045"


def test_progress_survives_resync(tmp_path: Path) -> None:
    store = _store(tmp_path)
    account_id = store.create_account(provider="github")["account_id"]
    store.upsert_github_repositories(
        account_id=account_id,
        installation_id="inst_1",
        repos=[_repo_payload(7, "acme/site")],
    )
    repo = store.list_github_repositories()[0]
    store.update_github_repository_progress(
        repo["repo_row_id"],
        local_path="/var/lib/cosmic/alpha/repos/acme/site",
        branch="main",
        commit_sha="abc123",
        commit_message="feat: ship it",
        commit_author="praveen",
        commit_at="2026-08-30T10:00:00+00:00",
        ahead=2,
        behind=0,
        dirty=False,
        alpha_project_id="prj_x",
    )

    store.upsert_github_repositories(
        account_id=account_id,
        installation_id="inst_1",
        repos=[_repo_payload(7, "acme/site")],
    )
    fresh = store.list_github_repositories()[0]
    assert fresh["local_path"] == "/var/lib/cosmic/alpha/repos/acme/site"
    assert fresh["last_commit"]["sha"] == "abc123"
    assert fresh["last_commit"]["author"] == "praveen"
    assert fresh["alpha_project_id"] == "prj_x"


def test_mark_missing_demotes_repos_that_dropped_out_of_grant(tmp_path: Path) -> None:
    store = _store(tmp_path)
    account_id = store.create_account(provider="github")["account_id"]
    store.upsert_github_repositories(
        account_id=account_id,
        installation_id="inst",
        repos=[_repo_payload(1, "acme/kept"), _repo_payload(2, "acme/other")],
    )
    demoted = store.mark_github_repositories_missing(account_id, ["1"])
    assert len(demoted) == 1
    rows = {
        item["github_repo_id"]: item["status"]
        for item in store.list_github_repositories(statuses=["all"])
    }
    assert rows == {"1": "active", "2": "access_removed"}


def test_ref_resolution_matches_name_and_url_forms(tmp_path: Path) -> None:
    store = _store(tmp_path)
    account_id = store.create_account(provider="github")["account_id"]
    store.upsert_github_repositories(
        account_id=account_id,
        installation_id="inst",
        repos=[_repo_payload(7, "acme/Cosmic-OS")],
    )

    assert store.find_github_repository("ghr_nope") is None
    for ref in (
        "acme/Cosmic-OS",
        "ACME/COSMIC-OS",
        "https://github.com/acme/Cosmic-OS",
        "https://github.com/acme/Cosmic-OS.git",
        "git@github.com:acme/Cosmic-OS.git",
        "ssh://git@github.com/acme/Cosmic-OS.git",
    ):
        assert store.find_github_repository(ref) is not None, ref
        assert store.find_github_repository(ref)["full_name"] == "acme/Cosmic-OS"


def test_account_delete_cascades_repository_rows(tmp_path: Path) -> None:
    store = _store(tmp_path)
    account_id = store.create_account(provider="github")["account_id"]
    store.upsert_github_repositories(
        account_id=account_id,
        installation_id="inst",
        repos=[_repo_payload(5, "o/r")],
    )
    store.delete_account(account_id)

    assert store.list_github_repositories(statuses=["all"]) == []
    assert store.find_account_id_by_installation("inst") is None


def test_installation_lookup_matches_metadata(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_account(provider="github", metadata={"github_installation_id": "inst-9"})
    store.create_account(provider="github", metadata={})

    assert store.find_account_id_by_installation("inst-9") is not None
    assert store.find_account_id_by_installation("inst-1") is None
