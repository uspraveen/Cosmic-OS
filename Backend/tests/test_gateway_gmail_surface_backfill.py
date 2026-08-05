from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from gateway.runtime import GMAIL_SURFACE_BACKFILL_ELIGIBLE_SINCE, GatewayRuntime


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


class _FakeGmailContextStore:
    def __init__(self, items: list[dict[str, object]]) -> None:
        self._items = items
        self.last_call: dict[str, object] | None = None

    def list_stale_active(self, *, stale_after_sec, limit, created_after=None):
        self.last_call = {
            "stale_after_sec": stale_after_sec,
            "limit": limit,
            "created_after": created_after,
        }
        eligible = [
            item
            for item in self._items
            if created_after is None or str(item["created_at"]) >= created_after
        ]
        return eligible[:limit]


def _runtime(store: _FakeGmailContextStore) -> GatewayRuntime:
    runtime = GatewayRuntime.__new__(GatewayRuntime)
    runtime.gmail_context_store = store
    runtime.config = SimpleNamespace(
        gmail_surface_backfill_stale_after_sec=1200.0,
        gmail_surface_backfill_batch_limit=3,
    )
    return runtime


@pytest.mark.asyncio
async def test_backfill_redispatches_each_stale_item_with_reconstructed_result() -> None:
    """Reproduces the mechanism behind the 338-item backlog: a decision
    dispatch that never resolved (crash, restart, exception) leaves an item
    permanently stuck in status=active with nothing to retry it. This is the
    sweep that gives such items one more chance. Confirms it passes through
    exactly what _dispatch_gmail_surface_decision needs, reconstructed from
    the stored row alone (no original webhook payload required)."""
    now = datetime.now(timezone.utc)
    stale_item = {
        "surfaced_id": "gmail_stuck_1",
        "message_id": "msg_1",
        "source_task_id": "tsk_original",
        "account_email": "user@example.com",
        "created_at": _iso(now - timedelta(hours=1)),
    }
    store = _FakeGmailContextStore([stale_item])
    runtime = _runtime(store)

    calls: list[dict[str, object]] = []

    async def fake_dispatch(*, result, raw_items, stored_items):
        calls.append({"result": result, "raw_items": raw_items, "stored_items": stored_items})

    runtime._dispatch_gmail_surface_decision = fake_dispatch  # type: ignore[method-assign]

    await runtime._backfill_stale_gmail_surface_decisions()

    assert store.last_call == {
        "stale_after_sec": 1200.0,
        "limit": 3,
        "created_after": GMAIL_SURFACE_BACKFILL_ELIGIBLE_SINCE,
    }
    assert len(calls) == 1
    call = calls[0]
    assert call["result"] == {"task_id": "tsk_original", "email": "user@example.com"}
    assert call["raw_items"] == [{}]
    assert call["stored_items"] == [stale_item]


@pytest.mark.asyncio
async def test_backfill_never_redispatches_the_pre_fix_backlog() -> None:
    """Explicit product decision: fix the durability gap going forward only.
    Items created before GMAIL_SURFACE_BACKFILL_ELIGIBLE_SINCE (the
    pre-existing ~338 item backlog) must never be swept, no matter how
    overdue they are, so months-old items don't suddenly surface as
    notifications."""
    since = datetime.fromisoformat(
        GMAIL_SURFACE_BACKFILL_ELIGIBLE_SINCE.replace("Z", "+00:00")
    )
    pre_fix_item = {
        "surfaced_id": "gmail_ancient",
        "message_id": "msg_old",
        "source_task_id": "tsk_old",
        "account_email": "user@example.com",
        "created_at": _iso(since - timedelta(days=30)),
    }
    store = _FakeGmailContextStore([pre_fix_item])
    runtime = _runtime(store)

    calls: list[dict[str, object]] = []

    async def fake_dispatch(**kwargs):
        calls.append(kwargs)

    runtime._dispatch_gmail_surface_decision = fake_dispatch  # type: ignore[method-assign]

    await runtime._backfill_stale_gmail_surface_decisions()

    assert calls == []


@pytest.mark.asyncio
async def test_backfill_skips_items_missing_a_surfaced_id() -> None:
    store = _FakeGmailContextStore([{"surfaced_id": "", "created_at": _iso(datetime.now(timezone.utc))}])
    runtime = _runtime(store)
    calls: list[dict[str, object]] = []

    async def fake_dispatch(**kwargs):
        calls.append(kwargs)

    runtime._dispatch_gmail_surface_decision = fake_dispatch  # type: ignore[method-assign]

    await runtime._backfill_stale_gmail_surface_decisions()

    assert calls == []


@pytest.mark.asyncio
async def test_backfill_list_failure_is_swallowed_not_raised() -> None:
    class _BrokenStore:
        def list_stale_active(self, **kwargs):
            raise RuntimeError("db unavailable")

    runtime = _runtime(_BrokenStore())  # type: ignore[arg-type]
    # Must not raise - this runs inside a long-lived background loop.
    await runtime._backfill_stale_gmail_surface_decisions()
