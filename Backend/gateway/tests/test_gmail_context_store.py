from __future__ import annotations

import shutil
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_ROOT))


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def test_gmail_context_store_upserts_and_lists_recent_items() -> None:
    from gateway.gmail_context_store import GmailContextStore

    temp_dir = Path(__file__).resolve().parent / f".tmp_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=False)
    try:
        store = GmailContextStore(temp_dir / "gmail_context.db")
        store.initialize()

        first = store.upsert_surfaced_item(
            {
                "account_id": "acc_1",
                "account_email": "user@example.com",
                "message_id": "msg_1",
                "thread_id": "thr_1",
                "subject": "YC S26 interview invite",
                "sender": "Y Combinator <interviews@ycombinator.com>",
                "category": "urgent",
                "priority": 97,
                "suggested_action": "Prepare interview response.",
                "reason": "Matches active YC application.",
            }
        )
        second = store.upsert_surfaced_item(
            {
                "account_id": "acc_1",
                "account_email": "user@example.com",
                "message_id": "msg_1",
                "thread_id": "thr_1",
                "subject": "YC S26 interview invite",
                "sender": "Y Combinator <interviews@ycombinator.com>",
                "category": "needs_reply",
                "priority": 98,
                "suggested_action": "Draft a reply.",
                "reason": "Updated triage result.",
            }
        )

        assert first["surfaced_id"] == second["surfaced_id"]
        recent = store.list_recent(limit=5)
        assert len(recent) == 1
        assert recent[0]["category"] == "needs_reply"
        assert recent[0]["thread_id"] == "thr_1"
        assert recent[0]["account_email"] == "user@example.com"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_gmail_context_store_preserves_terminal_status_on_upsert() -> None:
    from gateway.gmail_context_store import GmailContextStore

    temp_dir = Path(__file__).resolve().parent / f".tmp_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=False)
    try:
        store = GmailContextStore(temp_dir / "gmail_context.db")
        store.initialize()
        first = store.upsert_surfaced_item(
            {
                "account_id": "acc_1",
                "message_id": "msg_loop",
                "thread_id": "thr_loop",
                "subject": "COSMIC update",
                "sender": "Cosmic 001 <iamcosmic001@mail.thelearnchain.com>",
            }
        )
        assert store.mark_status(first["surfaced_id"], "notified")
        again = store.upsert_surfaced_item(
            {
                "account_id": "acc_1",
                "message_id": "msg_loop",
                "thread_id": "thr_loop",
                "subject": "COSMIC update",
                "sender": "Cosmic 001 <iamcosmic001@mail.thelearnchain.com>",
                "status": "active",
                "reason": "re-triaged",
            }
        )
        assert again["status"] == "notified"
        by_id = store.get_by_message_id("msg_loop")
        assert by_id is not None
        assert by_id["status"] == "notified"
        notified = store.list_recent(statuses=["notified"], limit=5)
        assert len(notified) == 1
        assert notified[0]["message_id"] == "msg_loop"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _set_created_at(store, surfaced_id: str, created_at: str) -> None:
    with store._lock, store._connect() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE surfaced_gmail_items SET created_at = ?, updated_at = ? WHERE surfaced_id = ?",
            (created_at, created_at, surfaced_id),
        )
        connection.commit()


def test_gmail_context_store_list_stale_active_only_returns_overdue_active_items() -> None:
    """Reproduces the durability gap behind the 338-item backlog: an active
    item whose decision dispatch never resolved (crash, restart, exception)
    has nothing that ever revisits it. list_stale_active is the query a
    reconciliation sweep uses to find and retry those - it must only return
    items that are (a) still active and (b) old enough that a normal
    dispatch would have finished by now, never notified/suppressed items or
    ones still within a legitimate in-flight window."""
    from gateway.gmail_context_store import GmailContextStore

    temp_dir = Path(__file__).resolve().parent / f".tmp_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=False)
    try:
        store = GmailContextStore(temp_dir / "gmail_context.db")
        store.initialize()
        now = datetime.now(timezone.utc)

        stale_orphan = store.upsert_surfaced_item(
            {"account_id": "acc_1", "message_id": "msg_stale", "thread_id": "thr_stale", "subject": "Stuck"}
        )
        _set_created_at(store, stale_orphan["surfaced_id"], _iso(now - timedelta(minutes=30)))

        fresh_active = store.upsert_surfaced_item(
            {"account_id": "acc_1", "message_id": "msg_fresh", "thread_id": "thr_fresh", "subject": "Just created"}
        )
        _set_created_at(store, fresh_active["surfaced_id"], _iso(now - timedelta(minutes=1)))

        already_notified = store.upsert_surfaced_item(
            {"account_id": "acc_1", "message_id": "msg_done", "thread_id": "thr_done", "subject": "Resolved"}
        )
        _set_created_at(store, already_notified["surfaced_id"], _iso(now - timedelta(minutes=30)))
        store.mark_status(already_notified["surfaced_id"], "notified")

        # stale_after_sec=1200 (20 min): stale_orphan (30m old) qualifies,
        # fresh_active (1m old) does not, already_notified is excluded by
        # status regardless of age.
        stale = store.list_stale_active(stale_after_sec=1200, limit=10)
        ids = {item["surfaced_id"] for item in stale}
        assert stale_orphan["surfaced_id"] in ids
        assert fresh_active["surfaced_id"] not in ids
        assert already_notified["surfaced_id"] not in ids
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_gmail_context_store_list_stale_active_respects_created_after_floor() -> None:
    """The created_after floor is what lets a backfill sweep exclude a
    pre-existing backlog permanently, without needing any "have I run
    before" persistent state - verifies that floor is honored."""
    from gateway.gmail_context_store import GmailContextStore

    temp_dir = Path(__file__).resolve().parent / f".tmp_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=False)
    try:
        store = GmailContextStore(temp_dir / "gmail_context.db")
        store.initialize()
        now = datetime.now(timezone.utc)
        floor = _iso(now - timedelta(minutes=10))

        pre_existing = store.upsert_surfaced_item(
            {"account_id": "acc_1", "message_id": "msg_old_backlog", "thread_id": "thr_old", "subject": "Old backlog"}
        )
        _set_created_at(store, pre_existing["surfaced_id"], _iso(now - timedelta(days=75)))

        post_fix = store.upsert_surfaced_item(
            {"account_id": "acc_1", "message_id": "msg_new", "thread_id": "thr_new", "subject": "After the fix"}
        )
        _set_created_at(store, post_fix["surfaced_id"], _iso(now - timedelta(minutes=5)))

        stale = store.list_stale_active(
            stale_after_sec=1,
            limit=10,
            created_after=floor,
        )
        ids = {item["surfaced_id"] for item in stale}
        assert pre_existing["surfaced_id"] not in ids
        assert post_fix["surfaced_id"] in ids
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
