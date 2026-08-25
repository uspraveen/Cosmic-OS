"""Owner-only Gmail drafts skip the approval gate; everything else does not.

The gate exists so Cosmic cannot correspond with other people unreviewed. A
message addressed only to the user's own declared addresses carries none of that
risk -- the reviewer and the recipient are the same person. These tests pin the
boundary, and most of them are about the cases where it must NOT fire.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway.runtime import GatewayRuntime


class _Store:
    def __init__(self, trusted):
        self._trusted = trusted

    def get_primary(self):
        if self._trusted is None:
            return None
        return SimpleNamespace(trusted_senders=self._trusted)


class _Boom:
    def get_primary(self):
        raise RuntimeError("store unavailable")


def _runtime(trusted) -> GatewayRuntime:
    runtime = GatewayRuntime.__new__(GatewayRuntime)
    runtime.agent_email_integration_store = _Store(trusted)  # type: ignore[attr-defined]
    return runtime


OWNER = ["uspraveenraj@gmail.com", "arunanitha34@gmail.com"]


@pytest.mark.parametrize(
    ("approval", "expected", "why"),
    [
        ({"to": ["uspraveenraj@gmail.com"]}, True, "single owner address"),
        ({"to": ["USPraveenRaj@Gmail.com"]}, True, "case is not significant"),
        ({"to": ["Praveen Raj <uspraveenraj@gmail.com>"]}, True, "display name is stripped"),
        (
            {"to": ["uspraveenraj@gmail.com"], "cc": ["arunanitha34@gmail.com"]},
            True,
            "every recipient is an owner address",
        ),
        # --- must NOT auto-approve ---
        ({"to": ["someone@else.com"]}, False, "third party"),
        (
            {"to": ["uspraveenraj@gmail.com"], "cc": ["someone@else.com"]},
            False,
            "one outside contact in cc is enough to need a human",
        ),
        (
            {"to": ["uspraveenraj@gmail.com"], "bcc": ["someone@else.com"]},
            False,
            "bcc is a recipient too -- this is the sneaky one",
        ),
        ({"to": []}, False, "no recipients at all"),
        ({}, False, "no recipient fields at all"),
        ({"to": ["uspraveenraj@gmail.com.evil.com"]}, False, "lookalike domain suffix"),
        ({"to": ["evil.com?uspraveenraj@gmail.com"]}, False, "address smuggling"),
    ],
)
def test_owner_only_boundary(approval: dict, expected: bool, why: str) -> None:
    runtime = _runtime(OWNER)
    assert runtime._gmail_approval_targets_owner_only(approval) is expected, why


def test_empty_trust_list_never_auto_approves() -> None:
    """No declared owner addresses means the gate stays exactly as it was."""
    runtime = _runtime([])
    assert runtime._gmail_approval_targets_owner_only({"to": ["uspraveenraj@gmail.com"]}) is False


def test_missing_integration_record_never_auto_approves() -> None:
    runtime = _runtime(None)
    assert runtime._gmail_approval_targets_owner_only({"to": ["uspraveenraj@gmail.com"]}) is False


def test_store_failure_fails_closed() -> None:
    """An unreadable trust list must gate, never open."""
    runtime = GatewayRuntime.__new__(GatewayRuntime)
    runtime.agent_email_integration_store = _Boom()  # type: ignore[attr-defined]
    assert runtime._owner_email_addresses() == set()
    assert runtime._gmail_approval_targets_owner_only({"to": ["uspraveenraj@gmail.com"]}) is False


@pytest.mark.asyncio
async def test_auto_approve_sends_and_reports_true() -> None:
    runtime = _runtime(OWNER)
    approved: list[str] = []

    async def _approve(approval_id: str):
        approved.append(approval_id)
        return {"status": "sent"}

    runtime.approve_gmail_approval = _approve  # type: ignore[assignment]
    runtime._safe_text = staticmethod(lambda v: str(v or "").strip())  # type: ignore[assignment]
    runtime._bounded_excerpt = lambda v, limit=80: str(v or "")[:limit]  # type: ignore[assignment]

    sent = await runtime._maybe_auto_approve_owner_gmail_draft(
        {"approval_id": "gma_1", "subject": "Your site is live", "to": ["uspraveenraj@gmail.com"]}
    )
    assert sent is True
    assert approved == ["gma_1"]


@pytest.mark.asyncio
async def test_auto_approve_failure_leaves_it_pending_for_a_human() -> None:
    runtime = _runtime(OWNER)

    async def _approve(approval_id: str):
        raise RuntimeError("gmail send failed")

    runtime.approve_gmail_approval = _approve  # type: ignore[assignment]
    runtime._safe_text = staticmethod(lambda v: str(v or "").strip())  # type: ignore[assignment]
    runtime._bounded_excerpt = lambda v, limit=80: str(v or "")[:limit]  # type: ignore[assignment]

    sent = await runtime._maybe_auto_approve_owner_gmail_draft(
        {"approval_id": "gma_2", "subject": "x", "to": ["uspraveenraj@gmail.com"]}
    )
    assert sent is False


@pytest.mark.asyncio
async def test_third_party_draft_is_never_auto_sent() -> None:
    runtime = _runtime(OWNER)
    approved: list[str] = []

    async def _approve(approval_id: str):
        approved.append(approval_id)

    runtime.approve_gmail_approval = _approve  # type: ignore[assignment]
    runtime._safe_text = staticmethod(lambda v: str(v or "").strip())  # type: ignore[assignment]

    sent = await runtime._maybe_auto_approve_owner_gmail_draft(
        {"approval_id": "gma_3", "subject": "x", "to": ["client@bigco.com"]}
    )
    assert sent is False
    assert approved == []
