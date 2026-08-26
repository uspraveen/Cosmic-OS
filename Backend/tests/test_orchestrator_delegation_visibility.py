"""A delegation that fails has to leave a trace.

On 2026-08-25 a cron follow-up tried the email specialist first, was refused, and
silently rerouted to the Gmail specialist -- which drafted from the user's own
mailbox instead of COSMIC's. The reroute was a channel and identity switch, and
the only surviving record of it anywhere was a sentence in an assistant response
whose delivery was dropped. There was no ledger row, because the dispatch failed
before a child task existed, and no receipt, because receipts were only collected
for delegations that returned a result.

These tests pin the two halves of that: a failed dispatch produces a receipt, and
trimming the receipt list never discards a receipt that carries state.
"""

from __future__ import annotations

import json

import pytest

from orchestrator.runtime import OrchestratorRuntime
from orchestrator.tools.executor import ToolExecutionContext, ToolExecutor
from shared import TaskEnvelope, sign_task_envelope, utcnow


def _runtime() -> OrchestratorRuntime:
    """A runtime shell: the receipt helpers below are pure and need no services."""
    return OrchestratorRuntime.__new__(OrchestratorRuntime)


def _parent_task() -> TaskEnvelope:
    task = TaskEnvelope(
        task_id="tsk_parent",
        task_list_id="sess_parent",
        parent_task_id=None,
        session_id="sess_parent",
        sender="cosmic/gateway:1.0.0",
        recipient="cosmic/orchestrator:1.0.0",
        intent="orchestrator.process",
        input={"query": "follow up on the overdraft", "request_id": "req_parent"},
        input_artifacts=[],
        idempotency_key="idem_parent",
        priority="normal",
        signature="",
        created_at=utcnow(),
        source="cron",
        source_id="cron_parent",
        channel="desktop",
    )
    return task.model_copy(update={"signature": sign_task_envelope(task, "signing-secret")})


# ── a dispatch that never reaches an agent ──────────────────────────────────


@pytest.mark.asyncio
async def test_undispatchable_intent_returns_a_structured_error_not_an_exception() -> None:
    async def dispatcher(**kwargs):
        raise RuntimeError("No registered agent advertises intent 'email.send'.")

    executor = ToolExecutor(agent_dispatcher=dispatcher)
    result = json.loads(
        await executor.execute(
            "delegate_to_agent",
            {"intent": "email.send", "input": {"to": "a@b.com", "body": "hi"}},
            context=ToolExecutionContext(parent_task=_parent_task(), session_id="sess_parent"),
        )
    )

    assert result["error"] is True
    assert result["code"] == "DISPATCH_FAILED"
    assert "email.send" in result["message"]
    assert "No registered agent advertises" in result["message"]
    # The delegation block is what makes the attempt collectable as a receipt.
    assert result["delegation"]["intent"] == "email.send"
    assert result["delegation"]["dispatched"] is False


@pytest.mark.asyncio
async def test_a_full_bus_is_reported_as_retryable_not_as_a_dead_end() -> None:
    """Backpressure means "try again", not "this specialist does not exist"."""
    from shared import BackpressureError

    async def dispatcher(**kwargs):
        raise RuntimeError("Stream is over capacity (1000/1000)") from BackpressureError("full")

    executor = ToolExecutor(agent_dispatcher=dispatcher)
    result = json.loads(
        await executor.execute(
            "delegate_to_agent",
            {"intent": "gmail.draft_reply", "input": {"to": "a@b.com"}},
            context=ToolExecutionContext(parent_task=_parent_task(), session_id="sess_parent"),
        )
    )

    assert result["error"] is True
    assert result["code"] == "AGENT_BUSY"
    assert result["retryable"] is True
    assert result["next_action"] == "retry"


def test_failed_delegation_is_collected_as_a_receipt() -> None:
    runtime = _runtime()
    receipts: list[dict] = []
    runtime._collect_specialist_receipt(
        "delegate_to_agent",
        {"intent": "email.send"},
        json.dumps(
            {
                "error": True,
                "code": "DISPATCH_FAILED",
                "message": "email.send could not be dispatched: no such intent.",
                "delegation": {"intent": "email.send", "agent_id": None, "dispatched": False},
            }
        ),
        specialist_receipts=receipts,
    )

    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["intent"] == "email.send"
    assert receipt["failed"] is True
    assert receipt["error"]["code"] == "DISPATCH_FAILED"
    assert receipt["error"]["dispatched"] is False


def test_successful_delegation_receipt_is_not_marked_failed() -> None:
    runtime = _runtime()
    receipts: list[dict] = []
    runtime._collect_specialist_receipt(
        "delegate_to_agent",
        {"intent": "gmail.draft_reply"},
        json.dumps({"status": "draft_created", "delegation": {"intent": "gmail.draft_reply"}}),
        specialist_receipts=receipts,
    )

    assert len(receipts) == 1
    assert "failed" not in receipts[0]
    assert "error" not in receipts[0]


# ── trimming must not throw away state ──────────────────────────────────────


def _narrative(index: int) -> dict:
    return {"intent": f"tool.step_{index}", "activity": f"did step {index}"}


def _with_approval(index: int) -> dict:
    return {
        "intent": "gmail.draft_reply",
        "activity": f"drafted {index}",
        "gmail_approval": {"account_id": "acc_1", "draft_id": f"draft_{index}"},
    }


def test_trim_is_a_no_op_below_the_cap() -> None:
    receipts = [_narrative(i) for i in range(3)]
    original = list(receipts)
    OrchestratorRuntime._trim_specialist_receipts(receipts)
    assert receipts == original


def test_trim_keeps_the_most_recent_narrative_receipts() -> None:
    receipts = [_narrative(i) for i in range(7)]
    OrchestratorRuntime._trim_specialist_receipts(receipts)
    assert [item["intent"] for item in receipts] == [
        "tool.step_3",
        "tool.step_4",
        "tool.step_5",
        "tool.step_6",
    ]


def test_trim_keeps_an_early_gmail_approval_that_the_cap_would_have_dropped() -> None:
    """The gateway persists the pending approval FROM this receipt.

    Dropping it leaves a draft sitting in the user's mailbox with no approval card
    anywhere -- which is exactly how a draft becomes invisible junk.
    """
    receipts = [_with_approval(0)] + [_narrative(i) for i in range(1, 8)]
    OrchestratorRuntime._trim_specialist_receipts(receipts)

    approvals = [item for item in receipts if "gmail_approval" in item]
    assert len(approvals) == 1
    assert approvals[0]["gmail_approval"]["draft_id"] == "draft_0"
    assert len(receipts) <= OrchestratorRuntime._RECEIPT_HARD_CAP


def test_trim_keeps_several_consequential_receipts_over_the_soft_cap() -> None:
    receipts = [_with_approval(i) for i in range(6)] + [_narrative(9)]
    OrchestratorRuntime._trim_specialist_receipts(receipts)

    kept = [item["gmail_approval"]["draft_id"] for item in receipts if "gmail_approval" in item]
    assert kept == [f"draft_{i}" for i in range(6)]


def test_trim_never_exceeds_the_hard_cap() -> None:
    receipts = [_with_approval(i) for i in range(20)]
    OrchestratorRuntime._trim_specialist_receipts(receipts)

    assert len(receipts) == OrchestratorRuntime._RECEIPT_HARD_CAP
    # Newest wins when even the consequential receipts overflow.
    assert receipts[-1]["gmail_approval"]["draft_id"] == "draft_19"


def test_trim_preserves_chronological_order() -> None:
    """A kept receipt must stay where it happened, not float to the front."""
    receipts = [_narrative(0), _with_approval(1), _narrative(2), _narrative(3), _narrative(4)]
    OrchestratorRuntime._trim_specialist_receipts(receipts)

    labels = [item.get("gmail_approval", {}).get("draft_id") or item["intent"] for item in receipts]
    assert labels == ["draft_1", "tool.step_2", "tool.step_3", "tool.step_4"]
