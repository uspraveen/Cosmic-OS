from __future__ import annotations

import json
import re
from contextlib import closing
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


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        if hasattr(row, "keys") and key not in row.keys():
            return default
        return row[key]
    except (IndexError, KeyError, TypeError):
        return default


def _compact_text(value: Any, *, limit: int = 4000) -> str | None:
    normalized = re.sub(r"\s+", " ", str(value or "")).strip()
    return normalized[:limit] or None


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if not item:
            continue
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _merge_unique(*groups: list[str] | None) -> list[str]:
    merged: list[str] = []
    for group in groups:
        merged.extend(group or [])
    return _dedupe(merged)


def _tokenize(value: Any) -> set[str]:
    text = str(value or "").casefold()
    return {token for token in re.findall(r"[a-z0-9][a-z0-9_-]*", text) if len(token) >= 2}


@dataclass(frozen=True)
class ProjectTaskRecord:
    task_id: str
    project_id: str
    session_id: str | None
    goal: str | None
    context_brief: str | None
    preferred_harness: str | None
    status: str | None
    summary: str | None
    artifact_ids: list[str]
    repo_urls: list[str]
    deployment_urls: list[str]
    local_paths: list[str]
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Any) -> "ProjectTaskRecord":
        return cls(
            task_id=str(row["task_id"]),
            project_id=str(row["project_id"]),
            session_id=_row_get(row, "session_id"),
            goal=_row_get(row, "goal"),
            context_brief=_row_get(row, "context_brief"),
            preferred_harness=_row_get(row, "preferred_harness"),
            status=_row_get(row, "status"),
            summary=_row_get(row, "summary"),
            artifact_ids=_json_list(_row_get(row, "artifact_ids")),
            repo_urls=_json_list(_row_get(row, "repo_urls")),
            deployment_urls=_json_list(_row_get(row, "deployment_urls")),
            local_paths=_json_list(_row_get(row, "local_paths")),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


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
    goal: str | None
    context_brief: str | None
    preferred_harness: str | None
    search_text: str | None
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
            goal=_row_get(row, "goal"),
            context_brief=_row_get(row, "context_brief"),
            preferred_harness=_row_get(row, "preferred_harness"),
            search_text=_row_get(row, "search_text"),
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
            "goal": self.goal,
            "context_brief": self.context_brief,
            "preferred_harness": self.preferred_harness,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class ProjectCandidate:
    project: ProjectRecord
    score: float
    match_type: str
    match_reason: str
    matched_fields: list[str]
    task_ids: list[str]
    artifact_ids: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "project": self.project.as_dict(),
            "score": round(self.score, 2),
            "match_type": self.match_type,
            "match_reason": self.match_reason,
            "matched_fields": self.matched_fields,
            "task_ids": self.task_ids,
            "artifact_ids": self.artifact_ids,
        }


class ProjectRegistry:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser()

    def initialize(self) -> None:
        with closing(connect_sync(self.db_path)) as connection:
            with connection:
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
                        goal TEXT,
                        context_brief TEXT,
                        preferred_harness TEXT,
                        search_text TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                self._ensure_column(connection, "alpha_projects", "goal", "TEXT")
                self._ensure_column(connection, "alpha_projects", "context_brief", "TEXT")
                self._ensure_column(connection, "alpha_projects", "preferred_harness", "TEXT")
                self._ensure_column(connection, "alpha_projects", "search_text", "TEXT")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS alpha_project_tasks (
                        task_id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        session_id TEXT,
                        goal TEXT,
                        context_brief TEXT,
                        preferred_harness TEXT,
                        status TEXT,
                        summary TEXT,
                        artifact_ids TEXT NOT NULL DEFAULT '[]',
                        repo_urls TEXT NOT NULL DEFAULT '[]',
                        deployment_urls TEXT NOT NULL DEFAULT '[]',
                        local_paths TEXT NOT NULL DEFAULT '[]',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY(project_id) REFERENCES alpha_projects(project_id) ON DELETE CASCADE
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_alpha_projects_session ON alpha_projects(last_session_id)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_alpha_projects_updated ON alpha_projects(updated_at)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_alpha_project_tasks_project ON alpha_project_tasks(project_id)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_alpha_project_tasks_session ON alpha_project_tasks(session_id)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_alpha_project_tasks_updated ON alpha_project_tasks(updated_at)"
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
        goal: str | None = None,
        context_brief: str | None = None,
        preferred_harness: str | None = None,
    ) -> ProjectRecord:
        self.initialize()
        now = utcnow().isoformat()
        project_id = generate_project_id()
        clean_aliases = _dedupe(aliases or [])
        search_text = self._build_project_search_text(
            project_id=project_id,
            aliases=clean_aliases,
            repo_url=repo_url,
            local_path=local_path,
            deployment_url=deployment_url,
            last_task_id=last_task_id,
            last_session_id=last_session_id,
            harness_thread_ids=[],
            status=status,
            summary=summary,
            artifact_ids=[],
            goal=goal,
            context_brief=context_brief,
            preferred_harness=preferred_harness,
        )
        with closing(connect_sync(self.db_path)) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO alpha_projects (
                        project_id, aliases, repo_url, local_path, deployment_url,
                        last_task_id, last_session_id, harness_thread_ids, status,
                        summary, artifact_ids, goal, context_brief, preferred_harness,
                        search_text, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        json.dumps(clean_aliases),
                        repo_url,
                        local_path,
                        deployment_url,
                        last_task_id,
                        last_session_id,
                        json.dumps([]),
                        status,
                        summary,
                        json.dumps([]),
                        goal,
                        context_brief,
                        preferred_harness,
                        search_text,
                        now,
                        now,
                    ),
                )
                if last_task_id:
                    self._upsert_task_locked(
                        connection,
                        project_id=project_id,
                        task_id=last_task_id,
                        session_id=last_session_id,
                        goal=goal,
                        context_brief=context_brief,
                        preferred_harness=preferred_harness,
                        status=status,
                        summary=summary,
                        repo_url=repo_url,
                        deployment_url=deployment_url,
                        local_path=local_path,
                        artifact_ids=[],
                        now=now,
                    )
        record = self.get_project(project_id)
        if record is None:
            raise RuntimeError("Alpha project insert succeeded but record could not be reloaded.")
        return record

    def get_project(self, project_id: str) -> ProjectRecord | None:
        self.initialize()
        with closing(connect_sync(self.db_path)) as connection:
            row = connection.execute(
                "SELECT * FROM alpha_projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        return ProjectRecord.from_row(row) if row is not None else None

    def find_project(self, project_ref: str) -> ProjectRecord | None:
        normalized = str(project_ref or "").strip()
        if not normalized:
            return None
        candidates = self.search_projects(normalized, limit=1)
        return candidates[0].project if candidates else None

    def search_projects(
        self,
        query: str | None,
        *,
        session_id: str | None = None,
        limit: int = 5,
    ) -> list[ProjectCandidate]:
        self.initialize()
        capped_limit = max(1, min(int(limit or 5), 20))
        normalized_query = str(query or "").strip()
        normalized_session = str(session_id or "").strip()

        if not normalized_query:
            records = (
                self.recent_for_session(normalized_session, limit=capped_limit)
                if normalized_session
                else self.recent_projects(limit=capped_limit)
            )
            return [
                ProjectCandidate(
                    project=record,
                    score=max(1.0, 80.0 - index),
                    match_type="recent",
                    match_reason=(
                        "Recent Alpha project in this session."
                        if normalized_session
                        else "Recent Alpha project."
                    ),
                    matched_fields=["last_session_id"] if normalized_session else ["updated_at"],
                    task_ids=[record.last_task_id] if record.last_task_id else [],
                    artifact_ids=record.artifact_ids,
                )
                for index, record in enumerate(records)
            ]

        with closing(connect_sync(self.db_path)) as connection:
            project_rows = connection.execute(
                """
                SELECT * FROM alpha_projects
                ORDER BY updated_at DESC
                LIMIT 500
                """
            ).fetchall()
            task_rows = connection.execute(
                """
                SELECT * FROM alpha_project_tasks
                ORDER BY updated_at DESC
                LIMIT 2000
                """
            ).fetchall()

        tasks_by_project: dict[str, list[ProjectTaskRecord]] = {}
        for row in task_rows:
            task = ProjectTaskRecord.from_row(row)
            tasks_by_project.setdefault(task.project_id, []).append(task)

        candidates: list[ProjectCandidate] = []
        for index, row in enumerate(project_rows):
            record = ProjectRecord.from_row(row)
            candidate = self._score_project(
                record,
                tasks_by_project.get(record.project_id, []),
                query=normalized_query,
                session_id=normalized_session,
                recency_index=index,
            )
            if candidate is not None:
                candidates.append(candidate)

        candidates.sort(key=lambda item: (item.score, item.project.updated_at), reverse=True)
        return candidates[:capped_limit]

    def recent_projects(self, *, limit: int = 5) -> list[ProjectRecord]:
        self.initialize()
        with closing(connect_sync(self.db_path)) as connection:
            rows = connection.execute(
                """
                SELECT * FROM alpha_projects
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (max(1, min(limit, 20)),),
            ).fetchall()
        return [ProjectRecord.from_row(row) for row in rows]

    def recent_for_session(self, session_id: str | None, *, limit: int = 5) -> list[ProjectRecord]:
        normalized = str(session_id or "").strip()
        if not normalized:
            return []
        self.initialize()
        with closing(connect_sync(self.db_path)) as connection:
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
        goal: str | None = None,
        context_brief: str | None = None,
        preferred_harness: str | None = None,
        artifact_ids: list[str] | None = None,
        repo_url: str | None = None,
        deployment_url: str | None = None,
    ) -> ProjectRecord:
        self.initialize()
        now = utcnow().isoformat()
        current = self.get_project(project_id)
        if current is None:
            raise KeyError(project_id)

        next_artifact_ids = _merge_unique(current.artifact_ids, artifact_ids)
        next_repo_url = repo_url or current.repo_url
        next_local_path = local_path or current.local_path
        next_deployment_url = deployment_url or current.deployment_url
        next_last_task_id = task_id or current.last_task_id
        next_last_session_id = session_id or current.last_session_id
        next_summary = summary if summary is not None else current.summary
        next_status = status or current.status
        next_goal = goal if goal is not None else current.goal
        next_context_brief = context_brief if context_brief is not None else current.context_brief
        next_preferred_harness = (
            preferred_harness if preferred_harness is not None else current.preferred_harness
        )
        next_search_text = self._build_project_search_text(
            project_id=current.project_id,
            aliases=current.aliases,
            repo_url=next_repo_url,
            local_path=next_local_path,
            deployment_url=next_deployment_url,
            last_task_id=next_last_task_id,
            last_session_id=next_last_session_id,
            harness_thread_ids=current.harness_thread_ids,
            status=next_status,
            summary=next_summary,
            artifact_ids=next_artifact_ids,
            goal=next_goal,
            context_brief=next_context_brief,
            preferred_harness=next_preferred_harness,
        )
        with closing(connect_sync(self.db_path)) as connection:
            with connection:
                connection.execute(
                    """
                    UPDATE alpha_projects
                    SET last_task_id = ?,
                        last_session_id = ?,
                        repo_url = ?,
                        local_path = ?,
                        deployment_url = ?,
                        summary = ?,
                        status = ?,
                        artifact_ids = ?,
                        goal = ?,
                        context_brief = ?,
                        preferred_harness = ?,
                        search_text = ?,
                        updated_at = ?
                    WHERE project_id = ?
                    """,
                    (
                        next_last_task_id,
                        next_last_session_id,
                        next_repo_url,
                        next_local_path,
                        next_deployment_url,
                        next_summary,
                        next_status,
                        json.dumps(next_artifact_ids),
                        next_goal,
                        next_context_brief,
                        next_preferred_harness,
                        next_search_text,
                        now,
                        project_id,
                    ),
                )
                if task_id:
                    self._upsert_task_locked(
                        connection,
                        project_id=project_id,
                        task_id=task_id,
                        session_id=session_id,
                        goal=goal,
                        context_brief=context_brief,
                        preferred_harness=preferred_harness,
                        status=status,
                        summary=summary,
                        repo_url=repo_url,
                        deployment_url=deployment_url,
                        local_path=local_path,
                        artifact_ids=artifact_ids or [],
                        now=now,
                    )
        updated = self.get_project(project_id)
        if updated is None:
            raise KeyError(project_id)
        return updated

    @staticmethod
    def _ensure_column(connection: Any, table: str, column: str, declaration: str) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def _upsert_task_locked(
        self,
        connection: Any,
        *,
        project_id: str,
        task_id: str,
        session_id: str | None,
        goal: str | None,
        context_brief: str | None,
        preferred_harness: str | None,
        status: str | None,
        summary: str | None,
        artifact_ids: list[str] | None,
        repo_url: str | None,
        deployment_url: str | None,
        local_path: str | None,
        now: str,
    ) -> None:
        existing = connection.execute(
            "SELECT * FROM alpha_project_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        merged_artifact_ids = _merge_unique(_json_list(_row_get(existing, "artifact_ids")), artifact_ids)
        merged_repo_urls = _merge_unique(_json_list(_row_get(existing, "repo_urls")), [repo_url] if repo_url else [])
        merged_deployment_urls = _merge_unique(
            _json_list(_row_get(existing, "deployment_urls")),
            [deployment_url] if deployment_url else [],
        )
        merged_local_paths = _merge_unique(
            _json_list(_row_get(existing, "local_paths")),
            [local_path] if local_path else [],
        )
        if existing is None:
            connection.execute(
                """
                INSERT INTO alpha_project_tasks (
                    task_id, project_id, session_id, goal, context_brief,
                    preferred_harness, status, summary, artifact_ids,
                    repo_urls, deployment_urls, local_paths, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    project_id,
                    session_id,
                    goal,
                    context_brief,
                    preferred_harness,
                    status,
                    summary,
                    json.dumps(merged_artifact_ids),
                    json.dumps(merged_repo_urls),
                    json.dumps(merged_deployment_urls),
                    json.dumps(merged_local_paths),
                    now,
                    now,
                ),
            )
            return

        connection.execute(
            """
            UPDATE alpha_project_tasks
            SET project_id = ?,
                session_id = ?,
                goal = ?,
                context_brief = ?,
                preferred_harness = ?,
                status = ?,
                summary = ?,
                artifact_ids = ?,
                repo_urls = ?,
                deployment_urls = ?,
                local_paths = ?,
                updated_at = ?
            WHERE task_id = ?
            """,
            (
                project_id,
                session_id or _row_get(existing, "session_id"),
                goal if goal is not None else _row_get(existing, "goal"),
                context_brief if context_brief is not None else _row_get(existing, "context_brief"),
                preferred_harness if preferred_harness is not None else _row_get(existing, "preferred_harness"),
                status if status is not None else _row_get(existing, "status"),
                summary if summary is not None else _row_get(existing, "summary"),
                json.dumps(merged_artifact_ids),
                json.dumps(merged_repo_urls),
                json.dumps(merged_deployment_urls),
                json.dumps(merged_local_paths),
                now,
                task_id,
            ),
        )

    def _score_project(
        self,
        record: ProjectRecord,
        tasks: list[ProjectTaskRecord],
        *,
        query: str,
        session_id: str,
        recency_index: int,
    ) -> ProjectCandidate | None:
        query_folded = query.casefold()
        query_tokens = _tokenize(query)
        score = 0.0
        matched_fields: list[str] = []
        exact_match = False
        partial_match = False
        keyword_match = False

        def add_field(
            field_name: str,
            values: list[str | None],
            *,
            exact_weight: float,
            partial_weight: float,
            token_weight: float,
        ) -> None:
            nonlocal score, exact_match, partial_match, keyword_match
            field_score = 0.0
            for value in values:
                raw = str(value or "").strip()
                if not raw:
                    continue
                raw_folded = raw.casefold()
                if raw_folded == query_folded:
                    field_score = max(field_score, exact_weight)
                    exact_match = True
                elif query_folded and query_folded in raw_folded:
                    field_score = max(field_score, partial_weight)
                    partial_match = True
                if query_tokens and token_weight > 0:
                    overlap = query_tokens & _tokenize(raw)
                    if overlap:
                        token_score = token_weight * (len(overlap) / len(query_tokens))
                        if len(overlap) == len(query_tokens):
                            token_score += token_weight * 0.25
                        field_score = max(field_score, token_score)
                        keyword_match = True
            if field_score > 0:
                score += field_score
                matched_fields.append(field_name)

        add_field("project_id", [record.project_id], exact_weight=1000, partial_weight=150, token_weight=0)
        add_field("aliases", record.aliases, exact_weight=950, partial_weight=180, token_weight=80)
        add_field("repo_url", [record.repo_url], exact_weight=930, partial_weight=170, token_weight=70)
        add_field(
            "deployment_url",
            [record.deployment_url],
            exact_weight=930,
            partial_weight=170,
            token_weight=70,
        )
        add_field("local_path", [record.local_path], exact_weight=900, partial_weight=160, token_weight=60)
        add_field("last_task_id", [record.last_task_id], exact_weight=900, partial_weight=150, token_weight=0)
        add_field("last_session_id", [record.last_session_id], exact_weight=700, partial_weight=90, token_weight=0)
        add_field("artifact_ids", record.artifact_ids, exact_weight=875, partial_weight=150, token_weight=0)
        add_field(
            "harness_thread_ids",
            record.harness_thread_ids,
            exact_weight=850,
            partial_weight=140,
            token_weight=0,
        )
        add_field("goal", [record.goal], exact_weight=400, partial_weight=150, token_weight=110)
        add_field("summary", [record.summary], exact_weight=400, partial_weight=140, token_weight=100)
        add_field("context_brief", [record.context_brief], exact_weight=350, partial_weight=130, token_weight=90)
        add_field("search_text", [record.search_text], exact_weight=250, partial_weight=110, token_weight=60)
        add_field("status", [record.status], exact_weight=120, partial_weight=45, token_weight=20)
        add_field("preferred_harness", [record.preferred_harness], exact_weight=100, partial_weight=45, token_weight=20)

        for task in tasks[:100]:
            add_field("task_id", [task.task_id], exact_weight=925, partial_weight=150, token_weight=0)
            add_field("task_session_id", [task.session_id], exact_weight=650, partial_weight=85, token_weight=0)
            add_field("task_goal", [task.goal], exact_weight=400, partial_weight=150, token_weight=120)
            add_field("task_summary", [task.summary], exact_weight=400, partial_weight=140, token_weight=110)
            add_field("task_context", [task.context_brief], exact_weight=350, partial_weight=130, token_weight=100)
            add_field("task_artifact_ids", task.artifact_ids, exact_weight=875, partial_weight=150, token_weight=0)
            add_field("task_repo_urls", task.repo_urls, exact_weight=930, partial_weight=170, token_weight=70)
            add_field(
                "task_deployment_urls",
                task.deployment_urls,
                exact_weight=930,
                partial_weight=170,
                token_weight=70,
            )
            add_field("task_local_paths", task.local_paths, exact_weight=900, partial_weight=160, token_weight=60)
            add_field("task_harness", [task.preferred_harness], exact_weight=100, partial_weight=45, token_weight=20)

        if score <= 0:
            return None

        if session_id and (
            record.last_session_id == session_id or any(task.session_id == session_id for task in tasks)
        ):
            score += 35
            matched_fields.append("session_affinity")

        score += max(0.0, 20.0 - min(recency_index, 40) * 0.5)
        match_type = "exact" if exact_match else "partial" if partial_match else "keyword" if keyword_match else "scored"
        fields = _dedupe(matched_fields)
        reason_label = {
            "exact": "Exact Alpha project match",
            "partial": "Partial Alpha project match",
            "keyword": "Keyword match across Alpha project/task metadata",
            "scored": "Scored Alpha project match",
        }[match_type]
        task_ids = _dedupe([task.task_id for task in tasks if task.task_id])[:10]
        artifact_ids = _merge_unique(record.artifact_ids, *[task.artifact_ids for task in tasks])[:20]
        return ProjectCandidate(
            project=record,
            score=score,
            match_type=match_type,
            match_reason=f"{reason_label} on {', '.join(fields[:6])}.",
            matched_fields=fields,
            task_ids=task_ids,
            artifact_ids=artifact_ids,
        )

    def _build_project_search_text(
        self,
        *,
        project_id: str,
        aliases: list[str],
        repo_url: str | None,
        local_path: str | None,
        deployment_url: str | None,
        last_task_id: str | None,
        last_session_id: str | None,
        harness_thread_ids: list[str],
        status: str,
        summary: str | None,
        artifact_ids: list[str],
        goal: str | None,
        context_brief: str | None,
        preferred_harness: str | None,
    ) -> str | None:
        parts = _dedupe(
            [
                project_id,
                *aliases,
                repo_url or "",
                local_path or "",
                deployment_url or "",
                last_task_id or "",
                last_session_id or "",
                *harness_thread_ids,
                status,
                summary or "",
                *artifact_ids,
                goal or "",
                context_brief or "",
                preferred_harness or "",
            ]
        )
        return _compact_text(" ".join(parts), limit=4000)
