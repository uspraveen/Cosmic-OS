from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkspacePaths:
    project_id: str
    task_id: str
    workspace: Path
    artifacts: Path
    codex_home: Path
    opencode_home: Path
    cursor_home: Path
    deployments: Path
    caches: Path

    def as_dict(self) -> dict[str, str]:
        return {
            "project_id": self.project_id,
            "task_id": self.task_id,
            "workspace": str(self.workspace),
            "artifacts": str(self.artifacts),
            "codex_home": str(self.codex_home),
            "opencode_home": str(self.opencode_home),
            "cursor_home": str(self.cursor_home),
            "deployments": str(self.deployments),
            "caches": str(self.caches),
        }


class WorkspaceManager:
    def __init__(
        self,
        alpha_root: str | Path,
        *,
        codex_home: str | Path | None = None,
        cursor_home: str | Path | None = None,
    ) -> None:
        self.alpha_root = Path(alpha_root).expanduser()
        self.codex_home = Path(codex_home).expanduser() if codex_home else self.alpha_root / "homes" / "codex"
        self.cursor_home = Path(cursor_home).expanduser() if cursor_home else self.alpha_root / "homes" / "cursor"

    def ensure_base_layout(self) -> None:
        for child in (
            "workspaces",
            "artifacts",
            "homes/codex",
            "homes/opencode",
            "homes/cursor",
            "deployments",
            "caches",
        ):
            (self.alpha_root / child).mkdir(parents=True, exist_ok=True)

    def prepare(self, *, project_id: str, task_id: str) -> WorkspacePaths:
        normalized_project_id = self._validate_segment(project_id, prefix="prj_")
        normalized_task_id = self._validate_segment(task_id, prefix="tsk_")
        self.ensure_base_layout()
        paths = WorkspacePaths(
            project_id=normalized_project_id,
            task_id=normalized_task_id,
            workspace=self.alpha_root / "workspaces" / normalized_project_id,
            artifacts=self.alpha_root / "artifacts" / normalized_task_id,
            codex_home=self.codex_home,
            opencode_home=self.alpha_root / "homes" / "opencode",
            cursor_home=self.cursor_home,
            deployments=self.alpha_root / "deployments",
            caches=self.alpha_root / "caches",
        )
        for path in (
            paths.workspace,
            paths.artifacts,
            paths.codex_home,
            paths.opencode_home,
            paths.cursor_home,
            paths.deployments,
            paths.caches,
        ):
            path.mkdir(parents=True, exist_ok=True)
        return paths

    def _validate_segment(self, value: str, *, prefix: str) -> str:
        normalized = str(value or "").strip()
        if not normalized.startswith(prefix):
            raise ValueError(f"Expected id starting with {prefix!r}.")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
        if any(char not in allowed for char in normalized):
            raise ValueError("Workspace ids may only contain letters, digits, underscore, and dash.")
        return normalized
