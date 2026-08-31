"""Tests for Alpha's canonical GitHub repo checkout sync (agents.alpha_agent.repo_sync)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agents.alpha_agent.repo_sync import RepoWorktree


def _git_binary() -> str:
    binary = shutil.which("git")
    if not binary:
        pytest.skip("git is not available")
    return binary


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(args), cwd=str(cwd) if cwd else None, capture_output=True, text=True
    )


def _init_origin(tmp_path: Path) -> tuple[Path, Path, str]:
    """Create a bare origin with one commit; return (origin, seed_worktree, branch)."""
    binary = _git_binary()
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    _run(binary, "init", "--bare", str(origin))
    _run(binary, "clone", str(origin), str(seed))
    _run(binary, "config", "user.email", "t@t.com", cwd=seed)
    _run(binary, "config", "user.name", "T", cwd=seed)
    (seed / "f.txt").write_text("one", encoding="utf-8")
    _run(binary, "add", ".", cwd=seed)
    _run(binary, "commit", "-m", "first", cwd=seed)
    branch = _run(binary, "branch", "--show-current", cwd=seed).stdout.strip()
    _run(binary, "push", "-u", "origin", branch, cwd=seed)
    return origin, seed, branch


def _repo(origin: Path, branch: str, repo_id: str = "ghr_1") -> dict:
    return {
        "repo_row_id": repo_id,
        "full_name": "acme/site",
        "clone_url": str(origin),
        "default_branch": branch,
    }


def test_ensure_clones_into_canonical_path(tmp_path: Path) -> None:
    origin, _seed, branch = _init_origin(tmp_path)
    wt = RepoWorktree(tmp_path / "repos")

    checkout = wt.ensure(repo=_repo(origin, branch))

    assert checkout.action == "cloned"
    assert checkout.usable
    assert checkout.snapshot is not None
    assert checkout.snapshot.local_path == str(tmp_path / "repos" / "acme" / "site")
    assert checkout.snapshot.last_commit is not None
    assert (tmp_path / "repos" / "acme" / "site" / ".git").exists()


def test_ensure_fast_forwards_a_behind_checkout(tmp_path: Path) -> None:
    origin, seed, branch = _init_origin(tmp_path)
    wt = RepoWorktree(tmp_path / "repos")
    wt.ensure(repo=_repo(origin, branch))

    (seed / "g.txt").write_text("two", encoding="utf-8")
    _run("git", "add", ".", cwd=seed)
    _run("git", "commit", "-m", "second", cwd=seed)
    _run("git", "push", cwd=seed)

    checkout = wt.ensure(repo=_repo(origin, branch))

    assert checkout.action == "fast_forwarded"
    assert checkout.snapshot.behind == 0
    assert checkout.snapshot.ahead == 0


def test_ensure_reports_dirty_when_tracked_files_modified(tmp_path: Path) -> None:
    origin, _seed, branch = _init_origin(tmp_path)
    wt = RepoWorktree(tmp_path / "repos")
    wt.ensure(repo=_repo(origin, branch))

    path = wt.canonical_path("acme/site")
    (path / "f.txt").write_text("edited", encoding="utf-8")

    checkout = wt.ensure(repo=_repo(origin, branch))

    assert checkout.action == "dirty"
    assert checkout.snapshot.dirty is True


def test_ensure_reports_diverged_when_local_and_remote_commit(tmp_path: Path) -> None:
    origin, seed, branch = _init_origin(tmp_path)
    wt = RepoWorktree(tmp_path / "repos")
    wt.ensure(repo=_repo(origin, branch))
    path = wt.canonical_path("acme/site")

    (path / "local.txt").write_text("local", encoding="utf-8")
    _run("git", "add", ".", cwd=path)
    _run("git", "commit", "-m", "local", cwd=path)

    (seed / "i.txt").write_text("remote", encoding="utf-8")
    _run("git", "add", ".", cwd=seed)
    _run("git", "commit", "-m", "remote", cwd=seed)
    _run("git", "push", cwd=seed)

    checkout = wt.ensure(repo=_repo(origin, branch))

    assert checkout.action == "diverged"
    assert checkout.snapshot.ahead >= 1
    assert checkout.snapshot.behind >= 1


def test_ensure_refuses_path_conflict_outside_git(tmp_path: Path) -> None:
    _git_binary()
    wt = RepoWorktree(tmp_path / "repos")
    path = wt.canonical_path("acme/site")
    path.mkdir(parents=True, exist_ok=True)
    (path / "not-git.txt").write_text("x", encoding="utf-8")

    checkout = wt.ensure(
        repo={
            "repo_row_id": "ghr_1",
            "full_name": "acme/site",
            "clone_url": "https://github.com/acme/site.git",
            "default_branch": "main",
        }
    )

    assert checkout.action == "path_conflict"
    assert not checkout.usable


def test_ensure_skips_when_disabled(tmp_path: Path) -> None:
    _git_binary()
    wt = RepoWorktree(tmp_path / "repos", enabled=False)

    checkout = wt.ensure(
        repo={
            "repo_row_id": "ghr_1",
            "full_name": "acme/site",
            "clone_url": "https://github.com/acme/site.git",
            "default_branch": "main",
        }
    )

    assert checkout.action == "skipped"
