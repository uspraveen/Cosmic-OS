"""Tests for task-origin persistence, the dispatch-time anchor memory, and
interrupted-turn episode ingest."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from gateway.runtime import GatewayRuntime


def _runtime(**attrs) -> GatewayRuntime:
    runtime = GatewayRuntime.__new__(GatewayRuntime)
    for key, value in attrs.items():
        setattr(runtime, key, value)
    return runtime


FULL_ORIGINAL = (
    'Just created this public repo: "https://github.com/uspraveen/Jev-Reranker.git" '
    "Here is the tentative project plan: ... 26 sections ... "
    'BL_API_KEY=bl_aaab3tdwjry91djyu2mqhee1o9vbvnid BL_WORKSPACE=learnchain '
    "Send me email progress updates every 1 hr."
)


class _MessageStore:
    """Session store stub: one user message findable by request_id."""

    def __init__(self, messages=None, notebooks=None):
        self._messages = messages or {}
        self._notebooks = notebooks or {}
        self.upserts: list[tuple[str, str, dict]] = []

    def find_message_by_request_id(self, session_id, *, request_id, role):
        if role == "user":
            return self._messages.get(request_id)
        return None

    def get_task_notebook(self, task_id):
        return self._notebooks.get(task_id)

    def upsert_task_notebook(self, task_id, session_id, notebook):
        self.upserts.append((task_id, session_id, notebook))
        self._notebooks[task_id] = notebook

    def append_message(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("not expected in these tests")


def _event(event_type="task.accepted"):
    return {"type": event_type, "message": "working"}


def test_merge_captures_full_origin_once():
    store = _MessageStore(
        messages={"req_1": {"content": FULL_ORIGINAL, "role": "user"}}
    )
    runtime = _runtime(session_store=store)
    notebook = runtime._merge_task_notebook(
        task_id="tsk_1",
        session_id="sess_1",
        request_id="req_1",
        event=_event(),
    )
    # The bounded display goal stays bounded...
    assert len(notebook["goal"]) <= 280
    # ...but the origin is the full verbatim request with provenance.
    assert notebook["origin_query"] == " ".join(FULL_ORIGINAL.split())
    assert "BL_API_KEY" in notebook["origin_query"]
    assert notebook["origin_request_id"] == "req_1"
    assert notebook["origin_session_id"] == "sess_1"
    assert notebook["origin_anchored"] is False


def test_merge_does_not_overwrite_origin_from_later_requests():
    store = _MessageStore(messages={"req_2": {"content": "a later message", "role": "user"}})
    store._notebooks["tsk_1"] = {
        "task_id": "tsk_1",
        "goal": "original goal excerpt",
        "origin_query": FULL_ORIGINAL,
        "origin_request_id": "req_1",
        "origin_session_id": "sess_1",
        "origin_anchored": True,
        "status": "active",
    }
    runtime = _runtime(session_store=store)
    notebook = runtime._merge_task_notebook(
        task_id="tsk_1",
        session_id="sess_1",
        request_id="req_2",
        event=_event(),
    )
    assert notebook["origin_query"] == FULL_ORIGINAL
    assert notebook["origin_request_id"] == "req_1"


def test_origin_anchor_marks_once_and_requires_request():
    notebook = {"origin_request_id": "req_1", "origin_anchored": False}
    runtime = _runtime()
    assert runtime._mark_task_origin_anchor(notebook) is True
    assert notebook["origin_anchored"] is True
    # Second sighting: already anchored.
    assert runtime._mark_task_origin_anchor(notebook) is False
    # No origin request recorded: never anchorable.
    assert runtime._mark_task_origin_anchor({"origin_anchored": False}) is False


@pytest.mark.asyncio
async def test_origin_anchor_writes_pointer_memory():
    recorded = []

    async def _write_memory_record(payload, audit_event):
        recorded.append(payload)
        return {"record": {"memory_id": "mem_1"}}

    notebook = {
        "task_id": "tsk_1",
        "goal": "Build the Jev-Reranker project",
        "origin_query": "Build the Jev-Reranker project end to end",
        "origin_request_id": "req_1",
        "origin_session_id": "sess_1",
    }
    runtime = _runtime(_write_memory_record=_write_memory_record)
    await runtime._write_task_origin_anchor(
        task_id="tsk_1", session_id="sess_1", notebook=notebook
    )
    assert len(recorded) == 1
    payload = recorded[0]
    assert payload["kind"] == "task_summary"
    assert payload["metadata"]["lifecycle"] == "created"
    assert payload["metadata"]["task_id"] == "tsk_1"
    assert "Jev-Reranker" in payload["content"]
    assert "task_status" in payload["content"]  # points at the live registry


@pytest.mark.asyncio
async def test_origin_anchor_failure_rearm_for_retry():
    attempts = []

    async def _write_memory_record(payload, audit_event):
        attempts.append(payload)
        raise RuntimeError("memory service down")

    class _Store(_MessageStore):
        def get_task_notebook(self, task_id):
            return self._notebooks.get(task_id)

    notebook = {
        "task_id": "tsk_1",
        "goal": "g",
        "origin_query": "q",
        "origin_request_id": "req_1",
        "origin_session_id": "sess_1",
        "origin_anchored": True,
    }
    store = _Store()
    store._notebooks["tsk_1"] = dict(notebook)
    runtime = _runtime(session_store=store, _write_memory_record=_write_memory_record)
    await runtime._write_task_origin_anchor(
        task_id="tsk_1", session_id="sess_1", notebook=notebook
    )
    assert len(attempts) == 1
    # The persisted flag was cleared so a later event retries the anchor.
    assert not store._notebooks["tsk_1"].get("origin_anchored")


# ---------------------------------------------------------------------------
# Interrupted-turn episode ingest
# ---------------------------------------------------------------------------

class _ClaimStore:
    def __init__(self, *, claim_result=True, user_message=None):
        self._claim_result = claim_result
        self._user_message = user_message
        self.released: list[str | None] = []
        self.ingested: list[dict] = []

    def claim_memory_episode_ingest(self, *, request_id, session_id):
        return self._claim_result

    def release_memory_episode_ingest_claim(self, request_id, *, error_text=None):
        self.released.append(error_text)

    def find_message_by_request_id(self, session_id, *, request_id, role):
        if role == "user":
            return self._user_message
        return None


def _interrupt_runtime(store, *, ingest_result=None, ingest_raises=False):
    ingested: list[dict] = []

    async def _ingest_memory_episode(payload, audit_event):
        if ingest_raises:
            raise RuntimeError("memory down")
        ingested.append(payload)
        return ingest_result or {"record": {"memory_id": "mem_i"}}

    runtime = _runtime(
        session_store=store,
        _ingest_memory_episode=_ingest_memory_episode,
        config=SimpleNamespace(cosmic_memory_episode_extract_graph=False),
    )
    runtime.ingested = ingested
    return runtime


@pytest.mark.asyncio
async def test_interrupted_episode_records_user_and_partial():
    store = _ClaimStore(
        user_message={"content": FULL_ORIGINAL, "role": "user"}
    )
    runtime = _interrupt_runtime(store)
    await runtime._ingest_interrupted_episode(
        request_id="req_stop",
        session_id="sess_1",
        channel="desktop:desk_1",
        task_id="tsk_1",
        partial_content="I got as far as creating the repo and...",
        error_message="stream disconnected",
        reason="cancelled by user",
    )
    assert len(runtime.ingested) == 1
    payload = runtime.ingested[0]
    assert payload["kind"] == "transcript"
    obs = payload["observations"]
    assert obs[0]["role"] == "user" and "BL_API_KEY" in obs[0]["content"]
    assert obs[1]["metadata"]["interrupted"] is True
    assert "got as far as" in obs[1]["content"]
    assert payload["metadata"]["stop_reason"] == "cancelled by user"
    assert "interrupted" in payload["tags"]


@pytest.mark.asyncio
async def test_interrupted_episode_notes_when_no_response_started():
    store = _ClaimStore(user_message={"content": "do the thing", "role": "user"})
    runtime = _interrupt_runtime(store)
    await runtime._ingest_interrupted_episode(
        request_id="req_x",
        session_id="sess_1",
        channel="desktop:desk_1",
        task_id=None,
        partial_content="",
        error_message="",
        reason="failed",
    )
    obs = runtime.ingested[0]["observations"]
    assert "stopped before any response" in obs[1]["content"]


@pytest.mark.asyncio
async def test_interrupted_episode_claim_duplicate_is_noop():
    store = _ClaimStore(claim_result=False)
    runtime = _interrupt_runtime(store)
    await runtime._ingest_interrupted_episode(
        request_id="req_1",
        session_id="sess_1",
        channel="desktop:desk_1",
        task_id=None,
        partial_content="",
        error_message="",
        reason="failed",
    )
    assert runtime.ingested == []


@pytest.mark.asyncio
async def test_interrupted_episode_releases_claim_on_ingest_failure():
    store = _ClaimStore(user_message={"content": "hi", "role": "user"})
    runtime = _interrupt_runtime(store, ingest_raises=True)
    await runtime._ingest_interrupted_episode(
        request_id="req_1",
        session_id="sess_1",
        channel="desktop:desk_1",
        task_id=None,
        partial_content="",
        error_message="",
        reason="failed",
    )
    assert store.released and store.released[0]
    assert runtime.ingested == []
