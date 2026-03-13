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
    """SQLite-backed store for inbound attachment metadata.

    The store persists adapter-normalized attachment references so the Gateway
    can pass typed manifests into downstream orchestration without depending on
    a specific channel implementation.
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
                    metadata_json TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_inbound_artifacts_request
                    ON inbound_artifacts(request_id, created_at);

                CREATE INDEX IF NOT EXISTS idx_inbound_artifacts_session
                    ON inbound_artifacts(session_id, created_at);
                """
            )
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
                        metadata_json,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        "ingest_state": "bridge_reference",
                        "path": None,
                        "task_id": None,
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
                    metadata_json,
                    created_at
                FROM inbound_artifacts
                WHERE request_id = ?
                ORDER BY created_at ASC, artifact_id ASC
                """,
                (request_id,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            if row["metadata_json"]:
                try:
                    metadata = json.loads(row["metadata_json"])
                except json.JSONDecodeError:
                    logger.exception(
                        "artifact_store.metadata_parse_failed request_id=%s artifact_id=%s",
                        request_id,
                        row["artifact_id"],
                    )
                    metadata = None
            else:
                metadata = None
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
                    "metadata": metadata,
                    "created_at": row["created_at"],
                }
            )
        return result

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

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
