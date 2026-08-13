"""The desktop transcript must show a whole email thread, not just its start.

An inbound email reply is routed to its own `email-thread:<mailbox>:<id>`
session so the orchestrator answers with thread-scoped context. The desktop
loads exactly one session, so a thread rendered its opening message and
nothing else - Cosmic emailed, the user replied, Cosmic answered, and only the
first of the three was ever visible.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from gateway.runtime import GatewayRuntime
from gateway.session_store import SessionStore

DAY = "sess_20260805"
THREAD = "email-thread:iamcosmic001@mail.thelearnchain.com:thr_spc"
OTHER_THREAD = "email-thread:iamcosmic001@mail.thelearnchain.com:thr_dmv"


@pytest.fixture
def store(tmp_path) -> SessionStore:
    store = SessionStore(tmp_path / "sessions.db")
    store.initialize()
    return store


def _seed(store: SessionStore, session_id: str, role: str, content: str, created_at: str, metadata=None):
    message_id = store.append_message(
        session_id,
        role=role,
        content=content,
        channel="agent-email:iamcosmic001@mail.thelearnchain.com",
        metadata=metadata or {},
    )
    # append_message stamps wall-clock times. Force the ones the test needs,
    # including the session's own start, which bounds the merge window.
    with store._lock, store._connect() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE messages SET created_at = ? WHERE message_id = ?",
            (created_at, message_id),
        )
        connection.execute(
            "UPDATE sessions SET created_at = ("
            "  SELECT MIN(created_at) FROM messages WHERE session_id = ?"
            ") WHERE session_id = ?",
            (session_id, session_id),
        )
        connection.commit()


def _runtime(store: SessionStore) -> GatewayRuntime:
    runtime = GatewayRuntime.__new__(GatewayRuntime)
    runtime.session_store = store
    return runtime


def test_thread_messages_are_merged_into_the_day_transcript(store) -> None:
    _seed(store, DAY, "assistant", "Vinai just replied about SPC", "2026-08-05T18:51:00Z")
    _seed(store, THREAD, "user", "Who is Gopal?", "2026-08-05T19:26:00Z", {"subject": "Re: Vinai just replied about SPC"})
    _seed(store, THREAD, "assistant", "Here's Gopal Raman's profile", "2026-08-05T19:26:30Z")

    messages = _runtime(store).get_session_history_for_client(DAY)

    contents = [m["content"] for m in messages]
    assert contents == [
        "Vinai just replied about SPC",
        "Who is Gopal?",
        "Here's Gopal Raman's profile",
    ], "the reply and the answer must both be visible, in chronological order"

    thread_ids = [m.get("email_thread_id") for m in messages]
    assert thread_ids == [None, THREAD, THREAD]
    assert messages[1]["email_thread_subject"] == "Re: Vinai just replied about SPC"
    # A subject only arrives on the inbound message; the whole thread needs it.
    assert messages[2]["email_thread_subject"] == "Re: Vinai just replied about SPC"


def test_separate_threads_stay_separately_identified(store) -> None:
    _seed(store, DAY, "assistant", "day message", "2026-08-05T10:00:00Z")
    _seed(store, THREAD, "user", "spc reply", "2026-08-05T11:00:00Z", {"subject": "Re: SPC"})
    _seed(store, OTHER_THREAD, "user", "dmv reply", "2026-08-05T12:00:00Z", {"subject": "Re: DMV"})

    messages = _runtime(store).get_session_history_for_client(DAY)
    by_content = {m["content"]: m.get("email_thread_id") for m in messages}
    assert by_content["spc reply"] == THREAD
    assert by_content["dmv reply"] == OTHER_THREAD
    assert by_content["day message"] is None


def test_thread_activity_lands_only_in_the_day_it_happened(store) -> None:
    """Without a window, a long-running thread would be re-injected into every
    later transcript."""
    _seed(store, "sess_20260804", "assistant", "yesterday", "2026-08-04T10:00:00Z")
    _seed(store, THREAD, "user", "reply on the 4th", "2026-08-04T11:00:00Z")
    _seed(store, DAY, "assistant", "today", "2026-08-05T10:00:00Z")
    _seed(store, THREAD, "user", "reply on the 5th", "2026-08-05T11:00:00Z")

    runtime = _runtime(store)
    yesterday = [m["content"] for m in runtime.get_session_history_for_client("sess_20260804")]
    today = [m["content"] for m in runtime.get_session_history_for_client(DAY)]

    assert yesterday == ["yesterday", "reply on the 4th"]
    assert today == ["today", "reply on the 5th"]


def test_viewing_a_thread_session_directly_is_not_double_merged(store) -> None:
    _seed(store, THREAD, "user", "reply", "2026-08-05T11:00:00Z")
    _seed(store, THREAD, "assistant", "answer", "2026-08-05T11:01:00Z")

    messages = _runtime(store).get_session_history_for_client(THREAD)
    assert [m["content"] for m in messages] == ["reply", "answer"]
    assert all("email_thread_id" not in m for m in messages)


def test_a_session_with_no_threads_is_returned_unchanged(store) -> None:
    _seed(store, DAY, "user", "hello", "2026-08-05T10:00:00Z")
    _seed(store, DAY, "assistant", "hi", "2026-08-05T10:00:05Z")

    messages = _runtime(store).get_session_history_for_client(DAY)
    assert [m["content"] for m in messages] == ["hello", "hi"]
    assert all(m.get("email_thread_id") is None for m in messages)


def test_a_failing_thread_lookup_never_hides_the_users_own_session(store, monkeypatch) -> None:
    _seed(store, DAY, "user", "hello", "2026-08-05T10:00:00Z")
    runtime = _runtime(store)

    def boom(**kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(store, "list_email_thread_messages_in_window", boom)

    messages = runtime.get_session_history_for_client(DAY)
    assert [m["content"] for m in messages] == ["hello"]


def test_model_context_history_is_left_alone(store) -> None:
    """get_session_history feeds heartbeat prompts. Thread messages must not
    leak into it - that isolation is why threads get their own session."""
    _seed(store, DAY, "assistant", "day message", "2026-08-05T10:00:00Z")
    _seed(store, THREAD, "user", "thread reply", "2026-08-05T11:00:00Z")

    assert [m["content"] for m in _runtime(store).get_session_history(DAY)] == ["day message"]


def test_daily_rollover_transcript_includes_email_thread_turns(store) -> None:
    _seed(store, DAY, "assistant", "Heads up — coprlab.com just failed DNS", "2026-08-05T19:01:00Z")
    _seed(
        store,
        THREAD,
        "user",
        "It's not coprlab.com but copprlab.com",
        "2026-08-05T19:21:00Z",
        {"subject": "Re: Heads up"},
    )
    _seed(store, THREAD, "assistant", "You're right — copprlab.com is live.", "2026-08-05T19:22:00Z")

    markdown = _runtime(store)._render_session_transcript_markdown(
        {
            "session_id": DAY,
            "created_at": "2026-08-05T09:00:00Z",
            "updated_at": "2026-08-05T20:00:00Z",
        },
        store.get_history(DAY),
    )

    assert "Heads up — coprlab.com just failed DNS" in markdown
    assert "Email threads from this calendar day" in markdown
    assert "It's not coprlab.com but copprlab.com" in markdown
    assert "You're right — copprlab.com is live." in markdown


def test_email_thread_sessions_are_not_themselves_rolled_over(store) -> None:
    _seed(store, THREAD, "user", "It's not coprlab.com but copprlab.com", "2026-08-05T19:21:00Z")
    store.update_session_metadata(
        THREAD, {"session_scope": "email_thread", "rollover_exempt": True}
    )
    markdown = _runtime(store)._render_session_transcript_markdown(
        {
            "session_id": THREAD,
            "created_at": "2026-08-05T19:21:00Z",
            "updated_at": "2026-08-05T19:22:00Z",
        },
        store.get_history(THREAD),
    )
    assert "Email threads from this calendar day" not in markdown
    candidates = store.list_rollover_candidates(current_session_id="sess_20260806")
    assert THREAD not in [item["session_id"] for item in candidates]
