from __future__ import annotations

import shutil
import sys
import uuid
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_ROOT))


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
