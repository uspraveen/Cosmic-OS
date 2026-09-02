"""Alpha's checkouts must commit as the connected user, not the VM default.

The gateway tells Alpha who each repository belongs to (``git_author_name`` /
``git_author_email`` from the connected GitHub account); ``ensure`` applies it
as repo-local config so every writer in that checkout — Alpha's shell, the
Cursor/OpenCode runners — commits and pushes as the user.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from agents.alpha_agent.repo_sync import RepoWorktree


def _git(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        check=True,
        capture_output=True,
    )


def _seed_origin(origin: Path) -> None:
    """A bare origin with one commit, authored by a throwaway identity."""
    _git("init", "--bare", str(origin))
    seed = origin.parent / "seed"
    seed.mkdir()
    _git("init", str(seed))
    (seed / "README.md").write_text("seed\n")
    _git("add", ".", cwd=seed)
    _git("-c", "user.name=Seeder", "-c", "user.email=seed@example.com",
         "commit", "-m", "seed", cwd=seed)
    _git("branch", "-M", "main", cwd=seed)
    _git("push", str(origin), "main", cwd=seed)
    # A fresh `git init --bare` aims HEAD at the local default branch; aim it
    # at main so clones check out the seeded commit.
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=origin)


def _config(path: Path, key: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), "config", key],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def test_ensure_configures_the_connected_user_identity(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    _seed_origin(origin)
    worktree = RepoWorktree(repos_root=tmp_path / "worktrees", enabled=True)

    checkout = worktree.ensure(
        repo={
            "repo_row_id": "ghr_1",
            "full_name": "uspraveen/uspraveen.github.io",
            "clone_url": str(origin),
            "default_branch": "main",
            "git_author_name": "Praveen Raj Uma Maheswari Shyam Sundar",
            "git_author_email": "12345+uspraveen@users.noreply.github.com",
            "git_author_login": "uspraveen",
        }
    )

    assert checkout.error in (None, ""), checkout.error
    assert checkout.local_path
    assert (
        _config(Path(checkout.local_path), "user.name")
        == "Praveen Raj Uma Maheswari Shyam Sundar"
    )
    assert (
        _config(Path(checkout.local_path), "user.email")
        == "12345+uspraveen@users.noreply.github.com"
    )
    # Pins pushes to this account when several are connected.
    assert _config(Path(checkout.local_path), "credential.username") == "uspraveen"


def test_ensure_without_identity_leaves_config_alone(tmp_path: Path) -> None:
    """No identity in the payload must not invent one."""
    origin = tmp_path / "origin.git"
    _seed_origin(origin)
    worktree = RepoWorktree(repos_root=tmp_path / "worktrees", enabled=True)

    checkout = worktree.ensure(
        repo={
            "repo_row_id": "ghr_2",
            "full_name": "uspraveen/Cosmic-OS",
            "clone_url": str(origin),
            "default_branch": "main",
        }
    )

    assert checkout.error in (None, ""), checkout.error
    # Repo-local only: the machine's global gitconfig is irrelevant here.
    completed = subprocess.run(
        ["git", "-C", str(checkout.local_path), "config", "--local", "user.name"],
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0 or completed.stdout.strip() == ""
