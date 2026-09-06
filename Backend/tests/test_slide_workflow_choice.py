"""The Slide Agent's HTML-vs-template question must surface as an inline card.

A `slide.create` delegation without a workflow fails with NEEDS_WORKFLOW_CHOICE.
Leaving the answer to prose means the user types "the editable one" and the
orchestrator re-delegates from a paraphrase. These tests pin the trusted-card
path: the executor attaches a choice payload the Gateway can persist, the
receipt survives receipt trimming and the Gateway's receipt normalizer, and
the stored choice can be selected and re-rendered as a response block.
"""

from __future__ import annotations

import json

import pytest

from gateway.runtime import GatewayRuntime
from gateway.slide_workflow_choice_store import SlideWorkflowChoiceStore
from orchestrator.runtime import OrchestratorRuntime
from orchestrator.tools.executor import ToolExecutionContext, ToolExecutor
from shared import TaskEnvelope, sign_task_envelope, utcnow


def _parent_task(channel: str = "desktop") -> TaskEnvelope:
    task = TaskEnvelope(
        task_id="tsk_parent",
        task_list_id="sess_parent",
        parent_task_id=None,
        session_id="sess_parent",
        sender="cosmic/gateway:1.0.0",
        recipient="cosmic/orchestrator:1.0.0",
        intent="orchestrator.process",
        input={"query": "pitch deck for our seed round", "request_id": "req_parent"},
        input_artifacts=[],
        idempotency_key="idem_parent",
        priority="normal",
        signature="",
        created_at=utcnow(),
        source="user",
        source_id="req_parent",
        channel=channel,
    )
    return task.model_copy(update={"signature": sign_task_envelope(task, "signing-secret")})


def _context(channel: str = "desktop") -> ToolExecutionContext:
    return ToolExecutionContext(
        parent_task=_parent_task(channel),
        session_id="sess_parent",
        task_id="tsk_parent",
        request_id="req_parent",
        channel=channel,
    )


async def _delegate_slide_create(executor: ToolExecutor, channel: str = "desktop") -> dict:
    raw = await executor.execute(
        "delegate_to_agent",
        {
            "intent": "slide.create",
            "input": {"description": "Investor pitch deck for Acme", "max_slides": 10},
        },
        context=_context(channel),
    )
    return json.loads(raw)


# ── executor attaches the choice ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_slide_create_workflow_choice_failure_attaches_choice_and_contract() -> None:
    async def dispatcher(**kwargs):
        from shared.contracts import AgentError, AgentResult

        return AgentResult(
            status="failed",
            output={},
            error=AgentError(
                code="NEEDS_WORKFLOW_CHOICE",
                retryable=False,
                next_action="ask_user",
                message="Choose a slide workflow first.",
            ),
        )

    executor = ToolExecutor(agent_dispatcher=dispatcher)
    result = await _delegate_slide_create(executor)

    assert result["error"] is True
    assert result["code"] == "NEEDS_WORKFLOW_CHOICE"

    choice = result["slide_workflow_choice"]
    assert choice["description"] == "Investor pitch deck for Acme"
    assert choice["requested_slides"] == 10
    assert choice["choice_id"].startswith("slide_wf_")
    assert choice["session_id"] == "sess_parent"
    assert choice["channel"] == "desktop"

    contract = result["_cosmic_ui"]
    assert contract["block_type"] == "slide_workflow_choice"
    assert contract["render"] == "trusted_inline_block"
    assert "workflow choice card" in contract["instruction"]


@pytest.mark.asyncio
async def test_slide_workflow_choice_contract_is_withheld_off_the_desktop() -> None:
    """The card cannot render on channels without trusted blocks, so the
    contract must be withheld exactly like the Gmail approval contract."""

    async def dispatcher(**kwargs):
        from shared.contracts import AgentError, AgentResult

        return AgentResult(
            status="failed",
            output={},
            error=AgentError(
                code="NEEDS_WORKFLOW_CHOICE",
                retryable=False,
                next_action="ask_user",
                message="Choose a slide workflow first.",
            ),
        )

    executor = ToolExecutor(agent_dispatcher=dispatcher)
    result = await _delegate_slide_create(executor, channel="whatsapp:+15550001")

    assert result["slide_workflow_choice"]["choice_id"].startswith("slide_wf_")
    assert "_cosmic_ui" not in result


@pytest.mark.asyncio
async def test_slide_create_other_failures_do_not_attach_a_choice() -> None:
    async def dispatcher(**kwargs):
        from shared.contracts import AgentError, AgentResult

        return AgentResult(
            status="failed",
            output={},
            error=AgentError(
                code="INVALID_INPUT",
                retryable=False,
                next_action="escalate",
                message="slide.create requires a description.",
            ),
        )

    executor = ToolExecutor(agent_dispatcher=dispatcher)
    result = await _delegate_slide_create(executor)

    assert "slide_workflow_choice" not in result
    assert "_cosmic_ui" not in result


# ── receipt collection and trimming ─────────────────────────────────────────


def _choice_result_data(choice_id: str = "slide_wf_abc123") -> dict:
    """The delegate_to_agent result exactly as the executor emits it."""
    return {
        "error": True,
        "code": "NEEDS_WORKFLOW_CHOICE",
        "retryable": False,
        "next_action": "ask_user",
        "message": "Choose a slide workflow first.",
        "delegation": {"intent": "slide.create", "agent_id": "cosmic/slide-agent:1.0.0"},
        "slide_workflow_choice": {
            "choice_id": choice_id,
            "description": "Investor pitch deck for Acme",
            "requested_slides": 10,
            "validate": False,
            "force_catalog": False,
            "artifact_count": 1,
            "artifacts": [{"artifact_id": "art_1", "filename": "brief.pdf"}],
            "session_id": "sess_parent",
            "task_id": "tsk_parent",
            "channel": "desktop",
        },
    }


def _choice_receipt_data(choice_id: str = "slide_wf_abc123") -> dict:
    """The receipt as `_collect_specialist_receipt` stores it (flat intent)."""
    return {
        "intent": "slide.create",
        "agent_id": "cosmic/slide-agent:1.0.0",
        "activity": "delegated slide.create and asked for a workflow",
        "failed": True,
        "error": {"code": "NEEDS_WORKFLOW_CHOICE"},
        "slide_workflow_choice": {
            "choice_id": choice_id,
            "description": "Investor pitch deck for Acme",
            "requested_slides": 10,
            "validate": False,
            "force_catalog": False,
            "artifact_count": 1,
            "artifacts": [{"artifact_id": "art_1", "filename": "brief.pdf"}],
            "session_id": "sess_parent",
            "task_id": "tsk_parent",
            "channel": "desktop",
        },
    }


def test_workflow_choice_is_collected_as_a_receipt() -> None:
    runtime = OrchestratorRuntime.__new__(OrchestratorRuntime)
    receipts: list[dict] = []
    runtime._collect_specialist_receipt(
        "delegate_to_agent",
        {"intent": "slide.create"},
        json.dumps(_choice_result_data()),
        specialist_receipts=receipts,
    )

    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["intent"] == "slide.create"
    assert receipt["failed"] is True
    choice = receipt["slide_workflow_choice"]
    assert choice["choice_id"] == "slide_wf_abc123"
    assert choice["artifacts"][0]["artifact_id"] == "art_1"


def test_trim_keeps_a_slide_workflow_choice_over_the_soft_cap() -> None:
    """The gateway persists the pending choice FROM this receipt; trimming it
    away strands the card with no store row behind it."""
    runtime = OrchestratorRuntime.__new__(OrchestratorRuntime)
    receipts: list[dict] = []
    runtime._collect_specialist_receipt(
        "delegate_to_agent",
        {"intent": "slide.create"},
        json.dumps(_choice_result_data()),
        specialist_receipts=receipts,
    )
    for index in range(7):
        receipts.append({"intent": f"tool.step_{index}", "activity": f"step {index}"})
    OrchestratorRuntime._trim_specialist_receipts(receipts)

    kept = [item for item in receipts if "slide_workflow_choice" in item]
    assert len(kept) == 1
    assert kept[0]["slide_workflow_choice"]["choice_id"] == "slide_wf_abc123"
    assert len(receipts) <= OrchestratorRuntime._RECEIPT_HARD_CAP


# ── gateway normalizer must not strip the receipt ───────────────────────────


def _runtime_shell() -> GatewayRuntime:
    return GatewayRuntime.__new__(GatewayRuntime)


def test_gateway_receipt_normalizer_preserves_slide_workflow_choice() -> None:
    runtime = _runtime_shell()
    normalized = runtime._normalize_specialist_receipts(
        [_choice_receipt_data()], limit=12
    )

    assert len(normalized) == 1
    choice = normalized[0]["slide_workflow_choice"]
    assert choice["choice_id"] == "slide_wf_abc123"
    assert choice["description"] == "Investor pitch deck for Acme"
    assert choice["requested_slides"] == 10
    assert choice["artifact_count"] == 1
    assert choice["artifacts"][0]["artifact_id"] == "art_1"
    assert choice["channel"] == "desktop"


# ── store ───────────────────────────────────────────────────────────────────


def _store(tmp_path) -> SlideWorkflowChoiceStore:
    store = SlideWorkflowChoiceStore(tmp_path / "slide_workflow_choices.db")
    store.initialize()
    return store


def _choice_payload(**overrides) -> dict:
    payload = {
        "choice_id": "slide_wf_abc123",
        "description": "Investor pitch deck for Acme",
        "requested_slides": 10,
        "validate": False,
        "force_catalog": False,
        "artifacts": [{"artifact_id": "art_1", "filename": "brief.pdf"}],
        "session_id": "sess_parent",
        "task_id": "tsk_parent",
        "channel": "desktop",
        "request_id": "req_parent",
    }
    payload.update(overrides)
    return payload


def test_store_upserts_dedupes_and_round_trips(tmp_path) -> None:
    store = _store(tmp_path)
    row, created = store.upsert_pending(_choice_payload())
    assert created is True
    assert row["status"] == "pending"
    assert row["requested_slides"] == 10
    assert row["artifacts"][0]["artifact_id"] == "art_1"
    assert row["validate"] is False

    # Same request content must reuse one card instead of stacking choices.
    same_request = _choice_payload(choice_id="slide_wf_different")
    row_again, created_again = store.upsert_pending(same_request)
    assert created_again is False
    assert row_again["choice_id"] == "slide_wf_abc123"

    assert store.get("slide_wf_missing") is None
    assert store.get("slide_wf_abc123")["description"] == "Investor pitch deck for Acme"


def test_store_mark_selected_and_pending_round_trip(tmp_path) -> None:
    store = _store(tmp_path)
    store.upsert_pending(_choice_payload())

    selected = store.mark_selected("slide_wf_abc123", "template")
    assert selected["status"] == "selected"
    assert selected["workflow"] == "template"
    assert selected["selected_at"]

    reverted = store.mark_pending("slide_wf_abc123")
    assert reverted["status"] == "pending"

    cancelled = store.mark_cancelled("slide_wf_abc123")
    assert cancelled["status"] == "cancelled"


# ── response block ──────────────────────────────────────────────────────────


def test_response_block_carries_choice_fields() -> None:
    runtime = _runtime_shell()
    block = runtime._slide_workflow_choice_response_block(
        {
            "choice_id": "slide_wf_abc123",
            "status": "selected",
            "workflow": "html",
            "description": "Investor pitch deck for Acme",
            "requested_slides": 10,
            "artifact_count": 2,
            "session_id": "sess_parent",
            "task_id": "tsk_parent",
            "created_at": "2026-09-04T00:00:00Z",
        }
    )

    assert block["id"] == "slide_workflow_choice:slide_wf_abc123"
    assert block["type"] == "slide_workflow_choice"
    assert block["choice_id"] == "slide_wf_abc123"
    assert block["status"] == "selected"
    assert block["workflow"] == "html"
    assert block["title"] == "Investor pitch deck for Acme"
    assert block["requested_slides"] == 10
    assert block["artifact_count"] == 2


def test_historical_blocks_reload_from_the_store(tmp_path) -> None:
    store = _store(tmp_path)
    store.upsert_pending(_choice_payload())

    runtime = _runtime_shell()
    runtime.slide_workflow_choice_store = store
    blocks = runtime._historical_slide_workflow_choice_blocks(
        [_choice_receipt_data()]
    )

    assert len(blocks) == 1
    assert blocks[0]["type"] == "slide_workflow_choice"
    assert blocks[0]["choice_id"] == "slide_wf_abc123"
    assert blocks[0]["status"] == "pending"


# ── selecting a choice resumes the original request ─────────────────────────


@pytest.mark.asyncio
async def test_select_marks_the_choice_and_schedules_the_continuation_turn(tmp_path) -> None:
    from types import SimpleNamespace

    store = _store(tmp_path)
    store.upsert_pending(_choice_payload(channel="desktop:desk_test"))

    runtime = _runtime_shell()
    runtime.slide_workflow_choice_store = store
    persisted_blocks: list[dict] = []
    runtime.session_store = SimpleNamespace(
        update_response_action_block=lambda **kwargs: persisted_blocks.append(kwargs) or 1
    )
    published: list[dict] = []

    async def fake_publish(**kwargs) -> None:
        published.append(kwargs)

    runtime._publish_response_action_update = fake_publish
    scheduled: list = []
    runtime._schedule_background_task = lambda coroutine, *, name: scheduled.append(coroutine)
    turns: list[dict] = []

    async def fake_process_incoming(message: dict) -> dict:
        turns.append(message)
        return {"status": "accepted", "request_id": "req_test", "route": "opus"}

    fulfillments: list[dict] = []

    def fake_start_fulfillment(record: dict) -> None:
        fulfillments.append(record)

    runtime.process_incoming_user_message = fake_process_incoming
    runtime.start_request_fulfillment = fake_start_fulfillment

    result = await runtime.select_slide_workflow_choice("slide_wf_abc123", "template")
    assert result["status"] == "selected"
    assert result["choice"]["workflow"] == "template"
    assert result["response_block"]["type"] == "slide_workflow_choice"
    assert result["response_block"]["status"] == "selected"
    assert persisted_blocks and published

    # The continuation turn must run through the chat pipeline pinned to the
    # orchestrator, in the original session and channel — and its fulfillment
    # must actually START (a prepared-but-never-fulfilled record is what left
    # selections stored-but-never-built).
    assert len(scheduled) == 1
    await scheduled[0]
    assert len(turns) == 1
    turn = turns[0]
    assert turn["route_override"] == "opus"
    assert turn["channel"] == "desktop:desk_test"
    assert turn["session_id"] == "sess_parent"
    assert turn["metadata"]["slide_workflow_choice_id"] == "slide_wf_abc123"
    assert 'workflow="template"' in turn["content"]
    assert "Investor pitch deck for Acme" in turn["content"]
    assert fulfillments and fulfillments[0]["request_id"] == "req_test"

    # A second select on the same choice is ignored, not a double dispatch.
    again = await runtime.select_slide_workflow_choice("slide_wf_abc123", "html")
    assert again["status"] == "ignored"


@pytest.mark.asyncio
async def test_select_rejects_unknown_workflows_and_choices(tmp_path) -> None:
    store = _store(tmp_path)
    store.upsert_pending(_choice_payload())
    runtime = _runtime_shell()
    runtime.slide_workflow_choice_store = store

    with pytest.raises(ValueError):
        await runtime.select_slide_workflow_choice("slide_wf_abc123", "executable")
    with pytest.raises(ValueError):
        await runtime.select_slide_workflow_choice("slide_wf_missing", "html")
