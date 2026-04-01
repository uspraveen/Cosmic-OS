"""Bounded LangGraph workflow for diagram specialist intents.

Flow: analyze_request -> decide -> generate_definition -> render -> validate -> finalize

The graph:
1. Analyzes the user's natural language description via internal LLM
2. Selects the best renderer (Mermaid/D2/Excalidraw)
3. Generates the diagram definition
4. Renders via CLI (mmdc/d2) or outputs JSON (Excalidraw)
5. Validates output and returns artifacts
"""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from shared.contracts import AgentError, AgentResult, ArtifactManifest, TaskEnvelope

from .config import DiagramAgentConfig
from .internal_llm import (
    analyze_diagram_request,
    modify_diagram,
    validate_diagram_render,
)
from .renderers import (
    RenderError,
    explain_render_error,
    render_d2,
    render_excalidraw,
    render_mermaid,
)
from .skills import build_skills_context, discover_skills

logger = logging.getLogger(__name__)


AGENT_ID = "cosmic/diagram-agent:1.0.0"


@dataclass(frozen=True, slots=True)
class _GraphCtx:
    agent: Any
    task: TaskEnvelope


class DiagramWorkflowState(TypedDict, total=False):
    intent: str
    tool_round: int
    max_tool_rounds: int
    next_action: str
    agent_result: AgentResult | None
    # Input
    description: str
    modification_request: str
    preferred_renderer: str
    output_format: str
    title: str
    context: str
    existing_definition: str
    existing_renderer: str
    # LLM output
    llm_renderer: str
    llm_diagram_type: str
    llm_definition: str
    llm_confidence: float
    llm_reasoning: str
    llm_changes: str
    # Rendering
    render_ok: bool
    render_output_path: str
    render_content: bytes
    render_format: str
    render_error: str
    # Validation
    validated: bool
    validation_pass: bool
    validation_issues: list[str]
    validation_suggestion: str
    validation_attempts: int
    max_validation_attempts: int
    # Skills
    skills_context: str
    # Plan
    plan_active: bool
    plan_step: int | None
    plan_total_steps: int
    plan_steps: list[str]
    accumulated_artifacts: list[Any]
    accumulated_outputs: list[Any]
    # Progress
    analyzed: bool
    rendered: bool
    finalized: bool


def _normalize_output_format(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    return "png" if normalized == "png" else "svg"


def _result_error(
    *, code: str, message: str, retryable: bool = False, next_action: str = "escalate"
) -> AgentResult:
    return AgentResult(
        status="failed",
        output={},
        artifacts=[],
        error=AgentError(
            code=code,
            retryable=retryable,
            message=message,
            next_action=next_action,
        ),
    )


def _bump_round(state: DiagramWorkflowState, *, action: str) -> dict[str, Any]:
    rounds = int(state.get("tool_round") or 0) + 1
    max_rounds = int(state.get("max_tool_rounds") or 6)
    if rounds > max_rounds:
        return {
            "tool_round": rounds,
            "next_action": "finish",
            "agent_result": _result_error(
                code="INTERNAL_ERROR",
                retryable=False,
                message=f"Diagram workflow exceeded max tool rounds while running {action}.",
            ),
        }
    return {"tool_round": rounds}


async def _step_plan_update(
    ctx: _GraphCtx, step: int, status: str, note: str | None = None
) -> None:
    """Update a plan step via the shared StepPlan tool (same pattern as tabular)."""
    step_plan = getattr(ctx.agent, "step_plan", None)
    if step_plan is None:
        return
    try:
        await step_plan.update(step, status, note=note)
    except Exception:  # noqa: BLE001
        logger.debug("diagram.graph.step_plan_update_failed", exc_info=True)


def _build_graph(cfg: DiagramAgentConfig, ctx: _GraphCtx):
    graph = StateGraph(DiagramWorkflowState)
    task = ctx.task
    agent = ctx.agent

    async def analyze_request(state: DiagramWorkflowState) -> dict[str, Any]:
        """Analyze the user's request and generate the diagram definition.

        The LLM may return action="create_plan" for multi-step requests.
        In that case, we create the plan via StepPlan and re-enter this node.
        """
        bump = _bump_round(state, action="analyze")
        intent = state.get("intent", "")

        # Discover skills for context
        skills = discover_skills()
        skills_ctx = build_skills_context(skills)

        if intent == "diagram.modify":
            llm_result = await modify_diagram(
                cfg=cfg,
                http_client=__import__("httpx").AsyncClient(timeout=30),
                renderer=state.get("existing_renderer", "mermaid"),
                existing_definition=state.get("existing_definition", ""),
                modification_request=state.get("modification_request", ""),
                skills_context=skills_ctx,
                task_id=task.task_id,
                session_id=task.session_id,
                source=task.source,
                source_id=task.source_id,
                channel=task.channel,
            )
            if llm_result:
                return {
                    **bump,
                    "llm_renderer": llm_result.get(
                        "renderer", state.get("existing_renderer", "mermaid")
                    ),
                    "llm_definition": llm_result.get("definition", ""),
                    "llm_confidence": llm_result.get("confidence", 0.0),
                    "llm_changes": llm_result.get("changes", ""),
                    "analyzed": True,
                    "skills_context": skills_ctx,
                }
        else:
            # diagram.create
            desc = state.get("description", "")
            if not desc:
                desc = task.input.get("description", "") or task.input.get("query", "")

            # If plan is active, inject the current step text
            plan_step_text = ""
            if state.get("plan_active"):
                plan_steps = state.get("plan_steps") or []
                current_step = int(state.get("plan_step") or 1)
                if 0 < current_step <= len(plan_steps):
                    plan_step_text = plan_steps[current_step - 1]

            llm_result = await analyze_diagram_request(
                cfg=cfg,
                http_client=__import__("httpx").AsyncClient(timeout=30),
                description=desc,
                preferred_renderer=state.get("preferred_renderer"),
                skills_context=skills_ctx,
                plan_step_text=plan_step_text,
                task_id=task.task_id,
                session_id=task.session_id,
                source=task.source,
                source_id=task.source_id,
                channel=task.channel,
            )
            if llm_result:
                action = llm_result.get("action", "generate")

                # Handle create_plan (same pattern as tabular agent)
                if action == "create_plan":
                    raw_steps = llm_result.get("steps") or []
                    steps = [str(s).strip() for s in raw_steps if str(s).strip()][:8]
                    if not steps:
                        # Empty plan — skip planning, proceed to generate
                        return {**bump, "analyzed": False, "plan_active": False}

                    validation_cap = max(1, int(state.get("max_validation_attempts") or 2))
                    required_rounds = 1 + (len(steps) * ((2 * validation_cap) + 1))
                    effective_max_rounds = max(
                        int(state.get("max_tool_rounds") or cfg.diagram_max_tool_rounds),
                        required_rounds,
                    )

                    step_plan = getattr(agent, "step_plan", None)
                    if step_plan is not None:
                        try:
                            await step_plan.create(steps)
                            await step_plan.update(1, "in_progress")
                        except Exception:
                            logger.debug(
                                "diagram.graph.create_plan_failed", exc_info=True
                            )

                    return {
                        **bump,
                        "plan_active": True,
                        "plan_steps": steps,
                        "plan_total_steps": len(steps),
                        "plan_step": 1,
                        "max_tool_rounds": effective_max_rounds,
                        "analyzed": False,  # Re-enter analyze to execute step 1
                    }

                # Handle generate (normal flow)
                return {
                    **bump,
                    "llm_renderer": llm_result.get("renderer", "mermaid"),
                    "llm_diagram_type": llm_result.get("diagram_type", "other"),
                    "llm_definition": llm_result.get("definition", ""),
                    "llm_confidence": llm_result.get("confidence", 0.0),
                    "llm_reasoning": llm_result.get("reasoning", ""),
                    "analyzed": True,
                    "skills_context": skills_ctx,
                }

        # LLM failed — return error
        return {
            **bump,
            "next_action": "finish",
            "agent_result": _result_error(
                code="INTERNAL_ERROR",
                retryable=True,
                message="Internal LLM failed to analyze the diagram request.",
                next_action="retry",
            ),
        }

    async def render_diagram(state: DiagramWorkflowState) -> dict[str, Any]:
        """Render the generated definition via CLI. Writes to runs/artifacts/<task_id>/diagram_agent/."""
        bump = _bump_round(state, action="render")
        renderer = state.get("llm_renderer", "mermaid")
        definition = state.get("llm_definition", "")
        output_format = state.get("output_format", "svg")

        if not definition:
            return {
                **bump,
                "render_ok": False,
                "render_error": "No diagram definition generated.",
                "rendered": True,
            }

        task_artifact_dir = agent._task_artifact_dir(task.task_id)
        task_artifact_dir.mkdir(parents=True, exist_ok=True)

        try:
            if renderer == "mermaid":
                ext = "png" if output_format == "png" else "svg"
                out_path = task_artifact_dir / f"diagram.{ext}"
                result = await render_mermaid(
                    definition,
                    mmdc_path=cfg.mmdc_path,
                    output_format=output_format,
                    background=cfg.mermaid_background,
                    theme=cfg.default_theme,
                    output_path=out_path,
                )
            elif renderer == "d2":
                ext = "png" if output_format == "png" else "svg"
                out_path = task_artifact_dir / f"diagram.{ext}"
                result = await render_d2(
                    definition,
                    d2_path=cfg.d2_path,
                    output_format=output_format,
                    sketch=cfg.d2_sketch,
                    pad=cfg.d2_pad,
                    output_path=out_path,
                )
            elif renderer == "excalidraw":
                out_path = task_artifact_dir / "diagram.excalidraw"
                result = render_excalidraw(definition, output_path=out_path)
            else:
                return {
                    **bump,
                    "render_ok": False,
                    "render_error": f"Unknown renderer: {renderer}",
                    "rendered": True,
                }

            return {
                **bump,
                "render_ok": True,
                "render_output_path": str(result.get("output_path", "")),
                "render_content": result.get("content", b""),
                "render_format": result.get("output_format", output_format),
                "rendered": True,
            }
        except RenderError as exc:
            return {
                **bump,
                "render_ok": False,
                "render_error": explain_render_error(exc),
                "rendered": True,
            }
        except Exception as exc:
            return {
                **bump,
                "render_ok": False,
                "render_error": f"Unexpected render error: {exc}",
                "rendered": True,
            }

    async def validate_diagram(state: DiagramWorkflowState) -> dict[str, Any]:
        """Validate rendered diagram using vision or source analysis.

        If validation fails and attempts < max, loops back to render with
        the suggestion as context for re-generation.
        """
        bump = _bump_round(state, action="validate")
        renderer = state.get("llm_renderer", "mermaid")
        definition = state.get("llm_definition", "")
        diagram_type = state.get("llm_diagram_type", "other")
        render_ok = state.get("render_ok", False)
        png_content = state.get("render_content", b"")
        render_format = state.get("render_format", "svg")
        attempts = int(state.get("validation_attempts") or 0) + 1
        max_attempts = int(state.get("max_validation_attempts") or 2)

        # Skip validation if render failed or no LLM available
        if not render_ok or not definition:
            return {
                **bump,
                "validated": True,
                "validation_pass": render_ok,
                "validation_attempts": attempts,
            }

        # For non-PNG formats, try to get PNG for vision
        png_for_vision: bytes | None = None
        if render_format == "png" and png_content:
            png_for_vision = png_content
        elif render_format == "svg" and png_content:
            # SVG — can't do vision, fall back to text-based validation
            png_for_vision = None

        validation = await validate_diagram_render(
            cfg=cfg,
            http_client=__import__("httpx").AsyncClient(timeout=30),
            renderer=renderer,
            definition=definition,
            diagram_type=diagram_type,
            png_bytes=png_for_vision,
            task_id=task.task_id,
            session_id=task.session_id,
            source=task.source,
            source_id=task.source_id,
            channel=task.channel,
        )

        if validation is None:
            # Validation unavailable — pass by default
            return {
                **bump,
                "validated": True,
                "validation_pass": True,
                "validation_attempts": attempts,
            }

        passed = bool(validation.get("pass", True))
        issues = validation.get("issues", [])
        suggestion = str(validation.get("suggestion", ""))

        if passed:
            return {
                **bump,
                "validated": True,
                "validation_pass": True,
                "validation_issues": issues,
                "validation_suggestion": "",
                "validation_attempts": attempts,
            }

        # Validation failed
        if attempts >= max_attempts:
            # Max attempts reached — accept with warning
            logger.warning(
                "diagram.validation_failed_max_attempts: %s issues=%s",
                renderer,
                issues,
            )
            return {
                **bump,
                "validated": True,
                "validation_pass": False,
                "validation_issues": issues,
                "validation_suggestion": suggestion,
                "validation_attempts": attempts,
            }

        # Re-generate: send validation feedback to LLM to produce corrected definition
        from .internal_llm import regenerate_diagram_with_feedback

        regen_result = await regenerate_diagram_with_feedback(
            cfg=cfg,
            http_client=__import__("httpx").AsyncClient(timeout=30),
            renderer=renderer,
            existing_definition=definition,
            validation_issues=issues,
            validation_suggestion=suggestion,
            diagram_type=diagram_type,
            task_id=task.task_id,
            session_id=task.session_id,
            source=task.source,
            source_id=task.source_id,
            channel=task.channel,
        )

        new_definition = definition  # fallback to original if regeneration fails
        if regen_result and regen_result.get("definition"):
            new_definition = regen_result["definition"]

        return {
            **bump,
            "validated": False,
            "validation_pass": False,
            "validation_issues": issues,
            "validation_suggestion": suggestion,
            "validation_attempts": attempts,
            "llm_definition": new_definition,
            "rendered": False,  # Reset so render node runs again
        }

    async def finalize(state: DiagramWorkflowState) -> dict[str, Any]:
        """Build artifacts for current cycle. If plan has more steps, loop back."""
        if state.get("agent_result") is not None:
            # Error result or pre-built — skip remaining plan steps (don't mark as completed)
            if state.get("plan_active"):
                current = int(state.get("plan_step") or 1)
                total = int(state.get("plan_total_steps") or 0)
                for i in range(current, total + 1):
                    await _step_plan_update(
                        ctx, i, "skipped", note="Task ended before step executed"
                    )
            return {}

        renderer = state.get("llm_renderer", "mermaid")
        definition = state.get("llm_definition", "")
        diagram_type = state.get("llm_diagram_type", "other")
        title = state.get("title") or state.get("llm_reasoning", "")[:100]
        render_ok = state.get("render_ok", False)
        render_error = state.get("render_error", "")
        output_format = state.get("render_format", "svg")

        task_artifact_dir = agent._task_artifact_dir(task.task_id)
        step_artifacts: list[ArtifactManifest] = []

        if render_ok:
            # Rendered output artifact
            if renderer == "excalidraw":
                out_mime = "application/json"
                out_filename = "diagram.excalidraw"
            elif output_format == "png":
                out_mime = "image/png"
                out_filename = "diagram.png"
            else:
                out_mime = "image/svg+xml"
                out_filename = "diagram.svg"

            out_path = task_artifact_dir / out_filename
            if out_path.exists():
                step_artifacts.append(
                    agent._artifact_manifest(
                        task_id=task.task_id,
                        path=out_path,
                        mime=out_mime,
                        kind="output",
                        audience="deliverable",
                    )
                )

        if definition:
            source_ext = {"mermaid": "mmd", "d2": "d2", "excalidraw": "excalidraw"}.get(
                renderer, "txt"
            )
            source_mime = (
                "application/json" if renderer == "excalidraw" else "text/plain"
            )
            source_path = task_artifact_dir / f"diagram_source.{source_ext}"
            source_path.write_text(definition, encoding="utf-8")
            step_artifacts.append(
                agent._artifact_manifest(
                    task_id=task.task_id,
                    path=source_path,
                    mime=source_mime,
                    kind="output",
                    audience="supporting",
                )
            )

        step_output = {
            "renderer": renderer,
            "diagram_type": diagram_type,
            "title": title,
            "definition": definition,
            "rendered": render_ok,
            "render_format": output_format if render_ok else None,
            "render_error": render_error if not render_ok else None,
        }

        # ── Multi-step plan: check if more steps remain ───────────────
        plan_active = state.get("plan_active", False)
        if plan_active:
            current_step = int(state.get("plan_step") or 1)
            total_steps = int(state.get("plan_total_steps") or 0)

            # Mark current step as completed
            await _step_plan_update(
                ctx, current_step, "completed", note=step_output.get("title", "")
            )

            if current_step < total_steps:
                # More steps remain — accumulate artifacts, increment, loop back to analyze
                accumulated = list(state.get("accumulated_artifacts") or [])
                accumulated.extend(step_artifacts)
                accumulated_outputs = list(state.get("accumulated_outputs") or [])
                accumulated_outputs.append(step_output)

                next_step = current_step + 1
                await _step_plan_update(
                    ctx, next_step, "in_progress", note="Starting next plan step"
                )

                return {
                    "plan_step": next_step,
                    "analyzed": False,  # Reset so analyze runs again
                    "rendered": False,
                    "render_ok": False,
                    "render_output_path": "",
                    "render_content": b"",
                    "render_format": "",
                    "render_error": "",
                    "validated": False,
                    "validation_pass": False,
                    "validation_issues": [],
                    "validation_suggestion": "",
                    "validation_attempts": 0,
                    "llm_definition": "",
                    "llm_renderer": "",
                    "llm_diagram_type": "",
                    "llm_confidence": 0.0,
                    "llm_reasoning": "",
                    "accumulated_artifacts": accumulated,
                    "accumulated_outputs": accumulated_outputs,
                }

            # All steps completed — include accumulated artifacts
            all_artifacts = list(state.get("accumulated_artifacts") or [])
            all_artifacts.extend(step_artifacts)
            all_outputs = list(state.get("accumulated_outputs") or [])
            all_outputs.append(step_output)
        else:
            all_artifacts = step_artifacts
            all_outputs = [step_output]

        # ── Final result ───────────────────────────────────────────────
        return {
            "agent_result": AgentResult(
                status="completed",
                output={
                    "diagrams": all_outputs,
                    "count": len(all_outputs),
                    "validation_pass": state.get("validation_pass", True),
                    "validation_issues": state.get("validation_issues", [])
                    if not state.get("validation_pass", True)
                    else [],
                },
                artifacts=all_artifacts,
                error=None,
            ),
            "finalized": True,
        }

    # ── Graph topology ────────────────────────────────────────────────

    def route_after_analyze(state: DiagramWorkflowState) -> str:
        if state.get("agent_result") is not None:
            return "finish"
        # create_plan was handled — re-enter analyze to execute step 1
        if state.get("plan_active") and not state.get("analyzed"):
            return "analyze"
        if not state.get("analyzed"):
            return "finish"
        return "render"

    def route_after_render(state: DiagramWorkflowState) -> str:
        return "validate"

    def route_after_validate(state: DiagramWorkflowState) -> str:
        # Validation passed or max attempts reached — finalize
        if state.get("validation_pass") or state.get("validated"):
            return "finalize"
        # Validation failed and we have attempts left — re-render
        return "render"

    def route_after_finalize(state: DiagramWorkflowState) -> str:
        # If more plan steps remain, loop back to analyze
        if state.get("plan_active") and not state.get("finalized"):
            return "analyze"
        return "end"

    # Nodes
    graph.add_node("analyze_request", analyze_request)
    graph.add_node("render_diagram", render_diagram)
    graph.add_node("validate_diagram", validate_diagram)
    graph.add_node("finalize", finalize)

    # Edges
    graph.add_edge(START, "analyze_request")
    graph.add_conditional_edges(
        "analyze_request",
        route_after_analyze,
        {"render": "render_diagram", "analyze": "analyze_request", "finish": END},
    )
    graph.add_conditional_edges(
        "render_diagram",
        route_after_render,
        {"validate": "validate_diagram"},
    )
    graph.add_conditional_edges(
        "validate_diagram",
        route_after_validate,
        {"finalize": "finalize", "render": "render_diagram"},
    )
    graph.add_conditional_edges(
        "finalize",
        route_after_finalize,
        {"analyze": "analyze_request", "end": END},
    )

    return graph.compile()


async def run_diagram_langgraph(*, agent: Any, task: TaskEnvelope) -> AgentResult:
    """Run a bounded LangGraph workflow for diagram specialist tasks."""
    cfg: DiagramAgentConfig = agent._cfg

    # Extract input
    desc = task.input.get("description", "") or task.input.get("query", "")
    preferred_renderer = task.input.get("preferred_renderer")
    output_format = _normalize_output_format(task.input.get("output_format"))
    title = task.input.get("title", "")
    context = task.input.get("context", "")

    if not desc and task.intent == "diagram.create":
        return AgentResult(
            status="failed",
            output={},
            artifacts=[],
            error=AgentError(
                code="INVALID_INPUT",
                retryable=False,
                message="diagram.create requires a 'description' field.",
                next_action="escalate",
            ),
        )

    app = _build_graph(cfg, ctx=_GraphCtx(agent=agent, task=task))
    initial_state: DiagramWorkflowState = {
        "intent": task.intent,
        "tool_round": 0,
        "max_tool_rounds": cfg.diagram_max_tool_rounds,
        "next_action": "finish",
        "agent_result": None,
        "description": desc,
        "preferred_renderer": preferred_renderer or "",
        "output_format": output_format,
        "title": title,
        "context": context,
        # For modify
        "modification_request": task.input.get("modification_request", ""),
        "existing_definition": task.input.get("existing_definition", ""),
        "existing_renderer": task.input.get("renderer", "mermaid"),
        # Validation
        "validated": False,
        "validation_pass": False,
        "validation_issues": [],
        "validation_suggestion": "",
        "validation_attempts": 0,
        "max_validation_attempts": 2,
        # Plan
        "plan_active": False,
        "plan_step": None,
        "plan_total_steps": 0,
        "plan_steps": [],
        "accumulated_artifacts": [],
        "accumulated_outputs": [],
    }

    final_state = await app.ainvoke(initial_state)
    result = final_state.get("agent_result")
    if isinstance(result, AgentResult):
        return result

    return AgentResult(
        status="failed",
        output={},
        artifacts=[],
        error=AgentError(
            code="INTERNAL_ERROR",
            retryable=False,
            message="Diagram workflow completed without producing a result.",
            next_action="escalate",
        ),
    )
