"""Tests for dynamic LLM-driven StepPlan integration in tabular_reason_graph.

Tests the create_plan action, plan_step tracking, finalize auto-completion,
and the no-plan fallback path — all without requiring the full LangGraph
runtime or a live LLM.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.step_plan import StepPlan


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_step_plan() -> tuple[StepPlan, list[dict[str, Any]]]:
    """Create a StepPlan wired to a recording emit function."""
    events: list[dict[str, Any]] = []

    async def _record(task_id: str, event_type: str, payload: dict) -> str:
        events.append({"task_id": task_id, "event_type": event_type, **payload})
        return "ok"

    plan = StepPlan(task_id="test_task_1", emit_event_fn=_record)
    return plan, events


# ---------------------------------------------------------------------------
# StepPlan core behavior (sanity)
# ---------------------------------------------------------------------------

class TestStepPlanCore:
    """Verify StepPlan works as expected (since we depend on it)."""

    def test_no_active_plan_has_no_pending(self) -> None:
        plan, _ = _make_step_plan()
        assert plan.has_pending_steps() is False

    def test_create_marks_active(self) -> None:
        plan, events = _make_step_plan()
        result = asyncio.get_event_loop().run_until_complete(
            plan.create(["Step A", "Step B", "Step C"])
        )
        assert result["plan_active"] is True
        assert result["total_steps"] == 3
        assert plan.has_pending_steps() is True
        # Should have emitted agent_plan_created
        assert any(e.get("type") == "agent_plan_created" for e in events)

    def test_update_tracks_progress(self) -> None:
        plan, events = _make_step_plan()
        loop = asyncio.get_event_loop()
        loop.run_until_complete(plan.create(["A", "B"]))
        result = loop.run_until_complete(plan.update(1, "completed", note="Done A"))
        assert result["completed"] == 1
        assert result["percent"] == 50
        assert plan.has_pending_steps() is True
        loop.run_until_complete(plan.update(2, "completed"))
        assert plan.has_pending_steps() is False

    def test_update_without_plan_returns_error(self) -> None:
        plan, _ = _make_step_plan()
        result = asyncio.get_event_loop().run_until_complete(
            plan.update(1, "completed")
        )
        assert "error" in result

    def test_create_replaces_existing_plan(self) -> None:
        plan, events = _make_step_plan()
        loop = asyncio.get_event_loop()
        loop.run_until_complete(plan.create(["Old step 1", "Old step 2"]))
        loop.run_until_complete(plan.update(1, "completed"))
        # Now replace with a new plan
        result = loop.run_until_complete(plan.create(["New step 1", "New step 2", "New step 3"]))
        assert result["total_steps"] == 3
        assert plan.has_pending_steps() is True  # all pending again


# ---------------------------------------------------------------------------
# create_plan action in _run_tool
# ---------------------------------------------------------------------------

class TestCreatePlanAction:
    """Test the create_plan action handling without the full graph."""

    def _import_run_tool(self):
        """Import _run_tool from tabular_reason_graph."""
        from agents.tabular_agent.tabular_reason_graph import _run_tool
        return _run_tool

    def _make_ctx(self, step_plan=None):
        """Build a minimal _GraphCtx mock."""
        from agents.tabular_agent.tabular_reason_graph import _GraphCtx

        agent = MagicMock()
        agent.step_plan = step_plan
        cfg = MagicMock()
        http_client = MagicMock()
        task = MagicMock()
        return _GraphCtx(agent=agent, cfg=cfg, http_client=http_client, task=task)

    def _make_state(self, **overrides) -> dict[str, Any]:
        """Build a minimal TabularReasonState dict."""
        state: dict[str, Any] = {
            "bundle_id": "b1",
            "artifact_id": "a1",
            "bundle_root": "/tmp/fake",
            "goal": "test",
            "available_skills": [],
        }
        state.update(overrides)
        return state

    def test_create_plan_missing_steps(self) -> None:
        _run_tool = self._import_run_tool()
        plan, _ = _make_step_plan()
        ctx = self._make_ctx(step_plan=plan)
        state = self._make_state()
        pending = {"action": "create_plan"}  # no steps key

        result, obs = asyncio.get_event_loop().run_until_complete(
            _run_tool(ctx=ctx, state=state, pending=pending)
        )
        assert "error" in result

    def test_create_plan_empty_steps(self) -> None:
        _run_tool = self._import_run_tool()
        plan, _ = _make_step_plan()
        ctx = self._make_ctx(step_plan=plan)
        state = self._make_state()
        pending = {"action": "create_plan", "steps": []}

        result, obs = asyncio.get_event_loop().run_until_complete(
            _run_tool(ctx=ctx, state=state, pending=pending)
        )
        assert "error" in result

    def test_create_plan_valid_steps(self) -> None:
        _run_tool = self._import_run_tool()
        plan, events = _make_step_plan()
        ctx = self._make_ctx(step_plan=plan)
        state = self._make_state()
        pending = {
            "action": "create_plan",
            "steps": ["Inspect schema", "Run DSO query", "Verify results", "Summarize"],
        }

        result, obs = asyncio.get_event_loop().run_until_complete(
            _run_tool(ctx=ctx, state=state, pending=pending)
        )
        assert result["kind"] == "create_plan"
        assert result["total_steps"] == 4
        assert result["plan_active"] is True
        # StepPlan should have been called
        assert plan.has_pending_steps() is True
        # Step 1 should be in_progress
        plan_state = asyncio.get_event_loop().run_until_complete(plan.list())
        assert plan_state["steps"][0]["status"] == "in_progress"

    def test_create_plan_truncates_to_8_steps(self) -> None:
        _run_tool = self._import_run_tool()
        plan, _ = _make_step_plan()
        ctx = self._make_ctx(step_plan=plan)
        state = self._make_state()
        pending = {
            "action": "create_plan",
            "steps": [f"Step {i}" for i in range(12)],  # 12 steps, should cap at 8
        }

        result, obs = asyncio.get_event_loop().run_until_complete(
            _run_tool(ctx=ctx, state=state, pending=pending)
        )
        assert result["total_steps"] == 8

    def test_create_plan_without_step_plan_injected(self) -> None:
        """When StepPlan is not injected (e.g. testing), return plan as observation."""
        _run_tool = self._import_run_tool()
        ctx = self._make_ctx(step_plan=None)
        state = self._make_state()
        pending = {
            "action": "create_plan",
            "steps": ["Step A", "Step B"],
        }

        result, obs = asyncio.get_event_loop().run_until_complete(
            _run_tool(ctx=ctx, state=state, pending=pending)
        )
        assert result["kind"] == "create_plan"
        assert result["plan_active"] is True
        assert result["total_steps"] == 2

    def test_create_plan_strips_empty_strings(self) -> None:
        _run_tool = self._import_run_tool()
        plan, _ = _make_step_plan()
        ctx = self._make_ctx(step_plan=plan)
        state = self._make_state()
        pending = {
            "action": "create_plan",
            "steps": ["Step A", "", "  ", "Step B"],
        }

        result, obs = asyncio.get_event_loop().run_until_complete(
            _run_tool(ctx=ctx, state=state, pending=pending)
        )
        assert result["total_steps"] == 2
        assert result["steps"] == ["Step A", "Step B"]


# ---------------------------------------------------------------------------
# StepPlan update tracking via plan_step
# ---------------------------------------------------------------------------

class TestPlanStepTracking:
    """Test that plan_step on actions correctly updates the StepPlan."""

    def test_plan_step_update_on_completion(self) -> None:
        """Verify _step_plan_update is called with correct step number."""
        plan, events = _make_step_plan()
        loop = asyncio.get_event_loop()
        loop.run_until_complete(plan.create(["Query data", "Verify", "Summarize"]))

        # Simulate what tool_node does: mark step 1 in_progress then completed
        loop.run_until_complete(plan.update(1, "in_progress", note="Executing sql"))
        loop.run_until_complete(plan.update(1, "completed", note="Got results"))

        plan_state = loop.run_until_complete(plan.list())
        assert plan_state["steps"][0]["status"] == "completed"
        assert plan_state["steps"][1]["status"] == "pending"
        assert plan_state["completed"] == 1

    def test_plan_step_out_of_range_returns_error(self) -> None:
        """StepPlan.update with invalid step number returns error, not exception."""
        plan, _ = _make_step_plan()
        loop = asyncio.get_event_loop()
        loop.run_until_complete(plan.create(["Step 1", "Step 2"]))

        result = loop.run_until_complete(plan.update(5, "completed"))
        assert "error" in result
        # Plan should still be functional
        assert plan.has_pending_steps() is True


# ---------------------------------------------------------------------------
# Finalize auto-completion
# ---------------------------------------------------------------------------

class TestFinalizeAutoCompletion:
    """Test that finalize completes all remaining plan steps."""

    def test_auto_complete_all_steps(self) -> None:
        """Simulate what finalize does: complete all earlier steps, then final step."""
        plan, events = _make_step_plan()
        loop = asyncio.get_event_loop()
        loop.run_until_complete(plan.create([
            "Inspect schema",
            "Run analysis",
            "Verify results",
            "Summarize",
        ]))

        # Simulate: the internal LLM only completed step 1 explicitly during tool rounds
        loop.run_until_complete(plan.update(1, "completed"))

        # Finalize auto-completion logic (mirrors the actual finalize code)
        plan_total = 4
        # Mark last step as in_progress (summarization)
        loop.run_until_complete(
            plan.update(plan_total, "in_progress", note="Summarizing findings.")
        )
        # Complete all earlier steps
        for i in range(1, plan_total):
            loop.run_until_complete(plan.update(i, "completed"))
        # Complete the final step
        loop.run_until_complete(
            plan.update(plan_total, "completed", note="Summary done.")
        )

        assert plan.has_pending_steps() is False
        plan_state = loop.run_until_complete(plan.list())
        assert plan_state["completed"] == 4

    def test_no_plan_no_pending(self) -> None:
        """When no plan was created, has_pending_steps is False (runtime won't reject)."""
        plan, _ = _make_step_plan()
        assert plan.has_pending_steps() is False


# ---------------------------------------------------------------------------
# _VALID_ACTIONS includes create_plan
# ---------------------------------------------------------------------------

class TestValidActions:
    """Verify create_plan is in the valid actions set."""

    def test_create_plan_in_valid_actions(self) -> None:
        from agents.tabular_agent.tabular_reason_graph import _VALID_ACTIONS
        assert "create_plan" in _VALID_ACTIONS

    def test_all_expected_actions_present(self) -> None:
        from agents.tabular_agent.tabular_reason_graph import _VALID_ACTIONS
        expected = {
            "browse", "schema", "preview", "sql", "python",
            "clarify", "done", "activate_skill", "create_plan",
        }
        assert expected == _VALID_ACTIONS


# ---------------------------------------------------------------------------
# Prompt includes planning instructions
# ---------------------------------------------------------------------------

class TestPromptInstructions:
    """Verify the multi-step instruction includes planning and verification guidance."""

    def test_prompt_mentions_create_plan(self) -> None:
        from agents.tabular_agent.tabular_reason_graph import _MULTI_STEP_INSTRUCTION
        assert "create_plan" in _MULTI_STEP_INSTRUCTION

    def test_prompt_mentions_plan_step(self) -> None:
        from agents.tabular_agent.tabular_reason_graph import _MULTI_STEP_INSTRUCTION
        assert "plan_step" in _MULTI_STEP_INSTRUCTION

    def test_prompt_mentions_verification(self) -> None:
        from agents.tabular_agent.tabular_reason_graph import _MULTI_STEP_INSTRUCTION
        assert "verification" in _MULTI_STEP_INSTRUCTION.lower() or "verify" in _MULTI_STEP_INSTRUCTION.lower()

    def test_prompt_mentions_cross_check(self) -> None:
        from agents.tabular_agent.tabular_reason_graph import _MULTI_STEP_INSTRUCTION
        lower = _MULTI_STEP_INSTRUCTION.lower()
        assert "cross-check" in lower or "cross check" in lower or "sanity" in lower


# ---------------------------------------------------------------------------
# State fields
# ---------------------------------------------------------------------------

class TestStateFields:
    """Verify TabularReasonState includes plan fields."""

    def test_state_has_plan_fields(self) -> None:
        from agents.tabular_agent.tabular_reason_graph import TabularReasonState
        annotations = TabularReasonState.__annotations__
        assert "plan_active" in annotations
        assert "plan_total_steps" in annotations

    def test_resume_state_preserves_plan_fields(self) -> None:
        from agents.tabular_agent.tabular_reason_graph import _build_resume_state
        state = {
            "goal": "test",
            "bundle_id": "b1",
            "artifact_id": "a1",
            "plan_active": True,
            "plan_total_steps": 5,
        }
        resumed = _build_resume_state(state)
        assert resumed["plan_active"] is True
        assert resumed["plan_total_steps"] == 5

    def test_resume_state_defaults_plan_fields(self) -> None:
        from agents.tabular_agent.tabular_reason_graph import _build_resume_state
        state = {"goal": "test", "bundle_id": "b1", "artifact_id": "a1"}
        resumed = _build_resume_state(state)
        assert resumed["plan_active"] is False
        assert resumed["plan_total_steps"] == 0
