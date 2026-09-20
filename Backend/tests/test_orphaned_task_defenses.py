"""Tests for the orphaned-task defenses: agent timebox watchdog, opencode
reader bounding, queue-behind-busy default, orchestrator sweeper, and
task_status verdict semantics."""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT / "scripts"))

from shared.agent_runtime import AgentRuntime  # noqa: E402
from shared.contracts import AgentResult  # noqa: E402


def _minutes_ago(minutes: float) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(minutes=minutes)
    ).isoformat()


# ---------------------------------------------------------------------------
# Queue-behind-busy: busy must never mean gone
# ---------------------------------------------------------------------------

def test_every_intent_may_queue_behind_busy():
    from orchestrator.runtime import OrchestratorRuntime

    for intent in ("alpha.execute", "browser.run", "email.handle", "x.search"):
        assert OrchestratorRuntime._intent_can_queue_busy(intent) is True


# ---------------------------------------------------------------------------
# Agent timebox watchdog
# ---------------------------------------------------------------------------

class _FakeProcessTask:
    """A real asyncio.Task so gather()/cancel() semantics match production."""

    def __init__(self, *, hang: bool):
        self.cancel_called = False
        self._task = asyncio.create_task(self._run(hang))

    async def _run(self, hang: bool):
        if not hang:
            return "done"
        try:
            await asyncio.Event().wait()  # never completes
        except asyncio.CancelledError:
            self.cancel_called = True
            raise

    def done(self):
        return self._task.done()

    def cancel(self):
        self.cancel_called = True
        return self._task.cancel()

    def cancelled(self):
        return self._task.cancelled()

    def __await__(self):
        return self._task.__await__()

    async def wait_closed(self):
        try:
            await self._task
        except BaseException:
            pass


def _timebox_runtime(meta, process_task, *, max_duration=900):
    from shared.contracts import AgentError  # noqa: F401  (constructor shape)

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.agent_id = "cosmic/alpha-agent:1.0.0"
    runtime.max_task_duration_sec = max_duration
    runtime._active_task_meta = meta
    runtime._active_process_task = process_task
    emitted: list[tuple[str, AgentResult]] = []

    async def emit_terminal(task_id, result):
        emitted.append((task_id, result))

    runtime.emit_terminal_event = emit_terminal
    return runtime, emitted


@pytest.mark.asyncio
async def test_timebox_cancels_hung_handler_and_emits_terminal():
    fake = _FakeProcessTask(hang=True)
    meta = {
        "task_id": "tsk_hung",
        "intent": "alpha.execute",
        "started_monotonic": time.monotonic() - 9999,
    }
    runtime, emitted = _timebox_runtime(meta, fake)
    await runtime._enforce_execution_timebox()
    assert fake.cancel_called is True
    await fake.wait_closed()
    assert fake.cancelled() is True
    assert len(emitted) == 1
    task_id, result = emitted[0]
    assert task_id == "tsk_hung"
    assert result.status == "failed"
    assert result.error.code == "EXECUTION_TIMEBOX_EXCEEDED"
    assert runtime._active_task_meta == {  # cleared? no — the real finally does
        "task_id": "tsk_hung",
        "intent": "alpha.execute",
        "started_monotonic": meta["started_monotonic"],
    }


@pytest.mark.asyncio
async def test_timebox_ignores_young_execution():
    fake = _FakeProcessTask(hang=True)
    meta = {
        "task_id": "tsk_young",
        "intent": "alpha.execute",
        "started_monotonic": time.monotonic() - 60,
    }
    runtime, emitted = _timebox_runtime(meta, fake)
    await runtime._enforce_execution_timebox()
    assert fake.cancel_called is False
    assert emitted == []
    fake.cancel()
    await fake.wait_closed()


@pytest.mark.asyncio
async def test_timebox_ignores_idle_agent():
    runtime, emitted = _timebox_runtime(None, None)
    await runtime._enforce_execution_timebox()
    assert emitted == []


# ---------------------------------------------------------------------------
# Orchestrator sweeper
# ---------------------------------------------------------------------------

class _FakeLedger:
    def __init__(self, active):
        self._active = active
        self.failed: list[tuple[str, str, str]] = []

    def list_active_tasks(self):
        return self._active

    def mark_failed(self, task_id, *, code, message):
        self.failed.append((task_id, code, message))


class _FakeRedis:
    def __init__(self, instances):
        # instances: {match_pattern_value: state_hash}
        self._instances = instances

    async def scan(self, *, cursor, match, count):
        return 0, list(self._instances.keys())

    async def hgetall(self, key):
        return self._instances.get(key, {})


def _sweeper_runtime(active, instances, tmp_path):
    from orchestrator.runtime import OrchestratorRuntime

    runtime = OrchestratorRuntime.__new__(OrchestratorRuntime)
    runtime.task_ledger = _FakeLedger(active)
    runtime._redis = _FakeRedis(instances)
    runtime.config = SimpleNamespace(
        task_sweep_interval_sec=60,
        task_sweep_deferred_after_sec=1800,
        task_sweep_running_after_sec=14400,
        heartbeat_notes_path=tmp_path / "heartbeat_notes.md",
    )
    return runtime


def _old_deferred_task(task_id="tsk_stuck", recipient="cosmic/alpha-agent:1.0.0"):
    return {
        "task_id": task_id,
        "status": "deferred",
        "recipient": recipient,
        "updated_at": _minutes_ago(200),
    }


@pytest.mark.asyncio
async def test_sweeper_fails_orphaned_deferred_task(tmp_path):
    runtime = _sweeper_runtime([_old_deferred_task()], {}, tmp_path)
    await runtime._sweep_orphaned_tasks()
    assert len(runtime.task_ledger.failed) == 1
    task_id, code, message = runtime.task_ledger.failed[0]
    assert task_id == "tsk_stuck"
    assert code == "ORPHANED_EXECUTOR"
    note = runtime.config.heartbeat_notes_path.read_text(encoding="utf-8")
    assert "task-sweeper" in note and "tsk_stuck" in note


@pytest.mark.asyncio
async def test_sweeper_spots_task_claimed_by_live_instance(tmp_path):
    instances = {
        "registry:cosmic/alpha-agent:1.0.0:alpha-agent-1": {
            "status": "healthy",
            "health_details": '{"active_task_id": "tsk_stuck"}',
        }
    }
    runtime = _sweeper_runtime([_old_deferred_task()], instances, tmp_path)
    await runtime._sweep_orphaned_tasks()
    assert runtime.task_ledger.failed == []


@pytest.mark.asyncio
async def test_sweeper_fails_open_when_registry_unreachable(tmp_path):
    class _BrokenRedis:
        async def scan(self, *, cursor, match, count):
            raise RuntimeError("redis down")

        async def hgetall(self, key):
            return {}

    runtime = _sweeper_runtime([_old_deferred_task()], {}, tmp_path)
    runtime._redis = _BrokenRedis()
    await runtime._sweep_orphaned_tasks()
    assert runtime.task_ledger.failed == []


@pytest.mark.asyncio
async def test_sweeper_leaves_fresh_tasks_alone(tmp_path):
    fresh = _old_deferred_task()
    fresh["updated_at"] = _minutes_ago(5)
    runtime = _sweeper_runtime([fresh], {}, tmp_path)
    await runtime._sweep_orphaned_tasks()
    assert runtime.task_ledger.failed == []


# ---------------------------------------------------------------------------
# opencode runner: readers bounded after process exit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_opencode_readers_bounded_when_grandchild_holds_pipe():
    from agents.alpha_agent.opencode_runner import OpenCodeWorkspaceRunner

    runner = OpenCodeWorkspaceRunner.__new__(OpenCodeWorkspaceRunner)
    runner.config = SimpleNamespace(cli_idle_check_sec=60, opencode_timeout_sec=60)

    reader = asyncio.StreamReader()
    reader.feed_data(b'{"event":"step"}\n')  # buffered, but EOF never comes:
    # the grandchild "holds the pipe" — the stream never reports EOF even
    # though the process itself has exited.

    class _FakeProc:
        def __init__(self):
            self.stdout = reader
            self.stderr = None
            self.returncode = None

        async def wait(self):
            return self.returncode

    proc = _FakeProc()

    async def _exit_soon():
        await asyncio.sleep(0.3)
        proc.returncode = 0

    asyncio.create_task(_exit_soon())
    started = time.monotonic()
    stdout_parts, stderr_parts, state = await asyncio.wait_for(
        runner._communicate_streaming(
            process=proc, timeout_sec=30.0, event_callback=None, cancel_check=None
        ),
        timeout=20.0,
    )
    elapsed = time.monotonic() - started
    # The old code hung forever here; the new code bounds the readers and
    # returns with the partial output once the process exit is observed.
    assert elapsed < 15.0
    assert state["timed_out"] is False
    assert any("step" in part for part in stdout_parts)


# ---------------------------------------------------------------------------
# task_status verdict semantics
# ---------------------------------------------------------------------------

def _verdict_runtime(notebooks, ledger_rows):
    from gateway.runtime import GatewayRuntime

    class _Store:
        def list_recent_task_notebooks(self, *, limit=250):
            return notebooks

    class _Ledger:
        def get_task(self, task_id):
            return ledger_rows.get(task_id)

    runtime = GatewayRuntime.__new__(GatewayRuntime)
    runtime.session_store = _Store()
    runtime._orchestrator_task_ledger = _Ledger()
    return runtime


def _nb(task_id, goal, *, status="active", age_minutes):
    return {
        "task_id": task_id,
        "status": status,
        "current_state": "running" if status == "active" else status,
        "goal": goal,
        "updated_at": _minutes_ago(age_minutes),
    }


def test_verdict_stuck_for_long_silent_nonterminal_task():
    runtime = _verdict_runtime(
        [_nb("tsk_x", "remediation build for jev reranker", age_minutes=200)], {}
    )
    result = runtime.task_status_search("remediation jev")
    assert result["matches"][0]["verdict"] == "STUCK"


def test_verdict_active_for_recent_nonterminal_task():
    runtime = _verdict_runtime(
        [_nb("tsk_y", "remediation build for jev reranker", age_minutes=4)], {}
    )
    result = runtime.task_status_search("remediation jev")
    assert result["matches"][0]["verdict"] == "ACTIVE"


def test_verdict_failed_overrides_notebook_running():
    runtime = _verdict_runtime(
        [_nb("tsk_z", "remediation build for jev reranker", age_minutes=2)],
        {"tsk_z": {"status": "failed", "error_code": "ORPHANED_EXECUTOR"}},
    )
    result = runtime.task_status_search("remediation jev")
    assert result["matches"][0]["verdict"] == "FAILED"


def test_verdict_done_for_completed_task():
    runtime = _verdict_runtime(
        [_nb("tsk_ok", "remediation build for jev reranker", status="completed", age_minutes=2)],
        {"tsk_ok": {"status": "completed"}},
    )
    result = runtime.task_status_search("remediation jev")
    assert result["matches"][0]["verdict"] == "DONE"
