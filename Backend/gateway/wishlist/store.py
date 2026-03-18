from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any


def _json_dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _json_load_list(value: Any) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    results: list[str] = []
    for item in parsed:
        normalized = str(item or "").strip()
        if normalized:
            results.append(normalized)
    return results


class CapabilityWishlistStore:
    """Gateway-owned canonical SQLite store for COSMIC's capability wishlist."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS wishlist_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS wishlist_items (
                    capability_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    normalized_title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    desired_outcome TEXT,
                    domain TEXT NOT NULL DEFAULT 'general',
                    status TEXT NOT NULL DEFAULT 'candidate',
                    priority TEXT NOT NULL DEFAULT 'normal',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    aliases_json TEXT NOT NULL DEFAULT '[]',
                    canonical_fingerprint TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    created_by TEXT,
                    updated_by TEXT,
                    evidence_count INTEGER NOT NULL DEFAULT 0,
                    embedding_model TEXT,
                    embedding_dimensions INTEGER,
                    embedding_vector_json TEXT,
                    embedding_updated_at TEXT,
                    last_adjudication_mode TEXT,
                    last_adjudicated_at TEXT,
                    metadata_json TEXT
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_wishlist_fingerprint
                    ON wishlist_items(canonical_fingerprint);

                CREATE INDEX IF NOT EXISTS idx_wishlist_updated
                    ON wishlist_items(updated_at DESC, capability_id DESC);

                CREATE INDEX IF NOT EXISTS idx_wishlist_domain
                    ON wishlist_items(domain, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_wishlist_title
                    ON wishlist_items(normalized_title, updated_at DESC);

                CREATE TABLE IF NOT EXISTS wishlist_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    capability_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    source_component TEXT,
                    source_id TEXT,
                    request_id TEXT,
                    session_id TEXT,
                    task_id TEXT,
                    route TEXT,
                    title TEXT,
                    summary TEXT,
                    desired_outcome TEXT,
                    domain TEXT,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    evidence_text TEXT,
                    decision TEXT,
                    metadata_json TEXT,
                    FOREIGN KEY(capability_id) REFERENCES wishlist_items(capability_id)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_wishlist_evidence_hash
                    ON wishlist_evidence(capability_id, content_hash);

                CREATE INDEX IF NOT EXISTS idx_wishlist_evidence_capability
                    ON wishlist_evidence(capability_id, captured_at DESC);

                CREATE VIRTUAL TABLE IF NOT EXISTS wishlist_items_fts USING fts5(
                    capability_id UNINDEXED,
                    title,
                    summary,
                    desired_outcome,
                    domain,
                    tags_text,
                    aliases_text,
                    tokenize='unicode61 remove_diacritics 2'
                );
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO wishlist_meta(key, value)
                VALUES ('next_capability_sequence', '1')
                """
            )
            connection.commit()

    def summary(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS total_items,
                    MAX(updated_at) AS latest_updated_at,
                    SUM(CASE WHEN embedding_vector_json IS NOT NULL THEN 1 ELSE 0 END) AS embedded_items
                FROM wishlist_items
                """
            ).fetchone()
        return {
            "db_path": str(self.db_path),
            "total_items": int(row["total_items"] if row and row["total_items"] is not None else 0),
            "embedded_items": int(row["embedded_items"] if row and row["embedded_items"] is not None else 0),
            "latest_updated_at": row["latest_updated_at"] if row else None,
        }

    def list_items(self, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                self._select_items_sql(limit_clause=True),
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [self._row_to_item(row) for row in rows]

    def get_item(self, capability_id: str) -> dict[str, Any] | None:
        normalized_capability_id = str(capability_id or "").strip()
        if not normalized_capability_id:
            return None
        with self._lock, self._connect() as connection:
            row = connection.execute(
                self._select_items_sql(where_clause="WHERE i.capability_id = ?"),
                (normalized_capability_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_item(row)

    def search_lexical(self, query: str, *, limit: int = 8) -> list[dict[str, Any]]:
        normalized_query = self._fts_query(query)
        if not normalized_query:
            return []
        sql = """
            SELECT
                i.*,
                bm25(wishlist_items_fts) AS lexical_bm25,
                le.evidence_text AS latest_evidence_text,
                le.captured_at AS latest_evidence_at
            FROM wishlist_items_fts
            JOIN wishlist_items AS i ON i.capability_id = wishlist_items_fts.capability_id
            LEFT JOIN wishlist_evidence AS le
                ON le.evidence_id = (
                    SELECT evidence_id
                    FROM wishlist_evidence
                    WHERE capability_id = i.capability_id
                    ORDER BY captured_at DESC, evidence_id DESC
                    LIMIT 1
                )
            WHERE wishlist_items_fts MATCH ?
            ORDER BY bm25(wishlist_items_fts), i.updated_at DESC, i.capability_id DESC
            LIMIT ?
        """
        with self._lock, self._connect() as connection:
            try:
                rows = connection.execute(sql, (normalized_query, max(1, min(int(limit), 50)))).fetchall()
            except sqlite3.OperationalError:
                return self._search_lexical_fallback(query, limit=limit)
        return [self._row_to_item(row) for row in rows]

    def list_embedding_items(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    i.*,
                    le.evidence_text AS latest_evidence_text,
                    le.captured_at AS latest_evidence_at
                FROM wishlist_items AS i
                LEFT JOIN wishlist_evidence AS le
                    ON le.evidence_id = (
                        SELECT evidence_id
                        FROM wishlist_evidence
                        WHERE capability_id = i.capability_id
                        ORDER BY captured_at DESC, evidence_id DESC
                        LIMIT 1
                    )
                WHERE i.embedding_vector_json IS NOT NULL
                ORDER BY i.updated_at DESC, i.capability_id DESC
                """
            ).fetchall()
        return [self._row_to_item(row) for row in rows]

    def create_item(
        self,
        *,
        title: str,
        normalized_title: str,
        summary: str,
        desired_outcome: str | None,
        domain: str,
        tags: list[str],
        aliases: list[str],
        canonical_fingerprint: str,
        created_by: str | None,
        embedding_model: str | None,
        embedding_dimensions: int | None,
        embedding_vector: list[float] | None,
        adjudication_mode: str,
        evidence_event: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            capability_id = self._allocate_capability_id(connection)
            now = str(evidence_event.get("captured_at") or "")
            connection.execute(
                """
                INSERT INTO wishlist_items (
                    capability_id,
                    title,
                    normalized_title,
                    summary,
                    desired_outcome,
                    domain,
                    status,
                    priority,
                    tags_json,
                    aliases_json,
                    canonical_fingerprint,
                    created_at,
                    updated_at,
                    last_seen_at,
                    created_by,
                    updated_by,
                    evidence_count,
                    embedding_model,
                    embedding_dimensions,
                    embedding_vector_json,
                    embedding_updated_at,
                    last_adjudication_mode,
                    last_adjudicated_at,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, 'candidate', 'normal', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    capability_id,
                    title,
                    normalized_title,
                    summary,
                    desired_outcome,
                    domain,
                    _json_dumps(tags) or "[]",
                    _json_dumps(aliases) or "[]",
                    canonical_fingerprint,
                    now,
                    now,
                    now,
                    created_by,
                    created_by,
                    1,
                    embedding_model,
                    embedding_dimensions,
                    _json_dumps(embedding_vector),
                    now if embedding_vector is not None else None,
                    adjudication_mode,
                    now,
                    _json_dumps(metadata) if metadata else None,
                ),
            )
            self._upsert_fts(
                connection,
                capability_id=capability_id,
                title=title,
                summary=summary,
                desired_outcome=desired_outcome,
                domain=domain,
                tags=tags,
                aliases=aliases,
            )
            self._append_evidence(connection, capability_id=capability_id, event=evidence_event)
            row = connection.execute(
                self._select_items_sql(where_clause="WHERE i.capability_id = ?"),
                (capability_id,),
            ).fetchone()
            connection.commit()
        if row is None:
            raise RuntimeError("Failed to load created capability wishlist item.")
        return self._row_to_item(row)

    def update_item(
        self,
        capability_id: str,
        *,
        title: str,
        normalized_title: str,
        summary: str,
        desired_outcome: str | None,
        domain: str,
        tags: list[str],
        aliases: list[str],
        canonical_fingerprint: str,
        updated_by: str | None,
        embedding_model: str | None,
        embedding_dimensions: int | None,
        embedding_vector: list[float] | None,
        adjudication_mode: str,
        evidence_event: dict[str, Any] | None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        normalized_capability_id = str(capability_id or "").strip()
        if not normalized_capability_id:
            raise ValueError("capability_id is required")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT evidence_count FROM wishlist_items WHERE capability_id = ?",
                (normalized_capability_id,),
            ).fetchone()
            if existing is None:
                raise KeyError(normalized_capability_id)
            now = str((evidence_event or {}).get("captured_at") or "")
            evidence_inserted = False
            if evidence_event is not None:
                evidence_inserted = self._append_evidence(
                    connection,
                    capability_id=normalized_capability_id,
                    event=evidence_event,
                )
            evidence_count = int(existing["evidence_count"] or 0) + (1 if evidence_inserted else 0)
            connection.execute(
                """
                UPDATE wishlist_items
                SET
                    title = ?,
                    normalized_title = ?,
                    summary = ?,
                    desired_outcome = ?,
                    domain = ?,
                    tags_json = ?,
                    aliases_json = ?,
                    canonical_fingerprint = ?,
                    updated_at = ?,
                    last_seen_at = ?,
                    updated_by = ?,
                    evidence_count = ?,
                    embedding_model = ?,
                    embedding_dimensions = ?,
                    embedding_vector_json = ?,
                    embedding_updated_at = ?,
                    last_adjudication_mode = ?,
                    last_adjudicated_at = ?,
                    metadata_json = ?
                WHERE capability_id = ?
                """,
                (
                    title,
                    normalized_title,
                    summary,
                    desired_outcome,
                    domain,
                    _json_dumps(tags) or "[]",
                    _json_dumps(aliases) or "[]",
                    canonical_fingerprint,
                    now,
                    now,
                    updated_by,
                    evidence_count,
                    embedding_model,
                    embedding_dimensions,
                    _json_dumps(embedding_vector) if embedding_vector is not None else None,
                    now if embedding_vector is not None else None,
                    adjudication_mode,
                    now,
                    _json_dumps(metadata) if metadata else None,
                    normalized_capability_id,
                ),
            )
            self._upsert_fts(
                connection,
                capability_id=normalized_capability_id,
                title=title,
                summary=summary,
                desired_outcome=desired_outcome,
                domain=domain,
                tags=tags,
                aliases=aliases,
            )
            row = connection.execute(
                self._select_items_sql(where_clause="WHERE i.capability_id = ?"),
                (normalized_capability_id,),
            ).fetchone()
            connection.commit()
        if row is None:
            raise RuntimeError("Failed to load updated capability wishlist item.")
        return self._row_to_item(row), evidence_inserted

    def append_evidence_only(
        self,
        capability_id: str,
        *,
        updated_by: str | None,
        adjudication_mode: str,
        event: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        normalized_capability_id = str(capability_id or "").strip()
        if not normalized_capability_id:
            raise ValueError("capability_id is required")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT evidence_count, updated_at FROM wishlist_items WHERE capability_id = ?",
                (normalized_capability_id,),
            ).fetchone()
            if existing is None:
                raise KeyError(normalized_capability_id)
            inserted = self._append_evidence(connection, capability_id=normalized_capability_id, event=event)
            now = str(event.get("captured_at") or "")
            evidence_count = int(existing["evidence_count"] or 0) + (1 if inserted else 0)
            connection.execute(
                """
                UPDATE wishlist_items
                SET
                    last_seen_at = ?,
                    updated_at = ?,
                    updated_by = ?,
                    evidence_count = ?,
                    last_adjudication_mode = ?,
                    last_adjudicated_at = ?
                WHERE capability_id = ?
                """,
                (
                    now,
                    now if inserted else existing["updated_at"],
                    updated_by,
                    evidence_count,
                    adjudication_mode,
                    now,
                    normalized_capability_id,
                ),
            )
            row = connection.execute(
                self._select_items_sql(where_clause="WHERE i.capability_id = ?"),
                (normalized_capability_id,),
            ).fetchone()
            connection.commit()
        if row is None:
            raise RuntimeError("Failed to load capability wishlist item after evidence append.")
        return self._row_to_item(row), inserted

    def update_item_embedding(
        self,
        capability_id: str,
        *,
        embedding_model: str | None,
        embedding_dimensions: int | None,
        embedding_vector: list[float],
        embedding_updated_at: str,
    ) -> dict[str, Any]:
        normalized_capability_id = str(capability_id or "").strip()
        if not normalized_capability_id:
            raise ValueError("capability_id is required")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT 1 FROM wishlist_items WHERE capability_id = ?",
                (normalized_capability_id,),
            ).fetchone()
            if existing is None:
                raise KeyError(normalized_capability_id)
            connection.execute(
                """
                UPDATE wishlist_items
                SET
                    embedding_model = ?,
                    embedding_dimensions = ?,
                    embedding_vector_json = ?,
                    embedding_updated_at = ?
                WHERE capability_id = ?
                """,
                (
                    embedding_model,
                    embedding_dimensions,
                    _json_dumps(embedding_vector),
                    embedding_updated_at,
                    normalized_capability_id,
                ),
            )
            row = connection.execute(
                self._select_items_sql(where_clause="WHERE i.capability_id = ?"),
                (normalized_capability_id,),
            ).fetchone()
            connection.commit()
        if row is None:
            raise RuntimeError("Failed to load capability wishlist item after embedding update.")
        return self._row_to_item(row)

    def touch_duplicate(
        self,
        capability_id: str,
        *,
        updated_by: str | None,
        adjudication_mode: str,
        seen_at: str,
    ) -> dict[str, Any]:
        normalized_capability_id = str(capability_id or "").strip()
        if not normalized_capability_id:
            raise ValueError("capability_id is required")
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE wishlist_items
                SET
                    last_seen_at = ?,
                    updated_by = ?,
                    last_adjudication_mode = ?,
                    last_adjudicated_at = ?
                WHERE capability_id = ?
                """,
                (
                    seen_at,
                    updated_by,
                    adjudication_mode,
                    seen_at,
                    normalized_capability_id,
                ),
            )
            connection.commit()
            row = connection.execute(
                self._select_items_sql(where_clause="WHERE i.capability_id = ?"),
                (normalized_capability_id,),
            ).fetchone()
        if row is None:
            raise KeyError(normalized_capability_id)
        return self._row_to_item(row)

    def _append_evidence(
        self,
        connection: sqlite3.Connection,
        *,
        capability_id: str,
        event: dict[str, Any],
    ) -> bool:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO wishlist_evidence (
                evidence_id,
                capability_id,
                content_hash,
                captured_at,
                source_component,
                source_id,
                request_id,
                session_id,
                task_id,
                route,
                title,
                summary,
                desired_outcome,
                domain,
                tags_json,
                evidence_text,
                decision,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.get("evidence_id"),
                capability_id,
                event.get("content_hash"),
                event.get("captured_at"),
                event.get("source_component"),
                event.get("source_id"),
                event.get("request_id"),
                event.get("session_id"),
                event.get("task_id"),
                event.get("route"),
                event.get("title"),
                event.get("summary"),
                event.get("desired_outcome"),
                event.get("domain"),
                _json_dumps(event.get("tags") or []) or "[]",
                event.get("evidence_text"),
                event.get("decision"),
                _json_dumps(event.get("metadata")) if isinstance(event.get("metadata"), dict) else None,
            ),
        )
        return cursor.rowcount == 1

    def _allocate_capability_id(self, connection: sqlite3.Connection) -> str:
        row = connection.execute(
            "SELECT value FROM wishlist_meta WHERE key = 'next_capability_sequence'"
        ).fetchone()
        next_sequence = int(row["value"] if row and row["value"] is not None else 1)
        connection.execute(
            """
            INSERT INTO wishlist_meta(key, value)
            VALUES ('next_capability_sequence', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(next_sequence + 1),),
        )
        return f"cap_{next_sequence:06d}"

    def _upsert_fts(
        self,
        connection: sqlite3.Connection,
        *,
        capability_id: str,
        title: str,
        summary: str,
        desired_outcome: str | None,
        domain: str,
        tags: list[str],
        aliases: list[str],
    ) -> None:
        connection.execute("DELETE FROM wishlist_items_fts WHERE capability_id = ?", (capability_id,))
        connection.execute(
            """
            INSERT INTO wishlist_items_fts (
                capability_id,
                title,
                summary,
                desired_outcome,
                domain,
                tags_text,
                aliases_text
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                capability_id,
                title,
                summary,
                desired_outcome or "",
                domain,
                " ".join(tags),
                " ".join(aliases),
            ),
        )

    def _select_items_sql(self, *, where_clause: str = "", limit_clause: bool = False) -> str:
        limit_fragment = "LIMIT ?" if limit_clause else ""
        return f"""
            SELECT
                i.*,
                le.evidence_text AS latest_evidence_text,
                le.captured_at AS latest_evidence_at
            FROM wishlist_items AS i
            LEFT JOIN wishlist_evidence AS le
                ON le.evidence_id = (
                    SELECT evidence_id
                    FROM wishlist_evidence
                    WHERE capability_id = i.capability_id
                    ORDER BY captured_at DESC, evidence_id DESC
                    LIMIT 1
                )
            {where_clause}
            ORDER BY i.updated_at DESC, i.capability_id DESC
            {limit_fragment}
        """

    def _search_lexical_fallback(self, query: str, *, limit: int) -> list[dict[str, Any]]:
        like_value = f"%{str(query or '').strip()}%"
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                self._select_items_sql(
                    where_clause="WHERE i.title LIKE ? OR i.summary LIKE ? OR i.desired_outcome LIKE ?"
                ),
                (like_value, like_value, like_value),
            ).fetchall()
        return [self._row_to_item(row) for row in rows[: max(1, min(int(limit), 50))]]

    def _row_to_item(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        payload["tags"] = _json_load_list(payload.pop("tags_json", None))
        payload["aliases"] = _json_load_list(payload.pop("aliases_json", None))
        vector_text = payload.pop("embedding_vector_json", None)
        if vector_text:
            try:
                payload["embedding_vector"] = json.loads(vector_text)
            except (TypeError, ValueError, json.JSONDecodeError):
                payload["embedding_vector"] = None
        else:
            payload["embedding_vector"] = None
        metadata_text = payload.pop("metadata_json", None)
        if metadata_text:
            try:
                payload["metadata"] = json.loads(metadata_text)
            except (TypeError, ValueError, json.JSONDecodeError):
                payload["metadata"] = None
        else:
            payload["metadata"] = None
        lexical_bm25 = payload.get("lexical_bm25")
        payload["lexical_bm25"] = float(lexical_bm25) if lexical_bm25 is not None else None
        payload["latest_evidence_text"] = str(payload.get("latest_evidence_text") or "").strip() or None
        payload["latest_evidence_at"] = str(payload.get("latest_evidence_at") or "").strip() or None
        return payload

    def _fts_query(self, query: str) -> str:
        import re

        tokens = [token for token in re.findall(r"[A-Za-z0-9_]+", str(query or "").lower()) if token]
        if not tokens:
            return ""
        return " ".join(f"{token}*" for token in tokens[:10])

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection
