"""
LangGraph multi-step tabular reasoning (``tabular.reason_workbook``).

Bounded tool rounds; deterministic bundle operations only; MiMo proposes JSON actions.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from shared.contracts import TaskEnvelope
from shared.tabular_artifacts import validate_safe_sheet_id

from .config import TabularAgentConfig
from .internal_llm import invoke_tabular_mimo
from .internal_workflow import extract_json_object
from .orchestrator_clarify import request_orchestrator_task_input
from .prompt_assets import build_internal_context
from .sandbox import persist_bundle_python_script, provision_venv, run_python_script, validate_pip_packages, write_execution_receipt

logger = logging.getLogger(__name__)

_VALID_ACTIONS = frozenset({"browse", "schema", "preview", "sql", "python", "clarify", "done"})

_MULTI_STEP_INSTRUCTION = """\
You control internal tools over an already-parsed spreadsheet bundle. Reply with **one JSON object only** (no markdown).

Keys:
- "action": one of "browse", "schema", "preview", "sql", "python", "clarify", "done"
- "sheet_id": string or null — required for "preview"; optional filter for "schema"
- "sql": string or null — single read-only SELECT for "sql" (DuckDB views: s_<sheet_id>)
- "python_code": string or null — for "python" only; duckdb/pandas; cwd is bundle root
- "pip_install": array of strings or null — for "python" only; packages to install before running (e.g. ["openpyxl", "matplotlib"]); omit if not needed
- "question": string or null — for "clarify" only: concise user-facing question (blocking ambiguity)
- "options": array of strings or null — for "clarify" only: short choices when applicable (max ~8)
- "ambiguity": string or null — for "clarify" only: internal label (e.g. "multiple_sheets", "metric_definition")
- "answer": string or null — when action is "done", concise result for the orchestrator
- "rationale": short string

Rules:
- Prefer "sql" when the goal needs tabular aggregation/filtering.
- Use "schema" / "preview" to disambiguate columns or sheet choice.
- Use "browse" for a lightweight workbook list/handles reminder.
- Use **"clarify" at most once** when ambiguity blocks correct execution and cannot be resolved with internal tools
  (e.g. multiple plausible sheets, unclear metric, fiscal calendar, unit/currency). Requires an orchestrator parent task.
- Set action to "done" when the goal is satisfied or you cannot proceed.
"""


class TabularReasonState(TypedDict, total=False):
    goal: str
    bundle_id: str
    artifact_id: str
    transcript: str
    tool_round: int
    max_tool_rounds: int
    allow_python: bool
    clarify_used: bool
    pending: dict[str, Any]
    steps_log: list[dict[str, Any]]
    finish_reason: str
    response: str
    bundle_root: str
    last_tool_result: dict[str, Any]
    suspended: bool
    input_request_id: str
    resume_state: dict[str, Any]
    resumed: bool
    analysis_step_started: bool


@dataclass(frozen=True, slots=True)
class _GraphCtx:
    agent: Any
    cfg: TabularAgentConfig
    http_client: httpx.AsyncClient
    task: TaskEnvelope


def _build_resume_state(state: TabularReasonState) -> dict[str, Any]:
    return {
        "goal": state.get("goal"),
        "bundle_id": state.get("bundle_id"),
        "artifact_id": state.get("artifact_id"),
        "transcript": state.get("transcript") or "",
        "tool_round": int(state.get("tool_round") or 0),
        "max_tool_rounds": int(state.get("max_tool_rounds") or 5),
        "allow_python": bool(state.get("allow_python", True)),
        "clarify_used": bool(state.get("clarify_used", False)),
        "steps_log": list(state.get("steps_log") or []),
        "bundle_root": state.get("bundle_root"),
    }


def _append_step(state: TabularReasonState, entry: dict[str, Any]) -> list[dict[str, Any]]:
    log = list(state.get("steps_log") or [])
    log.append(entry)
    return log


async def _step_plan_create(ctx: _GraphCtx, state: TabularReasonState) -> None:
    step_plan = getattr(ctx.agent, "step_plan", None)
    if step_plan is None:
        return
    resumed = bool(state.get("resumed"))
    steps = [
        "Resume after user clarification" if resumed else "Inspect workbook context",
        "Run internal spreadsheet analysis",
        "Summarize the result for the orchestrator",
    ]
    try:
        await step_plan.create(steps)
        await step_plan.update(1, "in_progress")
    except Exception:  # noqa: BLE001
        logger.debug("tabular.graph.step_plan_create_failed", exc_info=True)


async def _step_plan_update(ctx: _GraphCtx, step: int, status: str, note: str | None = None) -> None:
    step_plan = getattr(ctx.agent, "step_plan", None)
    if step_plan is None:
        return
    try:
        await step_plan.update(step, status, note=note)
    except Exception:  # noqa: BLE001
        logger.debug("tabular.graph.step_plan_update_failed", exc_info=True)


async def _run_tool(
    *,
    ctx: _GraphCtx,
    state: TabularReasonState,
    pending: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """Execute one tool; returns (result_dict, observation_text_for_transcript)."""
    bundle_id = state["bundle_id"]
    artifact_id = state["artifact_id"]
    root = Path(state["bundle_root"])
    action = str(pending.get("action") or "").strip().lower()
    agent = ctx.agent
    cfg = ctx.cfg
    task = ctx.task

    if action == "clarify":
        _provenance = {
            "child_task_id": task.task_id,
            "session_id": task.session_id,
            "request_id": (agent._safe(task.input.get("request_id")) if isinstance(task.input, dict) else None),  # noqa: SLF001
            "channel": task.channel,
            "source": task.source,
            "source_id": task.source_id,
        }
        if state.get("clarify_used"):
            e: dict[str, Any] = {"error": "clarify_already_used", "kind": "clarify", "clarify_status": "clarify_already_used"}
            return e, json.dumps(e, ensure_ascii=False)
        parent_task_id = str(getattr(task, "parent_task_id", None) or "").strip()
        if not parent_task_id:
            e = {
                "error": "clarify_requires_parent_task_id",
                "kind": "clarify",
                "clarify_status": "missing_parent_task",
                "hint": "tabular.reason_workbook must run as a child of an orchestrator task (sheets_reason / delegate).",
            }
            return e, json.dumps(e, ensure_ascii=False)
        question = pending.get("question")
        if not isinstance(question, str) or not question.strip():
            e = {"error": "clarify_requires_question", "kind": "clarify", "clarify_status": "missing_question"}
            return e, json.dumps(e, ensure_ascii=False)
        raw_opts = pending.get("options")
        options: list[str] = []
        if isinstance(raw_opts, list):
            options = [str(o).strip() for o in raw_opts if str(o).strip()][:12]
        try:
            resp = await request_orchestrator_task_input(
                cfg=cfg,
                http_client=ctx.http_client,
                parent_task_id=parent_task_id,
                question=question.strip(),
                options=options,
                channel=task.channel,
                wait_timeout_sec=0.0,
                specialist_agent_id=str(getattr(agent, "agent_id", "") or "cosmic/tabular-agent:1.0.0"),
            )
        except Exception as exc:  # noqa: BLE001
            e = {
                "error": str(exc)[:500],
                "kind": "clarify",
                "clarify_status": "relay_error",
                "hint": "failed_to_publish_task_input_request",
            }
            return e, json.dumps(e, ensure_ascii=False)
        input_request_id = str(resp.get("input_request_id") or "").strip()
        if not input_request_id:
            e = {"error": "missing_input_request_id", "kind": "clarify", "clarify_status": "relay_error"}
            return e, json.dumps(e, ensure_ascii=False)

        suspended_payload: dict[str, Any] = {
            "reason": "tabular_clarify",
            "parent_task_id": parent_task_id,
            **_provenance,
            "input_request_id": input_request_id,
            "question": question.strip(),
            "options": options,
            "ambiguity": pending.get("ambiguity"),
            "resume_intent": "agent.resume",
            "resume_payload": _build_resume_state({**state, "clarify_used": True}),
        }
        await agent.emit_event(task.task_id, "task.suspended", suspended_payload)
        out: dict[str, Any] = {
            "kind": "clarify",
            "clarify_status": "suspended",
            "input_request_id": input_request_id,
            "question": question.strip(),
        }
        return out, json.dumps(out, ensure_ascii=False)

    if action == "browse":
        data = agent._load_bundle(bundle_id)  # noqa: SLF001
        wbs = data.get("workbooks") if isinstance(data.get("workbooks"), list) else []
        slim = [{"artifact_id": w.get("artifact_id"), "parse_status": w.get("parse_status"), "handles": w.get("handles")} for w in wbs if isinstance(w, dict)][:12]
        out = {"kind": "browse", "workbooks": slim}
        return out, json.dumps(out, ensure_ascii=False)[:8000]

    if action == "schema":
        cat_path = root / "sheet_catalog.json"
        raw = json.loads(cat_path.read_text(encoding="utf-8"))
        sheets = raw.get("sheets") if isinstance(raw.get("sheets"), list) else []
        sid = pending.get("sheet_id")
        if isinstance(sid, str) and sid.strip():
            try:
                vs = validate_safe_sheet_id(sid.strip())
                sheets = [s for s in sheets if isinstance(s, dict) and str(s.get("sheet_id")) == vs]
            except ValueError as exc:
                return {"error": str(exc)}, str(exc)
        out = {"kind": "schema", "sheets": sheets[:80]}
        return out, json.dumps(out, ensure_ascii=False)[:9000]

    if action == "preview":
        raw_sid = pending.get("sheet_id")
        if not isinstance(raw_sid, str) or not raw_sid.strip():
            return {"error": "preview requires sheet_id"}, "preview requires sheet_id"
        try:
            vs = validate_safe_sheet_id(raw_sid.strip())
        except ValueError as exc:
            return {"error": str(exc)}, str(exc)
        path = root / "sheets" / f"{vs}_preview.md"
        if not path.is_file():
            return {"error": "unknown sheet preview"}, "unknown sheet preview"
        text = path.read_text(encoding="utf-8")[:8000]
        out = {"kind": "preview", "sheet_id": vs, "preview_md": text}
        return out, json.dumps(out, ensure_ascii=False)[:9000]

    if action == "sql":
        sql = pending.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            return {"error": "sql action requires sql"}, "missing sql"
        try:
            out = agent.sync_run_select(bundle_id, artifact_id, sql.strip())  # noqa: SLF001
            return out, json.dumps(out, ensure_ascii=False)[:8000]
        except Exception as exc:  # noqa: BLE001
            logger.warning("tabular.graph.sql_failed: %s", exc)
            e = {"error": str(exc)[:500]}
            return e, json.dumps(e, ensure_ascii=False)

    if action == "python":
        if not state.get("allow_python", True):
            return {"error": "python disabled"}, "python disabled"
        py_code = pending.get("python_code")
        if not isinstance(py_code, str) or not py_code.strip():
            return {"error": "python action requires python_code"}, "missing python_code"
        execution_id = f"exec_{uuid4().hex[:14]}"
        network_enabled = bool(getattr(cfg, "sandbox_allow_network", False))
        pip_enabled = bool(getattr(cfg, "sandbox_allow_pip", False))
        raw_pip = pending.get("pip_install")
        requested_packages: list[str] = []
        if isinstance(raw_pip, list):
            requested_packages = [str(p).strip() for p in raw_pip if str(p).strip()]
        try:
            script_path = persist_bundle_python_script(
                bundle_root=root,
                execution_id=execution_id,
                code=py_code.strip(),
                allow_network=network_enabled,
            )
            python_exe = None
            installed_packages: list[str] = []
            pip_log: dict[str, Any] = {}
            if requested_packages and pip_enabled:
                clean_pkgs = validate_pip_packages(requested_packages)
                if clean_pkgs:
                    cache_root_raw = str(getattr(cfg, "sandbox_venv_cache_root", "") or "").strip()
                    cache_root = Path(cache_root_raw) if cache_root_raw else None
                    python_exe, installed_packages, pip_log = provision_venv(
                        packages=clean_pkgs,
                        cache_root=cache_root,
                        pip_timeout_sec=float(getattr(cfg, "sandbox_pip_timeout_sec", 120.0) or 120.0),
                    )
            elif requested_packages and not pip_enabled:
                pip_log = {"skipped": True, "reason": "sandbox_allow_pip=false", "packages_requested": requested_packages}
            run_out = run_python_script(
                script_path=script_path,
                cwd=root,
                timeout_sec=cfg.sandbox_timeout_sec,
                bundle_root=root,
                python_executable=python_exe,
            )
            parent_tid = str(getattr(task, "parent_task_id", None) or "").strip() or None
            write_execution_receipt(
                bundle_root=root,
                execution_id=execution_id,
                task_id=task.task_id,
                session_id=task.session_id,
                artifact_id=artifact_id,
                receipt={
                    "kind": "tabular_sandbox",
                    "graph": True,
                    "parent_task_id": parent_tid,
                    "network_enabled": network_enabled,
                    "packages_installed": installed_packages,
                    "pip_log": pip_log if pip_log else None,
                    "environment_mode": "isolated_minimal",
                    "exit_code": run_out.get("exit_code"),
                    "stdout": run_out.get("stdout"),
                    "stderr": run_out.get("stderr"),
                    "duration_ms": run_out.get("duration_ms"),
                    "script_relative": str(script_path.relative_to(root)).replace("\\", "/"),
                },
            )
            out = {
                "kind": "python",
                "execution_id": execution_id,
                "exit_code": run_out.get("exit_code"),
                "stdout": (run_out.get("stdout") or "")[:8000],
                "stderr": (run_out.get("stderr") or "")[:4000],
                "network_enabled": network_enabled,
                "packages_installed": installed_packages,
            }
            return out, json.dumps(out, ensure_ascii=False)[:8000]
        except ValueError as exc:
            return {"error": str(exc)}, str(exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("tabular.graph.python_failed")
            e = {"error": str(exc)[:500]}
            return e, json.dumps(e, ensure_ascii=False)

    return {"error": f"unknown action: {action}"}, json.dumps({"error": action})


def _build_graph(ctx: _GraphCtx) -> Any:
    async def bootstrap(state: TabularReasonState) -> dict[str, Any]:
        await ctx.agent._emit_stage(ctx.task.task_id, "reason_inspect", "Loading bundle context (LangGraph).")  # noqa: SLF001
        await _step_plan_create(ctx, state)
        root = ctx.agent._bundle_disk_path(state["bundle_id"], state["artifact_id"])  # noqa: SLF001
        preview_path = root / "preview.md"
        cat_path = root / "sheet_catalog.json"
        preview_excerpt = preview_path.read_text(encoding="utf-8")[:6000] if preview_path.is_file() else ""
        catalog_excerpt = cat_path.read_text(encoding="utf-8")[:9000] if cat_path.is_file() else "{}"
        initial = (
            f"## User goal\n{state['goal']}\n\n"
            f"## sheet_catalog.json (truncated)\n```\n{catalog_excerpt}\n```\n\n"
            f"## preview.md (truncated)\n```\n{preview_excerpt}\n```\n"
        )
        existing_transcript = str(state.get("transcript") or "").strip()
        transcript = f"{existing_transcript}\n\n{initial}" if existing_transcript else initial
        await _step_plan_update(
            ctx,
            1,
            "completed",
            note="Resumed from clarification." if state.get("resumed") else "Loaded workbook preview and sheet catalog.",
        )
        return {
            "transcript": transcript,
            "bundle_root": str(root.resolve()),
            "tool_round": int(state.get("tool_round") or 0),
            "analysis_step_started": bool(state.get("analysis_step_started")),
            "steps_log": _append_step(state, {"step": "bootstrap", "ok": True}),
        }

    async def decide(state: TabularReasonState) -> dict[str, Any]:
        await ctx.agent._emit_stage(ctx.task.task_id, "reason_plan", "Planning next internal tool step.")  # noqa: SLF001
        analysis_started = bool(state.get("analysis_step_started"))
        if not analysis_started:
            await _step_plan_update(ctx, 2, "in_progress", note="Choosing the next tabular action.")
        tr = state.get("transcript") or ""
        rounds = int(state.get("tool_round") or 0)
        max_r = int(state.get("max_tool_rounds") or 5)
        user_msg = (
            f"{_MULTI_STEP_INSTRUCTION}\n\n"
            f"tool_round so far: {rounds} (max tool executions: {max_r}).\n\n"
            f"## Transcript\n{tr[-24000:]}\n"
        )
        system = "\n\n".join(
            [
                "You are the COSMIC tabular specialist internal planner.",
                build_internal_context("plan", include_fpna=ctx.cfg.include_financial_fpna_prompt),
            ]
        )
        rid = ctx.agent._safe(ctx.task.input.get("request_id")) if isinstance(ctx.task.input, dict) else None  # noqa: SLF001
        raw = await invoke_tabular_mimo(
            cfg=ctx.cfg,
            http_client=ctx.http_client,
            system_content=system,
            user_message=user_msg,
            task_id=ctx.task.task_id,
            session_id=ctx.task.session_id,
            request_id=rid or None,
            source=ctx.task.source,
            source_id=ctx.task.source_id,
            channel=ctx.task.channel,
            operation="tabular.internal_llm.reason_step",
            max_output_chars=14_000,
            temperature=0.1,
        )
        parsed = extract_json_object(raw or "") if raw else None
        if not parsed:
            return {
                "pending": {"action": "done", "answer": "Planner did not return valid JSON.", "rationale": "parse_error"},
                "finish_reason": "llm_parse_error",
                "analysis_step_started": True,
                "steps_log": _append_step(state, {"step": "decide", "ok": False, "raw": (raw or "")[:500]}),
            }
        return {
            "pending": parsed,
            "analysis_step_started": True,
            "steps_log": _append_step(state, {"step": "decide", "ok": True, "action": parsed.get("action")}),
        }

    def route_after_decide(state: TabularReasonState) -> str:
        if state.get("finish_reason") == "llm_parse_error":
            return "finalize"
        p = state.get("pending") or {}
        act = str(p.get("action") or "").strip().lower()
        if act == "done":
            return "finalize"
        if act == "clarify" and state.get("clarify_used"):
            return "finalize"
        if int(state.get("tool_round") or 0) >= int(state.get("max_tool_rounds") or 5):
            return "finalize"
        if act not in _VALID_ACTIONS:
            return "finalize"
        return "tool"

    async def tool_node(state: TabularReasonState) -> dict[str, Any]:
        await ctx.agent._emit_stage(ctx.task.task_id, "reason_execute", "Running internal tabular tool.")  # noqa: SLF001
        pending = state.get("pending") or {}
        if str(pending.get("action") or "").strip().lower() == "clarify":
            await ctx.agent._emit_stage(ctx.task.task_id, "reason_clarify", "Requesting user input via orchestrator task-input relay.")  # noqa: SLF001
        result, obs = await _run_tool(ctx=ctx, state=state, pending=pending)
        tr = (state.get("transcript") or "") + f"\n\n--- tool_round {state.get('tool_round', 0) + 1} ---\n{obs}\n"
        out: dict[str, Any] = {
            "transcript": tr,
            "tool_round": int(state.get("tool_round") or 0) + 1,
            "last_tool_result": result,
            "steps_log": _append_step(
                state,
                {"step": "tool", "ok": "error" not in result, "kind": pending.get("action")},
            ),
        }
        if str(pending.get("action") or "").strip().lower() == "clarify":
            out["clarify_used"] = True
            if str(result.get("clarify_status") or "").strip().lower() == "suspended":
                await _step_plan_update(ctx, 2, "in_progress", note="Waiting for user clarification via orchestrator.")
                out["finish_reason"] = "awaiting_clarification"
                out["response"] = "Awaiting user clarification."
                out["suspended"] = True
        return out

    def route_after_tool(state: TabularReasonState) -> str:
        if state.get("finish_reason") == "awaiting_clarification":
            return "finalize"
        return "decide"

    async def finalize(state: TabularReasonState) -> dict[str, Any]:
        if state.get("finish_reason") == "awaiting_clarification":
            last = state.get("last_tool_result") if isinstance(state.get("last_tool_result"), dict) else {}
            return {
                "response": "Awaiting user clarification.",
                "finish_reason": "awaiting_clarification",
                "suspended": True,
                "input_request_id": last.get("input_request_id"),
                "resume_state": _build_resume_state(state),
                "steps_log": _append_step(state, {"step": "finalize", "ok": True, "suspended": True}),
            }
        last_tool = state.get("last_tool_result") if isinstance(state.get("last_tool_result"), dict) else {}
        analysis_note = "Planner answered directly without running a tool."
        if state.get("tool_round"):
            tool_kind = str(last_tool.get("kind") or "").strip()
            analysis_note = f"Completed {tool_kind or 'internal'} analysis after {int(state.get('tool_round') or 0)} tool round(s)."
        elif state.get("finish_reason") == "llm_parse_error":
            analysis_note = "Planner failed to return valid JSON; finalizing conservatively."
        elif str((state.get("pending") or {}).get("action") or "").strip().lower() == "clarify" and state.get("clarify_used"):
            analysis_note = "Second clarification was refused; finalizing with current evidence."
        await _step_plan_update(ctx, 2, "completed", note=analysis_note)
        await _step_plan_update(ctx, 3, "in_progress")
        await ctx.agent._emit_stage(ctx.task.task_id, "reason_summarize", "Summarizing LangGraph reasoning.")  # noqa: SLF001
        tr = state.get("transcript") or ""
        p = state.get("pending") or {}
        reason = state.get("finish_reason")
        if int(state.get("tool_round") or 0) >= int(state.get("max_tool_rounds") or 5) and str(p.get("action") or "").lower() != "done":
            reason = reason or "max_tool_rounds"
        pre_answer = ""
        if str(p.get("action") or "").lower() == "done":
            pre_answer = str(p.get("answer") or "").strip()
        summary_system = "\n\n".join(
            [
                "You are the COSMIC tabular specialist. Summarize the reasoning transcript for the orchestrator. "
                "Be concise; cite only facts from the transcript or tool results.",
                build_internal_context("summarize", include_fpna=ctx.cfg.include_financial_fpna_prompt),
            ]
        )
        extra = ""
        if reason == "max_tool_rounds":
            extra = "\n(Stopped: max internal tool rounds reached.)\n"
        elif reason == "llm_parse_error":
            extra = "\n(Planner JSON parse failed.)\n"
        elif str(p.get("action") or "").strip().lower() == "clarify" and state.get("clarify_used"):
            extra = "\n(Planner requested a second clarification; only one is allowed per run.)\n"
            reason = reason or "clarify_repeat"
        summary_user = f"## Goal\n{state.get('goal')}\n{extra}\n## Transcript\n{tr[-28000:]}\n"
        if pre_answer:
            summary_user += f"\n## Planner final answer hint\n{pre_answer[:8000]}\n"

        rid = ctx.agent._safe(ctx.task.input.get("request_id")) if isinstance(ctx.task.input, dict) else None  # noqa: SLF001
        final_text = await invoke_tabular_mimo(
            cfg=ctx.cfg,
            http_client=ctx.http_client,
            system_content=summary_system,
            user_message=summary_user,
            task_id=ctx.task.task_id,
            session_id=ctx.task.session_id,
            request_id=rid or None,
            source=ctx.task.source,
            source_id=ctx.task.source_id,
            channel=ctx.task.channel,
            operation="tabular.internal_llm.reason_answer",
            max_output_chars=8000,
            temperature=0.2,
        )
        text = (final_text or pre_answer or "").strip() or "Tabular reasoning completed."
        await _step_plan_update(ctx, 3, "completed", note="Produced compact summary for the orchestrator.")
        return {
            "response": text,
            "finish_reason": reason or ("answered" if pre_answer else None),
            "steps_log": _append_step(state, {"step": "finalize", "ok": True}),
        }

    g = StateGraph(TabularReasonState)
    g.add_node("bootstrap", bootstrap)
    g.add_node("decide", decide)
    g.add_node("tool", tool_node)
    g.add_node("finalize", finalize)
    g.add_edge(START, "bootstrap")
    g.add_edge("bootstrap", "decide")
    g.add_conditional_edges("decide", route_after_decide, {"tool": "tool", "finalize": "finalize"})
    g.add_conditional_edges("tool", route_after_tool, {"decide": "decide", "finalize": "finalize"})
    g.add_edge("finalize", END)
    return g.compile()


async def run_tabular_reason_langgraph(
    *,
    agent: Any,
    task: TaskEnvelope,
    http_client: httpx.AsyncClient,
    cfg: TabularAgentConfig,
    bundle_id: str,
    artifact_id: str,
    goal: str,
    allow_python: bool,
) -> dict[str, Any]:
    ctx = _GraphCtx(agent=agent, cfg=cfg, http_client=http_client, task=task)
    app = _build_graph(ctx)
    max_rounds = max(1, min(20, int(getattr(cfg, "tabular_reason_max_tool_rounds", 5) or 5)))
    resume_block = task.input.get("_resume") if isinstance(task.input, dict) and isinstance(task.input.get("_resume"), dict) else {}
    resume_state = resume_block.get("resume_state") if isinstance(resume_block.get("resume_state"), dict) else {}
    resume_reply = resume_block.get("reply") if isinstance(resume_block.get("reply"), dict) else {}
    initial: TabularReasonState = {
        "goal": goal,
        "bundle_id": bundle_id,
        "artifact_id": artifact_id,
        "allow_python": allow_python,
        "max_tool_rounds": max_rounds,
        "resumed": bool(resume_block),
    }
    if resume_state:
        for key in ("transcript", "tool_round", "clarify_used", "steps_log", "bundle_root", "analysis_step_started"):
            if key in resume_state:
                initial[key] = resume_state[key]
        if isinstance(resume_state.get("max_tool_rounds"), int):
            initial["max_tool_rounds"] = int(resume_state["max_tool_rounds"])
        if "allow_python" in resume_state:
            initial["allow_python"] = bool(resume_state.get("allow_python"))
    if resume_block:
        await agent.emit_event(
            task.task_id,
            "task.resumed",
            {
                "child_task_id": str(resume_block.get("resume_of_task_id") or task.task_id),
                "resumed_task_id": task.task_id,
                "input_request_id": str(resume_block.get("input_request_id") or "").strip() or None,
                "session_id": task.session_id,
                "request_id": agent._safe(task.input.get("request_id")) if isinstance(task.input, dict) else None,  # noqa: SLF001
                "channel": task.channel,
                "source": task.source,
                "source_id": task.source_id,
                "parent_task_id": getattr(task, "parent_task_id", None),
            },
        )
    reply_text = str(resume_reply.get("content") or "").strip()
    if reply_text:
        existing = str(initial.get("transcript") or "")
        initial["transcript"] = f"{existing}\n\n--- user_clarification ---\n{reply_text}\n"
    out = await app.ainvoke(initial)
    steps = out.get("steps_log") or []
    return {
        "response": out.get("response") or "Tabular reasoning completed.",
        "bundle_id": bundle_id,
        "artifact_id": artifact_id,
        "goal": goal,
        "workflow": "langgraph",
        "finish_reason": out.get("finish_reason"),
        "last_tool_result": out.get("last_tool_result"),
        "clarify_used": bool(out.get("clarify_used")),
        "suspended": bool(out.get("suspended")),
        "input_request_id": out.get("input_request_id"),
        "resume_state": out.get("resume_state"),
        "steps": steps,
        "summary": out.get("response"),
    }
