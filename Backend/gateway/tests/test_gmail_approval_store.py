from __future__ import annotations

from tempfile import TemporaryDirectory
from pathlib import Path


def test_gmail_approval_store_upsert_and_state_transitions():
    from gateway.gmail_approval_store import GmailApprovalStore

    with TemporaryDirectory() as td:
        store = GmailApprovalStore(Path(td) / "gmail_approvals.db")
        store.initialize()

        row, created = store.upsert_pending(
            {
                "account_id": "acc_123",
                "account_email": "user@example.com",
                "draft_id": "draft_123",
                "subject": "Hello",
                "to": ["friend@example.com"],
                "body_text": "Draft body",
            }
        )

        assert created is True
        assert row["approval_id"].startswith("gma_")
        assert row["status"] == "pending"
        assert row["to"] == ["friend@example.com"]

        duplicate, created_again = store.upsert_pending(
            {
                "account_id": "acc_123",
                "account_email": "user@example.com",
                "draft_id": "draft_123",
                "subject": "Hello again",
                "to": ["friend@example.com"],
                "body_text": "Updated body",
            }
        )

        assert created_again is False
        assert duplicate["approval_id"] == row["approval_id"]
        assert duplicate["subject"] == "Hello again"
        by_draft = store.get_by_account_draft("acc_123", "draft_123")
        assert by_draft is not None
        assert by_draft["approval_id"] == row["approval_id"]
        assert store.get_by_account_draft("", "draft_123") is None
        assert store.get_by_account_draft("acc_123", "") is None

        sent = store.mark_sent(row["approval_id"], {"message_id": "msg_123"})
        assert sent is not None
        assert sent["status"] == "sent"
        assert sent["send_result"] == {"message_id": "msg_123"}

        rejected = store.mark_rejected(row["approval_id"], "late rejection")
        assert rejected is not None
        assert rejected["status"] == "rejected"
        assert rejected["reviewer_note"] == "late rejection"


def test_gmail_approval_store_redraft_reopens_rejected_pending_draft():
    from gateway.gmail_approval_store import GmailApprovalStore

    with TemporaryDirectory() as td:
        store = GmailApprovalStore(Path(td) / "gmail_approvals.db")
        store.initialize()
        row, _ = store.upsert_pending(
            {
                "account_id": "acc_123",
                "draft_id": "draft_123",
                "subject": "First version",
                "body_text": "Weak draft",
            }
        )
        rejected = store.mark_rejected(row["approval_id"], "Please improve it")
        assert rejected is not None
        assert rejected["status"] == "rejected"

        redrafted, created = store.upsert_pending(
            {
                "account_id": "acc_123",
                "draft_id": "draft_123",
                "subject": "Improved version",
                "body_text": "Stronger draft",
            }
        )

        assert created is False
        assert redrafted["approval_id"] == row["approval_id"]
        assert redrafted["status"] == "pending"
        assert redrafted["reviewer_note"] is None
        assert redrafted["reviewed_at"] is None
        assert redrafted["subject"] == "Improved version"
