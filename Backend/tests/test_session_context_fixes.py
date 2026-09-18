"""Tests for the session-context fixes: cross-channel brief attachment for
fresh threads, running-task visibility, ledger reconciliation with post-crash
notices, task-status search, and reminder `until` expiry."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.runtime import GatewayRuntime
from gateway.session_store import SessionStore


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _minutes_ago(minutes: float) -> str:
    return _iso(datetime.now(timezone.utc) - timedelta(minutes=minutes))


def _runtime(**attrs) -> GatewayRuntime:
    runtime = GatewayRuntime.__new__(GatewayRuntime)
    for key, value in attrs.items():
        setattr(runtime, key, value)
    return runtime


# ---------------------------------------------------------------------------
# A1: the brief attaches for the first turn of a fresh isolated session
# ---------------------------------------------------------------------------

class _StoreWithEmptyHistory:
    def get_history_tail(self, session_id, limit=30):
        return []


def test_brief_attaches_when_history_and_fallback_empty():
    runtime = _runtime(session_store=_StoreWithEmptyHistory())
    note = {"role": "assistant", "content": "[Recent activity ...]"}
    runtime._recent_cross_channel_brief = lambda session_id: note
    context = runtime._build_conversation_context("email-thread:mb:new")
    assert context == [note]


def test_empty_context_when_no_history_no_fallback_no_brief():
    runtime = _runtime(session_store=_StoreWithEmptyHistory())
    runtime._recent_cross_channel_brief = lambda session_id: None
    assert runtime._build_conversation_context("email-thread:mb:new") == []


# ---------------------------------------------------------------------------
# A2/A3: running-task lines in the brief
# ---------------------------------------------------------------------------

def test_running_tasks_brief_lines_include_live_task_with_age():
    notebook = {
        "task_id": "tsk_live_1",
        "status": "active",
        "current_state": "running",
        "goal": "Build the Jev-Reranker project end to end",
        "updated_at": _minutes_ago(7),
    }

    class _Store:
        def list_recent_task_notebooks(self, *, limit=20, max_age_sec=None):
            assert max_age_sec == GatewayRuntime.RUNNING_TASKS_BRIEF_MAX_AGE_SEC
            return [notebook]

    runtime = _runtime(session_store=_Store())
    lines = runtime._running_tasks_brief_lines()
    assert len(lines) == 1
    assert "tsk_live_1" in lines[0]
    assert "Jev-Reranker" in lines[0]
    assert "7m ago" in lines[0]


def test_running_tasks_brief_lines_exclude_terminal_and_beyond_window():
    def _filter_by_age(notebooks, max_age_sec):
        if max_age_sec is None:
            return notebooks
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age_sec)
        return [
            n
            for n in notebooks
            if datetime.fromisoformat(n["updated_at"].replace("Z", "+00:00"))
            >= cutoff
        ]

    class _Store:
        """Mimics the real store's SQL-level age filtering."""

        def list_recent_task_notebooks(self, *, limit=20, max_age_sec=None):
            return _filter_by_age(
                [
                    {
                        "task_id": "tsk_done",
                        "status": "completed",
                        "goal": "finished thing",
                        "updated_at": _minutes_ago(1),
                    },
                    {
                        "task_id": "tsk_old",
                        "status": "active",
                        "goal": "quiet thing",
                        "updated_at": _minutes_ago(60),
                    },
                    {
                        "task_id": "tsk_ancient",
                        "status": "active",
                        "goal": "long dead thing",
                        "updated_at": _minutes_ago(60 * 24),
                    },
                ],
                max_age_sec,
            )

    runtime = _runtime(session_store=_Store())
    lines = runtime._running_tasks_brief_lines()
    # Terminal excluded, beyond-window excluded; quiet-but-recent stays with
    # its age label so the model can judge liveness itself.
    assert len(lines) == 1
    assert "tsk_old" in lines[0]
    assert "60m ago" in lines[0]


def test_running_tasks_brief_survives_store_failure():
    class _Store:
        def list_recent_task_notebooks(self, *, limit=20, max_age_sec=None):
            raise RuntimeError("db locked")

    runtime = _runtime(session_store=_Store())
    assert runtime._running_tasks_brief_lines() == []


# ---------------------------------------------------------------------------
# A5/A6: startup reconciliation + post-crash notice
# ---------------------------------------------------------------------------

class _ReconcileStore:
    def __init__(self, notebooks, assistant_for_request=None):
        self._notebooks = notebooks
        self._assistant_for_request = assistant_for_request or {}
        self.upserts: list[tuple[str, str, dict]] = []
        self.appended: list[dict] = []

    def list_non_terminal_task_notebooks(self, *, limit=500):
        return self._notebooks

    def find_message_by_request_id(self, session_id, *, request_id, role):
        if role == "assistant":
            return self._assistant_for_request.get(request_id)
        return {"message_id": "msg_user_1"}

    def upsert_task_notebook(self, task_id, session_id, notebook):
        self.upserts.append((task_id, session_id, notebook))

    def append_message(self, session_id, *, role, content, **kwargs):
        message_id = f"msg_appended_{len(self.appended) + 1}"
        self.appended.append(
            {"session_id": session_id, "role": role, "content": content, **kwargs}
        )
        return message_id


class _Ledger:
    def __init__(self, rows):
        self._rows = rows

    def get_task(self, task_id):
        return self._rows.get(task_id)


def _reconcile_runtime(store, ledger) -> GatewayRuntime:
    broadcast_calls: list[dict] = []

    async def _broadcast(*args, **kwargs):
        broadcast_calls.append(kwargs)

    runtime = _runtime(
        session_store=store,
        _orchestrator_task_ledger=ledger,
        _track_background_task=lambda coro: coro.close(),
    )
    runtime._merge_session_vault_action_blocks = (
        lambda session_id, metadata: metadata
    )
    runtime._broadcast_cross_channel_to_realtime_clients = _broadcast
    runtime._broadcast_calls = broadcast_calls
    return runtime


def _zombie_notebook(**overrides):
    base = {
        "task_id": "tsk_zombie",
        "session_id": "sess_20260917",
        "status": "active",
        "current_state": "running",
        "goal": "Build the Jev-Reranker project end to end",
        "activity_log": [
            {"id": "a1", "label": "Alpha agent planned 5 steps", "status": "progress"}
        ],
        "updated_at": _minutes_ago(600),
    }
    base.update(overrides)
    return base


def test_reconcile_flips_zombie_and_posts_notice():
    store = _ReconcileStore([_zombie_notebook()])
    ledger = _Ledger(
        {
            "tsk_zombie": {
                "task_id": "tsk_zombie",
                "status": "failed",
                "error_code": "STREAM_DISCONNECTED",
                "error_message": "The upstream stream ended before the task completed.",
                "request_id": "req_dead_1",
                "channel": "desktop:desk_a1",
            }
        }
    )
    runtime = _reconcile_runtime(store, ledger)
    asyncio.run(runtime._reconcile_task_notebooks_with_ledger())

    assert len(store.upserts) == 1
    task_id, session_id, notebook = store.upserts[0]
    assert (task_id, session_id) == ("tsk_zombie", "sess_20260917")
    assert notebook["status"] == "failed"
    assert "STREAM_DISCONNECTED" in notebook["current_state"]
    assert notebook["post_crash_notice_sent"] is True

    assert len(store.appended) == 1
    notice = store.appended[0]
    assert notice["role"] == "assistant"
    assert notice["session_id"] == "sess_20260917"
    assert "tsk_zombie" in notice["content"]
    assert "STREAM_DISCONNECTED" in notice["content"]
    assert notice["metadata"]["post_crash_notice"] is True


def test_reconcile_does_not_duplicate_notice_when_session_already_answered():
    store = _ReconcileStore(
        [_zombie_notebook(post_crash_notice_sent=True)],
        assistant_for_request={"req_dead_1": {"message_id": "msg_existing"}},
    )
    ledger = _Ledger(
        {
            "tsk_zombie": {
                "status": "failed",
                "error_code": "STREAM_DISCONNECTED",
                "error_message": "stream ended",
                "request_id": "req_dead_1",
            }
        }
    )
    runtime = _reconcile_runtime(store, ledger)
    asyncio.run(runtime._reconcile_task_notebooks_with_ledger())
    assert store.appended == []
    assert store.upserts[0][2]["status"] == "failed"


def test_reconcile_leaves_missing_ledger_rows_alone():
    store = _ReconcileStore([_zombie_notebook()])
    runtime = _reconcile_runtime(store, _Ledger({}))
    asyncio.run(runtime._reconcile_task_notebooks_with_ledger())
    assert store.upserts == []
    assert store.appended == []


def test_reconcile_completion_flips_without_notice():
    store = _ReconcileStore([_zombie_notebook()])
    ledger = _Ledger(
        {"tsk_zombie": {"status": "completed", "request_id": "req_done_1"}}
    )
    runtime = _reconcile_runtime(store, ledger)
    asyncio.run(runtime._reconcile_task_notebooks_with_ledger())
    assert store.upserts[0][2]["status"] == "completed"
    assert store.appended == []


# ---------------------------------------------------------------------------
# A8: task-status search
# ---------------------------------------------------------------------------

def test_task_status_search_matches_and_overlays_ledger():
    notebooks = [
        _zombie_notebook(
            task_id="tsk_jev",
            status="active",
            goal="Build the Jev-Reranker project end to end",
            current_state="running",
            updated_at=_minutes_ago(3),
        ),
        {
            "task_id": "tsk_other",
            "status": "completed",
            "goal": "Book dentist appointment",
            "updated_at": _minutes_ago(30),
        },
    ]

    class _Store:
        def list_recent_task_notebooks(self, *, limit=60):
            return notebooks

    ledger = _Ledger(
        {
            "tsk_jev": {
                "status": "failed",
                "error_code": "STREAM_DISCONNECTED",
                "error_message": "stream ended",
            }
        }
    )
    runtime = _runtime(session_store=_Store(), _orchestrator_task_ledger=ledger)
    result = runtime.task_status_search("jev reranker")
    matches = result["matches"]
    assert len(matches) == 1
    match = matches[0]
    assert match["task_id"] == "tsk_jev"
    # The ledger truth wins over the stale notebook status.
    assert match["status"] == "failed"
    assert match["ledger_error"].startswith("STREAM_DISCONNECTED")
    assert match["age_minutes"] is not None and match["age_minutes"] < 10


def test_task_status_search_no_keyword_is_rejected():
    runtime = _runtime(session_store=_StoreWithEmptyHistory())
    result = runtime.task_status_search("   ")
    assert result["matches"] == []


# ---------------------------------------------------------------------------
# A7: reminder `until` expiry
# ---------------------------------------------------------------------------

class _SchedulerStoreSpy:
    def __init__(self, crons):
        self._crons = crons
        self.paused: list[tuple[str, str | None]] = []
        self.results: list[dict] = []

    def fetch_due_crons(self, *, now_iso, limit=8):
        return self._crons

    def pause_cron(self, cron_id, *, reason=None):
        self.paused.append((cron_id, reason))
        return {"cron_id": cron_id, "paused": True}

    def record_cron_result(self, *, cron_id, scheduled_for, status, summary, next_fire_at):
        self.results.append(
            {
                "cron_id": cron_id,
                "status": status,
                "summary": summary,
                "next_fire_at": next_fire_at,
            }
        )


def test_due_cron_past_until_is_paused_not_fired():
    cron = {
        "cron_id": "cron_hourly_update",
        "name": "Hourly Jev update",
        "cron_expr": "0 * * * *",
        "timezone": "America/Chicago",
        "next_fire_at": _minutes_ago(5),
        "metadata": {
            "prompt": "Send the user a Jev-Reranker progress update.",
            "one_shot": False,
            "until": _minutes_ago(1),  # window already closed
        },
    }
    store = _SchedulerStoreSpy([cron])
    runtime = _runtime(scheduler_store=store)

    executed: list[dict] = []

    async def _no_execute(cron):
        executed.append(cron)
        raise AssertionError("must not execute")

    runtime._execute_custom_scheduler_cron = _no_execute
    runtime._sync_system_crons = lambda: None

    async def _heartbeat():
        return None

    runtime._maybe_run_due_heartbeat = _heartbeat
    asyncio.run(runtime._run_due_crons_locked())

    assert executed == []
    assert store.paused[0][0] == "cron_hourly_update"
    assert "until passed" in (store.paused[0][1] or "")
    assert store.results[0]["status"] == "skipped"
    assert "window ended" in store.results[0]["summary"]


def test_due_cron_before_until_still_fires():
    cron = {
        "cron_id": "cron_hourly_update",
        "name": "Hourly Jev update",
        "cron_expr": "0 * * * *",
        "timezone": "America/Chicago",
        "next_fire_at": _minutes_ago(5),
        "metadata": {
            "prompt": "Send the user a Jev-Reranker progress update.",
            "one_shot": False,
            "until": _minutes_ago(-120),  # two hours in the future
        },
    }
    store = _SchedulerStoreSpy([cron])

    executed: list[dict] = []

    async def _execute(cron):
        executed.append(cron)
        return ("completed", "ran", None)

    runtime = _runtime(scheduler_store=store)
    runtime._execute_custom_scheduler_cron = _execute
    runtime._sync_system_crons = lambda: None

    async def _heartbeat():
        return None

    runtime._maybe_run_due_heartbeat = _heartbeat
    asyncio.run(runtime._run_due_crons_locked())

    assert len(executed) == 1
    assert store.paused == []
    assert store.results[0]["status"] == "completed"


def test_cron_without_until_untouched():
    cron = {
        "cron_id": "cron_plain",
        "name": "Plain reminder",
        "cron_expr": "0 9 * * *",
        "timezone": "America/Chicago",
        "next_fire_at": _minutes_ago(5),
        "metadata": {"prompt": "Stand up.", "one_shot": True},
    }
    store = _SchedulerStoreSpy([cron])

    async def _execute(cron):
        return ("completed", "ran", None)

    runtime = _runtime(scheduler_store=store)
    runtime._execute_custom_scheduler_cron = _execute
    runtime._sync_system_crons = lambda: None

    async def _heartbeat():
        return None

    runtime._maybe_run_due_heartbeat = _heartbeat
    asyncio.run(runtime._run_due_crons_locked())
    assert store.paused == []


def test_reconcile_notice_gated_by_age():
    """Deaths older than the notice window are reconciled silently."""
    store = _ReconcileStore([_zombie_notebook(updated_at=_minutes_ago(60 * 24 * 10))])
    ledger = _Ledger(
        {
            "tsk_zombie": {
                "status": "failed",
                "error_code": "STREAM_DISCONNECTED",
                "error_message": "stream ended",
                "request_id": "req_old_1",
            }
        }
    )
    runtime = _reconcile_runtime(store, ledger)
    asyncio.run(runtime._reconcile_task_notebooks_with_ledger())
    assert store.upserts[0][2]["status"] == "failed"
    assert store.appended == []
