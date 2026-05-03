from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from shared.contracts import utcnow
from shared.sqlite_client import connect_sync


def generate_project_id() -> str:
    return f"prj_{uuid4().hex[:12]}"


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if not value:
        return []
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError:
        return []
    if not isinstance(decoded, list):
        return []
    return [str(item) for item in decoded if str(item).strip()]


@dataclass(frozen=True)
class ProjectRecord:
    project_id: str
    aliases: list[str]
    repo_url: str | None
    local_path: str | None
    deployment_url: str | None
    last_task_id: str | None
    last_session_id: str | None
    harness_thread_ids: list[str]
    status: str
    summary: str | None
    artifact_ids: list[str]
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Any) -> "ProjectRecord":
        return cls(
            project_id=str(row["project_id"]),
            aliases=_json_list(row["aliases"]),
            repo_url=row["repo_url"],
            local_path=row["local_path"],
            deployment_url=row["deployment_url"],
            last_task_id=row["last_task_id"],
            last_session_id=row["last_session_id"],
            harness_thread_ids=_json_list(row["harness_thread_ids"]),
            status=str(row["status"]),
            summary=row["summary"],
            artifact_ids=_json_list(row["artifact_ids"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "aliases": self.aliases,
            "repo_url": self.repo_url,
            "local_path": self.local_path,
            "deployment_url": self.deployment_url,
            "last_task_id": self.last_task_id,
            "last_session_id": self.last_session_id,
            "harness_thread_ids": self.harness_thread_ids,
            "status": self.status,
            "summary": self.summary,
            "artifact_ids": self.artifact_ids,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class ProjectRegistry:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser()

    def initialize(self) -> None:
        with connect_sync(self.db_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS alpha_projects (
                    project_id TEXT PRIMARY KEY,
                    aliases TEXT NOT NULL DEFAULT '[]',
                    repo_url TEXT,
                    local_path TEXT,
                    deployment_url TEXT,
                    last_task_id TEXT,
                    last_session_id TEXT,
                    harness_thread_ids TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL,
                    summary TEXT,
                    artifact_ids TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_alpha_projects_session ON alpha_projects(last_session_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_alpha_projects_updated ON alpha_projects(updated_at)"
            )

    def create_project(
        self,
        *,
        aliases: list[str] | None = None,
        repo_url: str | None = None,
        local_path: str | None = None,
        deployment_url: str | None = None,
        last_task_id: str | None = None,
        last_session_id: str | None = None,
        summary: str | None = None,
        status: str = "prepared",
    ) -> ProjectRecord:
        self.initialize()
        now = utcnow().isoformat()
        project_id = generate_project_id()
        with connect_sync(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO alpha_projects (
                    project_id, aliases, repo_url, local_path, deployment_url,
                    last_task_id, last_session_id, harness_thread_ids, status,
                    summary, artifact_ids, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    json.dumps(aliases or []),
                    repo_url,
                    local_path,
                    deployment_url,
                    last_task_id,
                    last_session_id,
                    json.dumps([]),
                    status,
                    summary,
                    json.dumps([]),
                    now,
                    now,
                ),
            )
        record = self.get_project(project_id)
        if record is None:
            raise RuntimeError("Alpha project insert succeeded but record could not be reloaded.")
        return record

    def get_project(self, project_id: str) -> ProjectRecord | None:
        self.initialize()
        with connect_sync(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM alpha_projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        return ProjectRecord.from_row(row) if row is not None else None

    def find_project(self, project_ref: str) -> ProjectRecord | None:
        normalized = str(project_ref or "").strip()
        if not normalized:
            return None
        self.initialize()
        with connect_sync(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM alpha_projects
                WHERE project_id = ?
                   OR repo_url = ?
                   OR local_path = ?
                   OR deployment_url = ?
                ORDER BY updated_at DESC
                LIMIT 20
                """,
                (normalized, normalized, normalized, normalized),
            ).fetchall()
            if rows:
                return ProjectRecord.from_row(rows[0])
            alias_rows = connection.execute(
                "SELECT * FROM alpha_projects ORDER BY updated_at DESC LIMIT 200"
            ).fetchall()
        for row in alias_rows:
            record = ProjectRecord.from_row(row)
            if normalized in record.aliases:
                return record
        return None

    def recent_for_session(self, session_id: str | None, *, limit: int = 5) -> list[ProjectRecord]:
        normalized = str(session_id or "").strip()
        if not normalized:
            return []
        self.initialize()
        with connect_sync(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM alpha_projects
                WHERE last_session_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (normalized, max(1, min(limit, 20))),
            ).fetchall()
        return [ProjectRecord.from_row(row) for row in rows]

    def mark_task(
        self,
        project_id: str,
        *,
        task_id: str | None,
        session_id: str | None,
        local_path: str | None = None,
        summary: str | None = None,
        status: str | None = None,
    ) -> ProjectRecord:
        self.initialize()
        now = utcnow().isoformat()
        current = self.get_project(project_id)
        if current is None:
            raise KeyError(project_id)
        with connect_sync(self.db_path) as connection:
            connection.execute(
                """
                UPDATE alpha_projects
                SET last_task_id = ?,
                    last_session_id = ?,
                    local_path = ?,
                    summary = ?,
                    status = ?,
                    updated_at = ?
                WHERE project_id = ?
                """,
                (
                    task_id or current.last_task_id,
                    session_id or current.last_session_id,
                    local_path or current.local_path,
                    summary if summary is not None else current.summary,
                    status or current.status,
                    now,
                    project_id,
                ),
            )
        updated = self.get_project(project_id)
        if updated is None:
            raise KeyError(project_id)
        return updated

