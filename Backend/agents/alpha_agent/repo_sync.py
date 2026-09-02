"""Canonical GitHub repository checkouts for the Alpha agent.

The Gateway's ``github_repositories`` table is the authority for *which*
repositories the user granted and where they come from. This module is the
authority for *how* those repositories exist on disk: every connected repo is
cloned to one stable path, ``<repos_root>/<owner>/<name>``, so the orchestrator
and Alpha always agree on a single checkout per repo, independent of the
ephemeral ``workspaces/prj_*`` project sandboxes.

``ensure`` creates the checkout (clone) or brings it up to date (fetch +
fast-forward only when clean and strictly behind), never silently rewriting
history or clobbering local work. Both ``ensure`` and ``snapshot`` report the
git state — branch, ahead/behind, dirty/untracked, and the last commit — in a
shape the agent forwards to the Gateway so the orchestrator can read a repo's
last progress without running git itself.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shared.cursor_cli import apply_git_credentials

logger = logging.getLogger(__name__)

_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")

_UP_TO_DATE = "up_to_date"
_CLONED = "cloned"
_FAST_FORWARDED = "fast_forwarded"
_DIVERGED = "diverged"
_DIRTY = "dirty"
_PATH_CONFLICT = "path_conflict"
_FAILED = "failed"
_SKIPPED = "skipped"

_OK_ACTIONS = {_CLONED, _FAST_FORWARDED, _UP_TO_DATE, _DIVERGED, _DIRTY}


@dataclass(frozen=True)
class RepoSnapshot:
    """Read-only description of a checkout's current git state."""

    local_path: str
    branch: str | None
    behind: int
    ahead: int
    dirty: bool
    untracked: int
    last_commit: dict | None
    error: str | None = None


@dataclass(frozen=True)
class RepoCheckout:
    """Outcome of ``ensure`` for one repository."""

    repo_row_id: str
    full_name: str
    local_path: str
    action: str
    snapshot: RepoSnapshot | None
    error: str | None = None

    @property
    def usable(self) -> bool:
        return self.action in _OK_ACTIONS and self.snapshot is not None


class RepoWorktree:
    """Clone and refresh connected repositories under one canonical root."""

    def __init__(
        self,
        repos_root: str | Path,
        *,
        timeout_sec: float = 600.0,
        enabled: bool = True,
        git_binary: str = "git",
    ) -> None:
        self.repos_root = Path(repos_root).expanduser()
        self.timeout_sec = max(30.0, float(timeout_sec))
        self.enabled = bool(enabled)
        self.git_binary = git_binary

    def canonical_path(self, full_name: str) -> Path:
        owner, _, name = full_name.partition("/")
        owner = owner or "?"
        name = name or full_name
        return self.repos_root / self._validate_segment(owner, "owner") / self._validate_segment(name, "name")

    @staticmethod
    def _validate_segment(value: str, label: str) -> str:
        normalized = str(value or "").strip()
        if not normalized or not _SEGMENT_PATTERN.match(normalized):
            raise ValueError(f"Invalid repository {label}: {value!r}")
        return normalized

    def ensure(self, *, repo: dict[str, Any]) -> RepoCheckout:
        repo_row_id = str(repo.get("repo_row_id") or "").strip()
        full_name = str(repo.get("full_name") or "").strip()
        clone_url = str(repo.get("clone_url") or "").strip()
        default_branch = str(repo.get("default_branch") or "main").strip() or "main"

        if not self.enabled:
            return RepoCheckout(repo_row_id, full_name, "", _SKIPPED, None, "repo sync disabled")

        if not full_name:
            return RepoCheckout(repo_row_id, "", "", _FAILED, None, "missing full_name")

        try:
            path = self.canonical_path(full_name)
        except ValueError as exc:
            return RepoCheckout(repo_row_id, full_name, "", _FAILED, None, str(exc))

        if not clone_url:
            return RepoCheckout(repo_row_id, full_name, str(path), _FAILED, None, "missing clone_url")

        if path.exists() and not (path / ".git").exists():
            return RepoCheckout(
                repo_row_id,
                full_name,
                str(path),
                _PATH_CONFLICT,
                None,
                f"Path already exists and is not a git repository: {path}",
            )

        author_name = str(repo.get("git_author_name") or "").strip()
        author_email = str(repo.get("git_author_email") or "").strip()
        author_login = str(repo.get("git_author_login") or "").strip()

        try:
            if not path.exists():
                self._run(["clone", "--origin", "origin", clone_url, str(path)])
                action = _CLONED
            else:
                action = self._refresh(path, default_branch)
            # Commits in this checkout must land as the connected user, never
            # as the VM's default identity. Repo-local config covers every
            # writer here — Alpha's shell and the Cursor/OpenCode runners —
            # and is re-asserted on every ensure so it cannot drift.
            if author_name:
                self._run(["config", "user.name", author_name], cwd=path)
            if author_email:
                self._run(["config", "user.email", author_email], cwd=path)
            if author_login:
                # Pin pushes to the owning account: git offers this username
                # to credential helpers, and the helper resolves that exact
                # account's token instead of a primary-account fallback.
                self._run(["config", "credential.username", author_login], cwd=path)
            snapshot = self.snapshot(path)
            return RepoCheckout(repo_row_id, full_name, str(path), action, snapshot, snapshot.error)
        except Exception as exc:
            logger.exception(
                "alpha.repo_sync.ensure_failed repo=%s error=%s", full_name, exc
            )
            return RepoCheckout(
                repo_row_id,
                full_name,
                str(path),
                _FAILED,
                None,
                str(exc)[:500],
            )

    def snapshot(self, path: str | Path) -> RepoSnapshot:
        resolved = Path(path).expanduser()
        try:
            if not (resolved / ".git").exists():
                return RepoSnapshot(str(resolved), None, 0, 0, False, 0, None, "not a git repository")

            branch = self._try_output(["rev-parse", "--abbrev-ref", "HEAD"], cwd=resolved).strip() or None

            ahead = 0
            behind = 0
            upstream_line = self._try_output(
                ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
                cwd=resolved,
            ).strip()
            upstream = upstream_line or (f"origin/{branch}" if branch else None)
            if upstream:
                counts = self._try_output(
                    ["rev-list", "--left-right", "--count", f"HEAD...{upstream}"],
                    cwd=resolved,
                ).split()
                if len(counts) == 2:
                    ahead = self._int_or_zero(counts[0])
                    behind = self._int_or_zero(counts[1])

            status = self._try_output(["status", "--porcelain"], cwd=resolved)
            lines = [line for line in status.splitlines() if line.strip()]
            untracked = sum(1 for line in lines if line.startswith("??"))
            dirty = any(not line.startswith("??") for line in lines)

            commit = self._read_last_commit(resolved)
            return RepoSnapshot(str(resolved), branch, behind, ahead, dirty, untracked, commit)
        except Exception as exc:
            return RepoSnapshot(str(resolved), None, 0, 0, False, 0, None, str(exc)[:500])

    def _refresh(self, path: Path, default_branch: str) -> str:
        self._run(["fetch", "--all", "--prune"], cwd=path)
        branch = self._try_output(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path).strip() or default_branch
        upstream = f"origin/{branch}"
        counts = self._try_output(
            ["rev-list", "--left-right", "--count", f"HEAD...{upstream}"], cwd=path
        ).split()
        ahead = self._int_or_zero(counts[0]) if len(counts) == 2 else 0
        behind = self._int_or_zero(counts[1]) if len(counts) == 2 else 0
        dirty = self._is_dirty(path)
        if behind and not ahead and not dirty:
            self._run(["merge", "--ff-only", upstream], cwd=path)
            return _FAST_FORWARDED
        if behind and ahead:
            return _DIVERGED
        if dirty:
            return _DIRTY
        return _UP_TO_DATE

    def _is_dirty(self, path: Path) -> bool:
        status = self._try_output(["status", "--porcelain"], cwd=path)
        return any(line.strip() and not line.strip().startswith("??") for line in status.splitlines())

    def _read_last_commit(self, path: Path) -> dict | None:
        raw = self._output(["log", "-1", "--format=%H%x1f%s%x1f%an%x1f%aI"], cwd=path)
        parts = raw.strip().split("\x1f")
        if len(parts) < 4:
            return None
        sha, message, author, committed_at = parts[0], parts[1], parts[2], parts[3]
        return {
            "sha": sha.strip(),
            "message": message.strip(),
            "author": author.strip(),
            "committed_at": committed_at.strip(),
        }

    def _run(self, args: list[str], *, cwd: Path | None = None) -> str:
        env = apply_git_credentials(dict(os.environ))
        env["GIT_TERMINAL_PROMPT"] = "0"
        completed = subprocess.run(
            [self.git_binary, *args],
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=self.timeout_sec,
        )
        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout or "").strip()[:800]
            raise RuntimeError(f"git {' '.join(args)} failed: {message or completed.returncode}")
        return completed.stdout or ""

    def _output(self, args: list[str], *, cwd: Path | None = None) -> str:
        return self._run(args, cwd=cwd)

    def _try_output(self, args: list[str], *, cwd: Path | None = None) -> str:
        try:
            return self._run(args, cwd=cwd)
        except Exception:
            return ""

    @staticmethod
    def _int_or_zero(value: str) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
