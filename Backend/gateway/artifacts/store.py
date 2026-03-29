from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ArtifactStore:
    """SQLite-backed store for Gateway-served artifact metadata.

    The store persists both inbound attachment references and produced output
    artifact references so signed preview/download URLs can resolve bytes
    without depending on the original channel implementation.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS inbound_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    source_channel TEXT NOT NULL,
                    source_platform TEXT,
                    source_message_id TEXT,
                    kind TEXT NOT NULL,
                    mime_type TEXT,
                    filename TEXT,
                    caption TEXT,
                    size_bytes INTEGER,
                    width INTEGER,
                    height INTEGER,
                    duration_ms INTEGER,
                    sha256 TEXT,
                    bridge_media_ref TEXT,
                    download_url TEXT,
                    path TEXT,
                    ingest_state TEXT,
                    parse_task_id TEXT,
                    parse_bundle_id TEXT,
                    parsed_summary_json TEXT,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_inbound_artifacts_request
                    ON inbound_artifacts(request_id, created_at);

                CREATE INDEX IF NOT EXISTS idx_inbound_artifacts_session
                    ON inbound_artifacts(session_id, created_at);

                CREATE TABLE IF NOT EXISTS output_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    source_channel TEXT NOT NULL,
                    source_platform TEXT,
                    source_message_id TEXT,
                    kind TEXT NOT NULL,
                    mime_type TEXT,
                    filename TEXT,
                    caption TEXT,
                    size_bytes INTEGER,
                    width INTEGER,
                    height INTEGER,
                    duration_ms INTEGER,
                    sha256 TEXT,
                    bridge_media_ref TEXT,
                    download_url TEXT,
                    path TEXT,
                    ingest_state TEXT,
                    parse_task_id TEXT,
                    parse_bundle_id TEXT,
                    parsed_summary_json TEXT,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_output_artifacts_request
                    ON output_artifacts(request_id, created_at);

                CREATE INDEX IF NOT EXISTS idx_output_artifacts_session
                    ON output_artifacts(session_id, created_at);
                """
            )
            self._ensure_column(connection, "inbound_artifacts", "path", "TEXT")
            self._ensure_column(connection, "inbound_artifacts", "ingest_state", "TEXT")
            self._ensure_column(connection, "inbound_artifacts", "parse_task_id", "TEXT")
            self._ensure_column(connection, "inbound_artifacts", "parse_bundle_id", "TEXT")
            self._ensure_column(connection, "inbound_artifacts", "parsed_summary_json", "TEXT")
            self._ensure_column(connection, "output_artifacts", "path", "TEXT")
            self._ensure_column(connection, "output_artifacts", "ingest_state", "TEXT")
            self._ensure_column(connection, "output_artifacts", "parse_task_id", "TEXT")
            self._ensure_column(connection, "output_artifacts", "parse_bundle_id", "TEXT")
            self._ensure_column(connection, "output_artifacts", "parsed_summary_json", "TEXT")
            connection.commit()

    def persist_inbound_attachments(
        self,
        *,
        request_id: str,
        session_id: str,
        source_channel: str,
        source_platform: str | None,
        source_message_id: str | None,
        attachments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        manifests: list[dict[str, Any]] = []
        if not request_id or not session_id or not source_channel:
            return manifests
        if not isinstance(attachments, list):
            return manifests

        created_at = utcnow_iso()
        with self._lock, self._connect() as connection:
            for index, attachment in enumerate(attachments, start=1):
                if not isinstance(attachment, dict):
                    continue
                artifact_id = str(attachment.get("artifact_id") or "").strip() or f"art_{uuid4().hex}"
                kind = str(attachment.get("kind") or "").strip().lower() or "unknown"
                mime_type = self._normalize_text(attachment.get("mime_type"))
                filename = self._normalize_text(attachment.get("filename"))
                caption = self._normalize_text(attachment.get("caption"))
                size_bytes = self._normalize_int(attachment.get("size_bytes"))
                width = self._normalize_int(attachment.get("width"))
                height = self._normalize_int(attachment.get("height"))
                duration_ms = self._normalize_int(attachment.get("duration_ms"))
                sha256 = self._normalize_text(attachment.get("sha256"))
                bridge_media_ref = self._normalize_text(attachment.get("bridge_media_ref"))
                download_url = self._normalize_text(attachment.get("download_url"))
                path = self._normalize_text(attachment.get("path"))
                ingest_state = self._normalize_text(attachment.get("ingest_state")) or "bridge_reference"
                parse_task_id = self._normalize_text(attachment.get("parse_task_id"))
                parse_bundle_id = self._normalize_text(attachment.get("parse_bundle_id"))
                parsed_summary = attachment.get("parsed_summary")
                passthrough_metadata = {
                    key: value
                    for key, value in attachment.items()
                    if key
                    not in {
                        "artifact_id",
                        "id",
                        "kind",
                        "mime_type",
                        "filename",
                        "caption",
                        "size_bytes",
                        "width",
                        "height",
                        "duration_ms",
                        "sha256",
                        "bridge_media_ref",
                        "download_url",
                        "path",
                        "ingest_state",
                        "parse_task_id",
                        "parse_bundle_id",
                        "parsed_summary",
                    }
                }
                try:
                    metadata_json = json.dumps(passthrough_metadata, default=str) if passthrough_metadata else None
                except (TypeError, ValueError):
                    logger.exception(
                        "artifact_store.metadata_serialize_failed request_id=%s artifact_id=%s kind=%s",
                        request_id,
                        artifact_id,
                        kind,
                    )
                    metadata_json = None
                try:
                    parsed_summary_json = (
                        json.dumps(parsed_summary, ensure_ascii=False, default=str)
                        if parsed_summary is not None
                        else None
                    )
                except (TypeError, ValueError):
                    logger.exception(
                        "artifact_store.parsed_summary_serialize_failed request_id=%s artifact_id=%s kind=%s",
                        request_id,
                        artifact_id,
                        kind,
                    )
                    parsed_summary_json = None
                connection.execute(
                    """
                    INSERT OR REPLACE INTO inbound_artifacts (
                        artifact_id,
                        request_id,
                        session_id,
                        source_channel,
                        source_platform,
                        source_message_id,
                        kind,
                        mime_type,
                        filename,
                        caption,
                        size_bytes,
                        width,
                        height,
                        duration_ms,
                        sha256,
                        bridge_media_ref,
                        download_url,
                        path,
                        ingest_state,
                        parse_task_id,
                        parse_bundle_id,
                        parsed_summary_json,
                        metadata_json,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact_id,
                        request_id,
                        session_id,
                        source_channel,
                        source_platform,
                        source_message_id,
                        kind,
                        mime_type,
                        filename,
                        caption,
                        size_bytes,
                        width,
                        height,
                        duration_ms,
                        sha256,
                        bridge_media_ref,
                        download_url,
                        path,
                        ingest_state,
                        parse_task_id,
                        parse_bundle_id,
                        parsed_summary_json,
                        metadata_json,
                        created_at,
                    ),
                )
                manifests.append(
                    {
                        "artifact_id": artifact_id,
                        "source_channel": source_channel,
                        "source_platform": source_platform,
                        "source_message_id": source_message_id,
                        "kind": kind,
                        "mime": mime_type or "application/octet-stream",
                        "filename": filename,
                        "caption": caption,
                        "size_bytes": size_bytes,
                        "width": width,
                        "height": height,
                        "duration_ms": duration_ms,
                        "sha256": sha256,
                        "bridge_media_ref": bridge_media_ref,
                        "download_url": download_url,
                        "ingest_state": ingest_state,
                        "path": path,
                        "parse_task_id": parse_task_id,
                        "parse_bundle_id": parse_bundle_id,
                        "parsed_summary": parsed_summary if isinstance(parsed_summary, dict) else None,
                        "task_id": parse_task_id,
                        "index": index,
                        "metadata": passthrough_metadata,
                    }
                )
            connection.commit()
        return manifests

    def list_for_request(self, request_id: str) -> list[dict[str, Any]]:
        if not request_id:
            return []
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    artifact_id,
                    request_id,
                    session_id,
                    source_channel,
                    source_platform,
                    source_message_id,
                    kind,
                    mime_type,
                    filename,
                    caption,
                    size_bytes,
                    width,
                    height,
                    duration_ms,
                    sha256,
                    bridge_media_ref,
                    download_url,
                    path,
                    ingest_state,
                    parse_task_id,
                    parse_bundle_id,
                    parsed_summary_json,
                    metadata_json,
                    created_at
                FROM inbound_artifacts
                WHERE request_id = ?
                ORDER BY created_at ASC, artifact_id ASC
                """,
                (request_id,),
            ).fetchall()
        return self._deserialize_rows(rows, request_id=request_id, session_id=None)

    def persist_output_artifacts(
        self,
        *,
        request_id: str,
        session_id: str,
        source_channel: str,
        source_platform: str | None,
        source_message_id: str | None,
        artifacts: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        manifests: list[dict[str, Any]] = []
        if not request_id or not session_id or not source_channel:
            return manifests
        if not isinstance(artifacts, list):
            return manifests

        created_at = utcnow_iso()
        with self._lock, self._connect() as connection:
            for index, artifact in enumerate(artifacts, start=1):
                if not isinstance(artifact, dict):
                    continue
                artifact_id = str(artifact.get("artifact_id") or "").strip()
                if not artifact_id:
                    continue
                kind = str(artifact.get("kind") or "").strip().lower() or "file"
                mime_type = self._normalize_text(artifact.get("mime")) or self._normalize_text(artifact.get("mime_type"))
                filename = self._normalize_text(artifact.get("filename"))
                size_bytes = self._normalize_int(artifact.get("size_bytes"))
                width = self._normalize_int(artifact.get("width"))
                height = self._normalize_int(artifact.get("height"))
                duration_ms = self._normalize_int(artifact.get("duration_ms"))
                sha256 = self._normalize_text(artifact.get("sha256"))
                path = self._normalize_text(artifact.get("path"))
                ingest_state = self._normalize_text(artifact.get("ingest_state")) or "available"
                passthrough_metadata = {
                    key: value
                    for key, value in artifact.items()
                    if key
                    not in {
                        "artifact_id",
                        "kind",
                        "mime",
                        "mime_type",
                        "filename",
                        "size_bytes",
                        "width",
                        "height",
                        "duration_ms",
                        "sha256",
                        "path",
                        "ingest_state",
                    }
                }
                try:
                    metadata_json = json.dumps(passthrough_metadata, default=str) if passthrough_metadata else None
                except (TypeError, ValueError):
                    logger.exception(
                        "artifact_store.output_metadata_serialize_failed request_id=%s artifact_id=%s kind=%s",
                        request_id,
                        artifact_id,
                        kind,
                    )
                    metadata_json = None

                connection.execute(
                    """
                    INSERT OR REPLACE INTO output_artifacts (
                        artifact_id,
                        request_id,
                        session_id,
                        source_channel,
                        source_platform,
                        source_message_id,
                        kind,
                        mime_type,
                        filename,
                        caption,
                        size_bytes,
                        width,
                        height,
                        duration_ms,
                        sha256,
                        bridge_media_ref,
                        download_url,
                        path,
                        ingest_state,
                        parse_task_id,
                        parse_bundle_id,
                        parsed_summary_json,
                        metadata_json,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact_id,
                        request_id,
                        session_id,
                        source_channel,
                        source_platform,
                        source_message_id,
                        kind,
                        mime_type,
                        filename,
                        None,
                        size_bytes,
                        width,
                        height,
                        duration_ms,
                        sha256,
                        None,
                        None,
                        path,
                        ingest_state,
                        None,
                        None,
                        None,
                        metadata_json,
                        created_at,
                    ),
                )
                manifests.append(
                    {
                        "artifact_id": artifact_id,
                        "request_id": request_id,
                        "session_id": session_id,
                        "source_channel": source_channel,
                        "source_platform": source_platform,
                        "source_message_id": source_message_id,
                        "kind": kind,
                        "mime": mime_type or "application/octet-stream",
                        "filename": filename,
                        "size_bytes": size_bytes,
                        "width": width,
                        "height": height,
                        "duration_ms": duration_ms,
                        "sha256": sha256,
                        "path": path,
                        "ingest_state": ingest_state,
                        "index": index,
                        "metadata": passthrough_metadata,
                    }
                )
            connection.commit()
        return manifests

    def list_for_session(self, session_id: str, *, limit: int = 32) -> list[dict[str, Any]]:
        if not session_id:
            return []
        normalized_limit = max(1, min(int(limit), 256))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    artifact_id,
                    request_id,
                    session_id,
                    source_channel,
                    source_platform,
                    source_message_id,
                    kind,
                    mime_type,
                    filename,
                    caption,
                    size_bytes,
                    width,
                    height,
                    duration_ms,
                    sha256,
                    bridge_media_ref,
                    download_url,
                    path,
                    ingest_state,
                    parse_task_id,
                    parse_bundle_id,
                    parsed_summary_json,
                    metadata_json,
                    created_at
                FROM inbound_artifacts
                WHERE session_id = ?
                ORDER BY created_at DESC, artifact_id DESC
                LIMIT ?
                """,
                (session_id, normalized_limit),
            ).fetchall()
        return self._deserialize_rows(rows, request_id=None, session_id=session_id)

    def get(self, artifact_id: str) -> dict[str, Any] | None:
        if not artifact_id:
            return None
        with self._lock, self._connect() as connection:
            row = self._get_artifact_row(connection, "inbound_artifacts", artifact_id)
            if row is None:
                row = self._get_artifact_row(connection, "output_artifacts", artifact_id)
        if row is None:
            return None
        records = self._deserialize_rows([row], request_id=row["request_id"], session_id=row["session_id"])
        return records[0] if records else None

    def _get_artifact_row(
        self,
        connection: sqlite3.Connection,
        table_name: str,
        artifact_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            f"""
            SELECT
                artifact_id,
                request_id,
                session_id,
                source_channel,
                source_platform,
                source_message_id,
                kind,
                mime_type,
                filename,
                caption,
                size_bytes,
                width,
                height,
                duration_ms,
                sha256,
                bridge_media_ref,
                download_url,
                path,
                ingest_state,
                parse_task_id,
                parse_bundle_id,
                parsed_summary_json,
                metadata_json,
                created_at
            FROM {table_name}
            WHERE artifact_id = ?
            LIMIT 1
            """,
            (artifact_id,),
        ).fetchone()

    def _deserialize_rows(
        self,
        rows: list[sqlite3.Row],
        *,
        request_id: str | None,
        session_id: str | None,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in rows:
            if row["metadata_json"]:
                try:
                    metadata = json.loads(row["metadata_json"])
                except json.JSONDecodeError:
                    logger.exception(
                        "artifact_store.metadata_parse_failed request_id=%s session_id=%s artifact_id=%s",
                        request_id,
                        session_id,
                        row["artifact_id"],
                    )
                    metadata = None
            else:
                metadata = None
            if row["parsed_summary_json"]:
                try:
                    parsed_summary = json.loads(row["parsed_summary_json"])
                except json.JSONDecodeError:
                    logger.exception(
                        "artifact_store.parsed_summary_parse_failed request_id=%s session_id=%s artifact_id=%s",
                        request_id,
                        session_id,
                        row["artifact_id"],
                    )
                    parsed_summary = None
            else:
                parsed_summary = None
            result.append(
                {
                    "artifact_id": row["artifact_id"],
                    "request_id": row["request_id"],
                    "session_id": row["session_id"],
                    "source_channel": row["source_channel"],
                    "source_platform": row["source_platform"],
                    "source_message_id": row["source_message_id"],
                    "kind": row["kind"],
                    "mime": row["mime_type"],
                    "filename": row["filename"],
                    "caption": row["caption"],
                    "size_bytes": row["size_bytes"],
                    "width": row["width"],
                    "height": row["height"],
                    "duration_ms": row["duration_ms"],
                    "sha256": row["sha256"],
                    "bridge_media_ref": row["bridge_media_ref"],
                    "download_url": row["download_url"],
                    "path": row["path"],
                    "ingest_state": row["ingest_state"],
                    "parse_task_id": row["parse_task_id"],
                    "parse_bundle_id": row["parse_bundle_id"],
                    "parsed_summary": parsed_summary,
                    "metadata": metadata,
                    "created_at": row["created_at"],
                }
            )
        return result

    def update_ingest_state(
        self,
        artifact_id: str,
        *,
        sha256: str | None = None,
        path: str | None = None,
        ingest_state: str | None = None,
        parse_task_id: str | None = None,
        parse_bundle_id: str | None = None,
        parsed_summary: dict[str, Any] | None = None,
    ) -> None:
        if not artifact_id:
            return
        parsed_summary_json = json.dumps(parsed_summary, ensure_ascii=False) if parsed_summary is not None else None
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE inbound_artifacts
                SET
                    sha256 = COALESCE(?, sha256),
                    path = COALESCE(?, path),
                    ingest_state = COALESCE(?, ingest_state),
                    parse_task_id = COALESCE(?, parse_task_id),
                    parse_bundle_id = COALESCE(?, parse_bundle_id),
                    parsed_summary_json = COALESCE(?, parsed_summary_json)
                WHERE artifact_id = ?
                """,
                (
                    self._normalize_text(sha256),
                    self._normalize_text(path),
                    self._normalize_text(ingest_state),
                    self._normalize_text(parse_task_id),
                    self._normalize_text(parse_bundle_id),
                    parsed_summary_json,
                    artifact_id,
                ),
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_column(self, connection: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {str(row["name"]) for row in rows}
        if column in existing:
            return
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")

    def _normalize_text(self, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _normalize_int(self, value: Any) -> int | None:
        try:
            if value is None:
                return None
            return int(value)
        except (TypeError, ValueError):
            return None
