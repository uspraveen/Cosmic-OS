from __future__ import annotations

import json

from gateway.session_store import SessionStore


def _insert_session_with_message(
    store: SessionStore,
    *,
    session_id: str,
    session_created_at: str,
    session_updated_at: str,
    message_created_at: str | None,
    content: str = "Hello from the session",
) -> None:
    with store._connect() as connection:  # noqa: SLF001 - direct store fixture setup
        connection.execute(
            """
            INSERT INTO sessions (session_id, user_id, created_at, updated_at, metadata_json)
            VALUES (?, 'local-user', ?, ?, ?)
            """,
            (session_id, session_created_at, session_updated_at, json.dumps({})),
        )
        if message_created_at is not None:
            connection.execute(
                """
                INSERT INTO messages (
                    message_id, session_id, role, content, route, request_id,
                    awaiting_reply, channel, created_at, metadata_json
                )
                VALUES (?, ?, 'user', ?, NULL, NULL, 0, 'desktop:test', ?, NULL)
                """,
                (f"msg_{session_id}", session_id, content, message_created_at),
            )
        connection.commit()


def test_list_sessions_orders_by_message_activity_and_skips_empty_rollover_rows(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    store.initialize()
    _insert_session_with_message(
        store,
        session_id="sess_20260403",
        session_created_at="2026-04-03T09:00:00Z",
        session_updated_at="2026-04-04T09:00:00Z",
        message_created_at="2026-04-03T16:10:00Z",
        content="Older Apr 3 work",
    )
    _insert_session_with_message(
        store,
        session_id="sess_20260404",
        session_created_at="2026-04-04T09:00:00Z",
        session_updated_at="2026-04-05T09:00:00Z",
        message_created_at=None,
    )
    _insert_session_with_message(
        store,
        session_id="sess_20260405",
        session_created_at="2026-04-05T09:00:00Z",
        session_updated_at="2026-04-06T09:00:00Z",
        message_created_at="2026-04-05T17:53:00Z",
        content="Newer Apr 5 work",
    )

    sessions = store.list_sessions(limit=10)

    assert [session["id"] for session in sessions] == ["sess_20260405", "sess_20260403"]
    assert sessions[0]["message_count"] == 1
    assert sessions[0]["first_message_at"] == "2026-04-05T17:53:00Z"
    assert sessions[0]["last_message_at"] == "2026-04-05T17:53:00Z"
