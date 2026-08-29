"""Email threads must produce the same recallable summary day sessions get.

An email-thread session is rollover_exempt, so the daily rollover never
summarized it: a thread's turns existed only as raw transcripts, which passive
recall does not search. The idle-thread summarizer gives email activity the
same distilled, recallable session_summary the daily session already gets -
without touching the session isolation itself.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from gateway.runtime import GatewayRuntime
from gateway.session_store import SessionStore

THREAD = "email-thread:iamcosmic001@mail.thelearnchain.com:thr_parag"
OTHER_THREAD = "email-thread:iamcosmic001@mail.thelearnchain.com:thr_other"
DAY_SESSION = "sess_20260829"


def _pin_updated_at(store: SessionStore, session_id: str, *, hours_ago: float) -> str:
    """append_message stamps wall-clock times; pin updated_at for determinism."""
    pinned = (
        (datetime.now(timezone.utc) - timedelta(hours=hours_ago))
        .isoformat()
        .replace("+00:00", "Z")
    )
    with store._lock, store._connect() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
            (pinned, session_id),
        )
        connection.commit()
    return pinned


def _seed_thread(store: SessionStore, thread_id: str, *, hours_ago: float = 8.0) -> str:
    """A two-turn thread whose updated_at sits hours in the past."""
    store.append_message(
        thread_id,
        role="user",
        content="Listen to this: https://youtu.be/fUcnE6pjq5w",
        channel="agent-email:iamcosmic001@mail.thelearnchain.com",
        metadata={"subject": "Parallel"},
    )
    store.append_message(
        thread_id,
        role="assistant",
        content="This is the Sequoia Training Data podcast with Parag Agrawal.",
        channel="agent-email:iamcosmic001@mail.thelearnchain.com",
        metadata={},
    )
    return _pin_updated_at(store, thread_id, hours_ago=hours_ago)


def _candidates(store: SessionStore, *, idle_minutes: int = 360) -> list:
    return store.list_email_thread_summary_candidates(
        idle_minutes=idle_minutes,
        claim_stale_sec=1_800,
        max_attempts=3,
        backoff_sec=1_800,
    )


class _MemoryClient:
    def __init__(self):
        self.enabled = True
        self.written: list[dict] = []

    async def write_memory(self, payload):
        self.written.append(payload)
        return {"memory_id": "mem_thread_summary_1"}


def _runtime(
    store: SessionStore,
    *,
    tmp_path: Path,
    summary_text: str | None = "A quiet thread about the Parag video.",
    fail_summary: bool = False,
) -> GatewayRuntime:
    runtime = GatewayRuntime.__new__(GatewayRuntime)
    runtime.session_store = store  # type: ignore[attr-defined]
    runtime._email_thread_summary_lock = asyncio.Lock()  # type: ignore[attr-defined]
    runtime.memory_client = _MemoryClient()  # type: ignore[attr-defined]
    runtime.config = SimpleNamespace(
        session_transcript_dir=tmp_path / "transcripts",
        session_summary_max_output_tokens=2500,
    )

    async def _fake_summarize(**kwargs):
        if fail_summary:
            raise RuntimeError("haiku down")
        return summary_text

    runtime._summarize_completed_session = _fake_summarize  # type: ignore[method-assign]

    async def _fake_write(*, payload, audit_event, writer_id=None):
        runtime.memory_client.written.append(payload)
        return {"memory_id": "mem_thread_1"}

    runtime._write_memory_record = _fake_write  # type: ignore[method-assign]
    return runtime


def _thread_summary_state(store: SessionStore, session_id: str) -> dict:
    metadata = store.get_session_metadata(session_id)
    state = metadata.get(SessionStore.EMAIL_THREAD_SUMMARY_STATE_KEY)
    return state if isinstance(state, dict) else {}


# ---------------------------------------------------------------------------
# Candidacy, claims, attempts (session_store).
# ---------------------------------------------------------------------------


def test_idle_thread_is_a_candidate(store) -> None:
    _seed_thread(store, THREAD)
    candidates = _candidates(store)
    assert [c["session_id"] for c in candidates] == [THREAD]


def test_fresh_thread_is_not_a_candidate(store) -> None:
    store.append_message(
        THREAD,
        role="user",
        content="Listen to this: https://youtu.be/fUcnE6pjq5w",
        channel="agent-email:iamcosmic001@mail.thelearnchain.com",
        metadata={"subject": "Parallel"},
    )
    assert _candidates(store) == []


def test_recently_active_thread_is_not_a_candidate(store) -> None:
    _seed_thread(store, THREAD, hours_ago=0.5)
    assert _candidates(store) == []


def test_day_sessions_are_never_candidates(store) -> None:
    store.append_message(
        DAY_SESSION,
        role="user",
        content="desktop chatter",
        channel="desktop:desk_abc",
        metadata={},
    )
    _pin_updated_at(store, DAY_SESSION, hours_ago=48.0)
    assert _candidates(store) == []


def test_claim_suppresses_a_second_worker_until_stale(store) -> None:
    updated_at = _seed_thread(store, THREAD)
    assert _candidates(store)
    assert store.claim_email_thread_summary(THREAD, updated_at=updated_at)
    assert not store.claim_email_thread_summary(THREAD, updated_at=updated_at)


def test_claim_rejects_stale_candidacy(store) -> None:
    updated_at = _seed_thread(store, THREAD)
    assert store.claim_email_thread_summary(THREAD, updated_at=updated_at)
    # A pass that computed candidacy from older content must not proceed.
    assert not store.claim_email_thread_summary(THREAD, updated_at="stale")


def test_failures_back_off_then_stop(store) -> None:
    _seed_thread(store, THREAD)
    store.mark_email_thread_summary(THREAD, status="failed")
    # Immediate retry is suppressed by the attempts-scaled backoff.
    assert _candidates(store) == []
    # ...but the attempt is on record.
    state = _thread_summary_state(store, THREAD)
    assert state["attempts"] == 1
    assert state["status"] == "failed"


def test_max_attempts_stop_the_thread_until_new_messages(store) -> None:
    _seed_thread(store, THREAD)
    store.mark_email_thread_summary(THREAD, status="failed")
    store.mark_email_thread_summary(THREAD, status="failed")
    store.mark_email_thread_summary(THREAD, status="failed")
    # Three failed attempts on this content: leave it alone.
    assert _candidates(store) == []
    # A new message moves updated_at and reopens candidacy -- but only once
    # the thread goes quiet again, exactly like a first summary.
    store.append_message(
        THREAD,
        role="user",
        content="Also read this one",
        channel="agent-email:iamcosmic001@mail.thelearnchain.com",
        metadata={},
    )
    assert _candidates(store) == [], "a fresh message means the thread is live again"
    _pin_updated_at(store, THREAD, hours_ago=8.0)
    assert [c["session_id"] for c in _candidates(store)] == [THREAD]


def test_new_activity_after_summary_reopens_candidacy(store) -> None:
    updated_at = _seed_thread(store, THREAD)
    assert store.claim_email_thread_summary(THREAD, updated_at=updated_at)
    store.mark_email_thread_summary(
        THREAD, status="stored", memory_id="mem_1", summary_text="done"
    )
    assert _candidates(store) == []
    store.append_message(
        THREAD,
        role="user",
        content="One more thought on Parag",
        channel="agent-email:iamcosmic001@mail.thelearnchain.com",
        metadata={},
    )
    assert _candidates(store) == [], "a fresh message means the thread is live again"
    _pin_updated_at(store, THREAD, hours_ago=8.0)
    assert _candidates(store), "post-summary activity must reopen candidacy"


# ---------------------------------------------------------------------------
# The summarization pass itself (runtime).
# ---------------------------------------------------------------------------


def test_idle_thread_summary_is_written_to_memory(store, tmp_path) -> None:
    updated_at = _seed_thread(store, THREAD)
    runtime = _runtime(store, tmp_path=tmp_path)
    candidates = _candidates(store)
    assert len(candidates) == 1
    asyncio.run(runtime._summarize_single_email_thread(candidates[0]))

    written = runtime.memory_client.written
    assert len(written) == 1
    payload = written[0]
    assert payload["kind"] == "session_summary"
    assert "Parallel" in payload["title"]
    assert "Parag" in payload["content"]
    assert payload["metadata"]["session_scope"] == "email_thread"

    state = _thread_summary_state(store, THREAD)
    assert state["status"] == "stored"
    assert state["memory_id"] == "mem_thread_1"
    # The claim must be released once the outcome is recorded.
    assert state.get("state") != "in_progress"


def test_summarize_failure_records_the_attempt(store, tmp_path) -> None:
    _seed_thread(store, THREAD)
    runtime = _runtime(store, tmp_path=tmp_path, fail_summary=True)
    candidates = _candidates(store)
    asyncio.run(runtime._summarize_single_email_thread(candidates[0]))
    state = _thread_summary_state(store, THREAD)
    assert state["status"] == "failed"
    assert state["attempts"] == 1
    # No summary memory was written.
    assert not runtime.memory_client.written


def test_summary_memory_payload_shape(store, tmp_path) -> None:
    _seed_thread(store, THREAD)
    runtime = _runtime(store, tmp_path=tmp_path)
    payload = runtime._build_email_thread_summary_memory_payload(
        session_id=THREAD,
        transcript_path=str(tmp_path / "transcript.md"),
        summary_text="Summary of the thread.",
        history=store.get_history(THREAD),
    )
    assert payload["kind"] == "session_summary"
    assert payload["title"].startswith("Email thread summary:")
    assert payload["metadata"]["session_scope"] == "email_thread"
    assert "email_thread_summary" in payload["tags"]
    provenance = payload["provenance"]
    assert provenance["source_kind"] == "gateway_email_thread_summary"
    assert provenance["session_id"] == THREAD


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> SessionStore:
    store = SessionStore(tmp_path / "sessions.db")
    store.initialize()
    return store
