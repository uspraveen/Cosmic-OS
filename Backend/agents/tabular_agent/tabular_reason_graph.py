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

from .config import AGENT_ROOT, TabularAgentConfig
from .internal_llm import invoke_tabular_mimo
from .internal_workflow import extract_json_object
from .orchestrator_clarify import request_orchestrator_task_input
from .prompt_assets import build_internal_context
from .sandbox import (
    persist_bundle_python_script,
    provision_venv,
    run_python_script,
    validate_pip_packages,
    write_execution_receipt,
)

logger = logging.getLogger(__name__)

_VALID_ACTIONS = frozenset(
    {
        "browse",
        "schema",
        "preview",
        "sql",
        "python",
        "clarify",
        "done",
        "activate_skill",
        "create_plan",
    }
)

_MULTI_STEP_INSTRUCTION = """\
You control internal tools over an already-parsed spreadsheet bundle. Reply with **one JSON object only** (no markdown).

Keys:
- "action": one of "browse", "schema", "preview", "sql", "python", "clarify", "activate_skill", "create_plan", "done"
- "sheet_id": string or null — required for "preview"; optional filter for "schema"
- "sql": string or null — single read-only SELECT for "sql" (DuckDB views: s_<sheet_id>)
- "python_code": string or null — for "python" only; duckdb/pandas; cwd is bundle root
- "pip_install": array of strings or null — for "python" only; packages to install before running (e.g. ["openpyxl", "matplotlib"]); omit if not needed
- "question": string or null — for "clarify" only: concise user-facing question (blocking ambiguity)
- "options": array of strings or null — for "clarify" only: short choices when applicable (max ~8)
- "ambiguity": string or null — for "clarify" only: internal label (e.g. "multiple_sheets", "metric_definition")
- "skill_name": string or null — for "activate_skill" only: name of skill to activate (e.g. "three-statement", "ratio-analysis", "revenue-analytics")
- "steps": array of strings — for "create_plan" only: ordered list of concrete step descriptions (3-8 steps)
- "plan_step": integer or null — optional on any action: which plan step (1-based) the current action is working on
- "answer": string or null — when action is "done", concise result for the orchestrator
- "rationale": short string

## Execution Planning

Use **"create_plan"** as your FIRST action when the user's goal requires 3 or more logical steps.
Plan when:
- The goal involves multi-sheet or multi-metric analysis (DSO + DPO + DIO → CCC)
- Financial analysis requiring skill activation then multiple queries (variance, PVM, consolidation)
- Any goal that needs explore → compute → cross-check → summarize

Skip planning when:
- Single lookup or one SQL query answers the goal
- Simple schema/preview inspection

Planning rules:
1. "create_plan" must be your FIRST action — plan before doing any work
2. Steps must be concrete and completable: "Query revenue by segment from s_pnl" not "Analyze data"
3. Always include a verification step near the end: "Cross-check totals / validate results sum correctly"
4. Always end with a summary step: "Synthesize findings into final answer"
5. After creating a plan, you will re-decide to start step 1
6. Include "plan_step": N on subsequent actions to track which step you are executing
7. You may work on multiple actions within one plan step — move to the next step when the logical unit is done

## Tool Rules

- Prefer "sql" when the goal needs tabular aggregation/filtering.
- Use "schema" / "preview" to disambiguate columns or sheet choice.
- Use "browse" for a lightweight workbook list/handles reminder.
- Use **"activate_skill"** when user's goal relates to financial analysis patterns: variance, ratios, margins, revenue, working capital, etc.
  - Check the Available Skills list in context for matching triggers ("variance" → financial-variance, "MRR" → revenue-analytics, etc.)
  - Activating a skill loads domain-specific formulas and SQL patterns
  - After activation, re-decide with the new context to choose sql/python/done
- Use "python" when need Python logic (Regressions, complex Pandas, visualization data prep)
- Use "python" for **charts and visualizations** — you have full matplotlib/seaborn access via pip_install:
  - Line charts for trends (revenue over time, margin trajectory)
  - Bar charts for comparisons (segment revenue, budget vs actual)
  - Waterfall charts for bridges (margin bridge, MRR movement)
  - Stacked bars for composition (expense breakdown, revenue mix)
  - Save output to `exports/chart_<descriptive_name>.png` via `plt.savefig()`
  - Always include: title, axis labels, data labels on key points, and a clean layout (`plt.tight_layout()`)
  - When the user asks to "show", "visualize", "plot", or "chart" something, generate a chart — don't just return numbers
- Use **"clarify" at most once** when ambiguity blocks correct execution and cannot be resolved with internal tools
  (e.g. multiple plausible sheets, unclear metric, fiscal calendar, unit/currency). Requires an orchestrator parent task.

## Provenance & Audit Trail

Every number you report must be traceable. In your final answer:
- **Cite the source**: which sheet and column each input came from (e.g. "Revenue from s_pnl.total_revenue, rows for FY2025")
- **Show the formula chain**: list the computation steps, not just the result
  - Bad: "Gross margin is 72%"
  - Good: "Revenue = $1,234,567 (SUM of s_pnl.revenue WHERE fiscal_year=2025). COGS = $345,678 (SUM of s_pnl.cogs, same filter). Gross Profit = $888,889. Gross Margin = 888,889 / 1,234,567 = 72.0%"
- **State the SQL** that produced each key number — the user or an auditor should be able to re-run your query and get the same result
- **Note the record count**: "Based on 12 monthly records" or "Across 4 segments" — this helps catch missing data
- For multi-step analyses (ratios, CCC, PVM), show each intermediate value and its source before the final result
- If you used a skill's formula (e.g. DSO = AR/Revenue × Days), state which formula you applied and with what inputs

## Verification & Completion

Before setting action to "done", run a **final verification pass**. This is mandatory — never skip it.

### 1. Data Quality (run BEFORE computing anything)
- Check for NULLs in key columns: `SELECT COUNT(*) FILTER (WHERE revenue IS NULL) FROM s_pnl`
- Check for duplicates that would inflate sums: `SELECT period, COUNT(*) FROM s_pnl GROUP BY period HAVING COUNT(*) > 1`
- Spot-check row counts — does the number of rows match the expected periods/entities/products?
- Look for unexpected blanks, zeros where there should be values, or negative values in unsigned fields

### 2. Sheet Structure & Column Alignment
- Verify you are reading the correct columns — preview the sheet first if column names are ambiguous
- Watch for off-by-one header issues (data starting on row 2 vs row 1, merged header rows)
- If multiple sheets exist for the same entity, confirm you are using the right one (e.g. "PnL" vs "PnL_Draft")
- Cross-check column names against the schema — don't assume "Amount" means revenue; it could be cost

### 3. Arithmetic & Formula Integrity
- Cross-check computed totals: Revenue - COGS should equal Gross Profit; Assets should equal Liabilities + Equity
- For multi-step calculations, verify intermediate results before building on them
- If a formula was used (DSO, NRR, Z-Score, PVM, etc.), verify the denominator is non-zero and the result is in a sensible range
- Watch for sign conventions: are expenses positive or negative? Are credits stored as negative? Mixing conventions silently corrupts sums

### 4. Cross-Sheet & Cross-Period Consistency
- If data comes from multiple sheets, verify they align on the same periods, entities, and currency
- Check that totals in summary sheets match detail sheets (e.g. segment totals should sum to consolidated)
- For YoY or QoQ comparisons, verify both periods have complete data — partial periods produce misleading deltas

### 5. Reasonableness & Ranges
- Validate that key metrics fall within plausible ranges (e.g. DSO 15-120 days, gross margin 0-100%, NRR 70-200%)
- Flag outliers — a sudden 10x revenue jump or negative headcount likely indicates a data issue, not a real change
- For currency amounts, check the magnitude — a $50B revenue for a mid-market company means the column is probably in thousands or the entity is wrong

### 6. Sanity-Check SQL
- If any number looks off, run a quick targeted SQL to isolate the issue before reporting it as a finding
- When aggregating, always verify: `SELECT SUM(amount) FROM ... WHERE ...` matches what you used in the larger query

Set action to "done" only when the goal is fully satisfied AND the verification pass confirms results are trustworthy, or you cannot proceed.
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
    # Skills system fields (progressive disclosure)
    available_skills: list[dict[str, Any]]
    active_skill_content: str
    # Dynamic step plan fields (LLM-driven planning per §32.2)
    plan_active: bool
    plan_total_steps: int


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
        "plan_active": bool(state.get("plan_active")),
        "plan_total_steps": int(state.get("plan_total_steps") or 0),
    }


def _append_step(
    state: TabularReasonState, entry: dict[str, Any]
) -> list[dict[str, Any]]:
    log = list(state.get("steps_log") or [])
    log.append(entry)
    return log


async def _step_plan_update(
    ctx: _GraphCtx, step: int, status: str, note: str | None = None
) -> None:
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
            "request_id": (
                agent._safe(task.input.get("request_id"))
                if isinstance(task.input, dict)
                else None
            ),  # noqa: SLF001
            "channel": task.channel,
            "source": task.source,
            "source_id": task.source_id,
        }
        if state.get("clarify_used"):
            e: dict[str, Any] = {
                "error": "clarify_already_used",
                "kind": "clarify",
                "clarify_status": "clarify_already_used",
            }
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
            e = {
                "error": "clarify_requires_question",
                "kind": "clarify",
                "clarify_status": "missing_question",
            }
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
                specialist_agent_id=str(
                    getattr(agent, "agent_id", "") or "cosmic/tabular-agent:1.0.0"
                ),
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
            e = {
                "error": "missing_input_request_id",
                "kind": "clarify",
                "clarify_status": "relay_error",
            }
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
        slim = [
            {
                "artifact_id": w.get("artifact_id"),
                "parse_status": w.get("parse_status"),
                "handles": w.get("handles"),
            }
            for w in wbs
            if isinstance(w, dict)
        ][:12]
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
                sheets = [
                    s
                    for s in sheets
                    if isinstance(s, dict) and str(s.get("sheet_id")) == vs
                ]
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
            return {
                "error": "python action requires python_code"
            }, "missing python_code"
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
                    cache_root_raw = str(
                        getattr(cfg, "sandbox_venv_cache_root", "") or ""
                    ).strip()
                    cache_root = Path(cache_root_raw) if cache_root_raw else None
                    python_exe, installed_packages, pip_log = provision_venv(
                        packages=clean_pkgs,
                        cache_root=cache_root,
                        pip_timeout_sec=float(
                            getattr(cfg, "sandbox_pip_timeout_sec", 120.0) or 120.0
                        ),
                    )
            elif requested_packages and not pip_enabled:
                pip_log = {
                    "skipped": True,
                    "reason": "sandbox_allow_pip=false",
                    "packages_requested": requested_packages,
                }
            run_out = run_python_script(
                script_path=script_path,
                cwd=root,
                timeout_sec=cfg.sandbox_timeout_sec,
                bundle_root=root,
                python_executable=python_exe,
            )
            parent_tid = (
                str(getattr(task, "parent_task_id", None) or "").strip() or None
            )
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
                    "script_relative": str(script_path.relative_to(root)).replace(
                        "\\", "/"
                    ),
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
            return e, json.dumps({"error": str(exc)[:500]}, ensure_ascii=False)

    if action == "create_plan":
        raw_steps = pending.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            return {
                "error": "create_plan requires a non-empty 'steps' array"
            }, "create_plan requires a non-empty 'steps' array"
        steps = [str(s).strip() for s in raw_steps if str(s).strip()][:8]
        if not steps:
            return {"error": "create_plan: all steps were empty"}, "empty steps"
        step_plan = getattr(ctx.agent, "step_plan", None)
        if step_plan is None:
            # StepPlan not injected (e.g. testing) — return plan as observation only
            out: dict[str, Any] = {
                "kind": "create_plan",
                "steps": steps,
                "total_steps": len(steps),
                "plan_active": True,
            }
            return out, json.dumps(out, ensure_ascii=False)
        try:
            plan_result = await step_plan.create(steps)
            await step_plan.update(1, "in_progress")
            out = {
                "kind": "create_plan",
                "steps": steps,
                "total_steps": plan_result.get("total_steps", len(steps)),
                "plan_active": True,
            }
            return out, json.dumps(out, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("tabular.graph.create_plan_failed: %s", exc)
            return {"error": str(exc)[:500]}, str(exc)

    if action == "activate_skill":
        skill_name = pending.get("skill_name")
        if not skill_name:
            return {
                "error": "activate_skill requires skill_name"
            }, "activate_skill requires skill_name"
        available_skills = state.get("available_skills") or []
        # Find matching skill
        matched = None
        for s in available_skills:
            if s.get("name") == skill_name:
                matched = s
                break
        if not matched:
            return {
                "error": f"unknown skill: {skill_name}"
            }, f"unknown skill: {skill_name}"
        try:
            from .skills import load_skill_content

            content = load_skill_content(matched.get("path", ""))
            if content is None:
                return {
                    "error": f"failed to load skill: {skill_name}"
                }, f"failed to load skill: {skill_name}"
            out = {
                "kind": "activate_skill",
                "skill_name": skill_name,
                "content": content[:16000],
            }
            return out, json.dumps(out, ensure_ascii=False)[:18000]
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)[:500]}, str(exc)

    return {"error": f"unknown action: {action}"}, json.dumps({"error": action})


def _build_graph(ctx: _GraphCtx) -> Any:
    async def bootstrap(state: TabularReasonState) -> dict[str, Any]:
        await ctx.agent._emit_stage(
            ctx.task.task_id, "reason_inspect", "Loading bundle context (LangGraph)."
        )  # noqa: SLF001
        root = ctx.agent._bundle_disk_path(state["bundle_id"], state["artifact_id"])  # noqa: SLF001
        preview_path = root / "preview.md"
        cat_path = root / "sheet_catalog.json"
        preview_excerpt = (
            preview_path.read_text(encoding="utf-8")[:6000]
            if preview_path.is_file()
            else ""
        )
        catalog_excerpt = (
            cat_path.read_text(encoding="utf-8")[:9000] if cat_path.is_file() else "{}"
        )
        initial = (
            f"## User goal\n{state['goal']}\n\n"
            f"## sheet_catalog.json (truncated)\n```\n{catalog_excerpt}\n```\n\n"
            f"## preview.md (truncated)\n```\n{preview_excerpt}\n```\n"
        )
        existing_transcript = str(state.get("transcript") or "").strip()
        transcript = (
            f"{existing_transcript}\n\n{initial}" if existing_transcript else initial
        )
        skills_dir = Path(
            getattr(ctx.cfg, "skills_dir", "") or str(AGENT_ROOT / "skills")
        )
        available_skills: list[dict[str, Any]] = []
        if getattr(ctx.cfg, "skills_enabled", True) and skills_dir.is_dir():
            try:
                from .skills import discover_skills

                discovered = discover_skills(skills_dir)
                available_skills = [
                    {
                        "name": s["name"],
                        "description": s["description"],
                        "tags": s.get("tags", []),
                        "path": s["path"],
                    }
                    for s in discovered
                ]
                logger.debug(
                    "tabular.graph.skills_discovered: %d", len(available_skills)
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("tabular.graph.skills_discover_failed: %s", e)

        return {
            "transcript": transcript,
            "bundle_root": str(root.resolve()),
            "tool_round": int(state.get("tool_round") or 0),
            "analysis_step_started": bool(state.get("analysis_step_started")),
            "available_skills": available_skills,
            "steps_log": _append_step(state, {"step": "bootstrap", "ok": True}),
        }

    async def decide(state: TabularReasonState) -> dict[str, Any]:
        await ctx.agent._emit_stage(
            ctx.task.task_id, "reason_plan", "Planning next internal tool step."
        )  # noqa: SLF001
        tr = state.get("transcript") or ""
        rounds = int(state.get("tool_round") or 0)
        max_r = int(state.get("max_tool_rounds") or 5)
        user_msg = (
            f"{_MULTI_STEP_INSTRUCTION}\n\n"
            f"tool_round so far: {rounds} (max tool executions: {max_r}).\n\n"
            f"## Transcript\n{tr[-24000:]}\n"
        )
        # Build system prompt with skills context
        system_parts = [
            "You are the COSMIC tabular specialist internal planner.",
            build_internal_context(
                "plan", include_fpna=ctx.cfg.include_financial_fpna_prompt
            ),
        ]
        # Add skills context if available
        skills_list = state.get("available_skills") or []
        if skills_list and getattr(ctx.cfg, "skills_enabled", True):
            try:
                from .prompt_assets import build_skills_context

                skills_ctx = build_skills_context(skills_list)
                if skills_ctx:
                    system_parts.append(skills_ctx)
            except Exception as e:  # noqa: BLE001
                logger.debug("tabular.graph.skills_context_failed: %s", e)
        system = "\n\n".join(system_parts)
        rid = (
            ctx.agent._safe(ctx.task.input.get("request_id"))
            if isinstance(ctx.task.input, dict)
            else None
        )  # noqa: SLF001
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
                "pending": {
                    "action": "done",
                    "answer": "Planner did not return valid JSON.",
                    "rationale": "parse_error",
                },
                "finish_reason": "llm_parse_error",
                "analysis_step_started": True,
                "steps_log": _append_step(
                    state, {"step": "decide", "ok": False, "raw": (raw or "")[:500]}
                ),
            }
        return {
            "pending": parsed,
            "analysis_step_started": True,
            "steps_log": _append_step(
                state, {"step": "decide", "ok": True, "action": parsed.get("action")}
            ),
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
        await ctx.agent._emit_stage(
            ctx.task.task_id, "reason_execute", "Running internal tabular tool."
        )  # noqa: SLF001
        pending = state.get("pending") or {}
        current_action = str(pending.get("action") or "").strip().lower()
        if current_action == "clarify":
            await ctx.agent._emit_stage(
                ctx.task.task_id,
                "reason_clarify",
                "Requesting user input via orchestrator task-input relay.",
            )  # noqa: SLF001

        # --- plan_step: mark in_progress BEFORE executing the tool ---
        plan_step_num = pending.get("plan_step")
        if isinstance(plan_step_num, int) and plan_step_num >= 1 and state.get("plan_active"):
            await _step_plan_update(
                ctx, plan_step_num, "in_progress",
                note=f"Executing {current_action}",
            )

        result, obs = await _run_tool(ctx=ctx, state=state, pending=pending)
        tool_ok = "error" not in result

        # --- Branch: create_plan (lightweight, no tool_round) ---
        if current_action == "create_plan":
            plan_steps = result.get("steps") or []
            plan_total = result.get("total_steps") or len(plan_steps)
            tr = (
                (state.get("transcript") or "")
                + f"\n\n--- plan created ({plan_total} steps) ---\n"
                + "\n".join(f"  {i+1}. {s}" for i, s in enumerate(plan_steps))
                + "\n"
            )
            out: dict[str, Any] = {
                "transcript": tr,
                "last_tool_result": result,
                "plan_active": tool_ok,
                "plan_total_steps": plan_total if tool_ok else 0,
                "steps_log": _append_step(
                    state,
                    {
                        "step": "tool",
                        "ok": tool_ok,
                        "kind": "create_plan",
                        "total_steps": plan_total,
                    },
                ),
            }
            return out

        # --- Branch: activate_skill (lightweight, no tool_round) ---
        if current_action == "activate_skill":
            skill_content = ""
            if isinstance(result, dict):
                skill_content = result.get("content") or ""
            tr = (
                (state.get("transcript") or "")
                + f"\n\n--- skill activated: {pending.get('skill_name', 'unknown')} ---\n{skill_content}\n"
            )
            out = {
                "transcript": tr,
                "last_tool_result": result,
                "active_skill_content": skill_content,
                "steps_log": _append_step(
                    state,
                    {
                        "step": "tool",
                        "ok": tool_ok,
                        "kind": "activate_skill",
                        "skill_name": pending.get("skill_name"),
                    },
                ),
            }
            # plan_step tracking for skill activation
            if isinstance(plan_step_num, int) and plan_step_num >= 1 and state.get("plan_active") and tool_ok:
                await _step_plan_update(
                    ctx, plan_step_num, "completed",
                    note=f"Activated skill: {pending.get('skill_name', 'unknown')}",
                )
            return out

        # --- Branch: regular tool action (consumes a tool_round) ---
        tr = (
            state.get("transcript") or ""
        ) + f"\n\n--- tool_round {state.get('tool_round', 0) + 1} ---\n{obs}\n"
        out = {
            "transcript": tr,
            "tool_round": int(state.get("tool_round") or 0) + 1,
            "last_tool_result": result,
            "steps_log": _append_step(
                state,
                {
                    "step": "tool",
                    "ok": tool_ok,
                    "kind": pending.get("action"),
                },
            ),
        }

        # plan_step tracking for regular tools
        if isinstance(plan_step_num, int) and plan_step_num >= 1 and state.get("plan_active") and tool_ok:
            await _step_plan_update(
                ctx, plan_step_num, "completed",
                note=str(pending.get("rationale") or "")[:200] or None,
            )

        if current_action == "clarify":
            out["clarify_used"] = True
            if str(result.get("clarify_status") or "").strip().lower() == "suspended":
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
            last = (
                state.get("last_tool_result")
                if isinstance(state.get("last_tool_result"), dict)
                else {}
            )
            return {
                "response": "Awaiting user clarification.",
                "finish_reason": "awaiting_clarification",
                "suspended": True,
                "input_request_id": last.get("input_request_id"),
                "resume_state": _build_resume_state(state),
                "steps_log": _append_step(
                    state, {"step": "finalize", "ok": True, "suspended": True}
                ),
            }
        # --- Auto-complete remaining plan steps to prevent PLAN_INCOMPLETE ---
        if state.get("plan_active"):
            plan_total = int(state.get("plan_total_steps") or 0)
            step_plan = getattr(ctx.agent, "step_plan", None)
            if step_plan and plan_total > 0:
                # Mark the last step as in_progress (summarization step)
                await _step_plan_update(
                    ctx, plan_total, "in_progress", note="Summarizing findings.",
                )
                # Complete all earlier steps that weren't explicitly completed
                for i in range(1, plan_total):
                    await _step_plan_update(ctx, i, "completed")

        await ctx.agent._emit_stage(
            ctx.task.task_id, "reason_summarize", "Summarizing LangGraph reasoning."
        )  # noqa: SLF001
        tr = state.get("transcript") or ""
        p = state.get("pending") or {}
        reason = state.get("finish_reason")
        if (
            int(state.get("tool_round") or 0) >= int(state.get("max_tool_rounds") or 5)
            and str(p.get("action") or "").lower() != "done"
        ):
            reason = reason or "max_tool_rounds"
        pre_answer = ""
        if str(p.get("action") or "").lower() == "done":
            pre_answer = str(p.get("answer") or "").strip()
        summary_system = "\n\n".join(
            [
                "You are the COSMIC tabular specialist. Summarize the reasoning transcript for the orchestrator. "
                "Be concise; cite only facts from the transcript or tool results.",
                build_internal_context(
                    "summarize", include_fpna=ctx.cfg.include_financial_fpna_prompt
                ),
            ]
        )
        extra = ""
        if reason == "max_tool_rounds":
            extra = "\n(Stopped: max internal tool rounds reached.)\n"
        elif reason == "llm_parse_error":
            extra = "\n(Planner JSON parse failed.)\n"
        elif str(p.get("action") or "").strip().lower() == "clarify" and state.get(
            "clarify_used"
        ):
            extra = "\n(Planner requested a second clarification; only one is allowed per run.)\n"
            reason = reason or "clarify_repeat"
        summary_user = (
            f"## Goal\n{state.get('goal')}\n{extra}\n## Transcript\n{tr[-28000:]}\n"
        )
        if pre_answer:
            summary_user += f"\n## Planner final answer hint\n{pre_answer[:8000]}\n"

        rid = (
            ctx.agent._safe(ctx.task.input.get("request_id"))
            if isinstance(ctx.task.input, dict)
            else None
        )  # noqa: SLF001
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
        text = (
            final_text or pre_answer or ""
        ).strip() or "Tabular reasoning completed."
        # Complete the final plan step (summary)
        if state.get("plan_active"):
            plan_total = int(state.get("plan_total_steps") or 0)
            if plan_total > 0:
                await _step_plan_update(
                    ctx, plan_total, "completed",
                    note="Produced compact summary for the orchestrator.",
                )
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
    g.add_conditional_edges(
        "decide", route_after_decide, {"tool": "tool", "finalize": "finalize"}
    )
    g.add_conditional_edges(
        "tool", route_after_tool, {"decide": "decide", "finalize": "finalize"}
    )
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
    max_rounds = max(
        1, min(20, int(getattr(cfg, "tabular_reason_max_tool_rounds", 5) or 5))
    )
    resume_block = (
        task.input.get("_resume")
        if isinstance(task.input, dict) and isinstance(task.input.get("_resume"), dict)
        else {}
    )
    resume_state = (
        resume_block.get("resume_state")
        if isinstance(resume_block.get("resume_state"), dict)
        else {}
    )
    resume_reply = (
        resume_block.get("reply") if isinstance(resume_block.get("reply"), dict) else {}
    )
    initial: TabularReasonState = {
        "goal": goal,
        "bundle_id": bundle_id,
        "artifact_id": artifact_id,
        "allow_python": allow_python,
        "max_tool_rounds": max_rounds,
        "resumed": bool(resume_block),
    }
    if resume_state:
        for key in (
            "transcript",
            "tool_round",
            "clarify_used",
            "steps_log",
            "bundle_root",
            "analysis_step_started",
            "plan_active",
            "plan_total_steps",
        ):
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
                "child_task_id": str(
                    resume_block.get("resume_of_task_id") or task.task_id
                ),
                "resumed_task_id": task.task_id,
                "input_request_id": str(
                    resume_block.get("input_request_id") or ""
                ).strip()
                or None,
                "session_id": task.session_id,
                "request_id": agent._safe(task.input.get("request_id"))
                if isinstance(task.input, dict)
                else None,  # noqa: SLF001
                "channel": task.channel,
                "source": task.source,
                "source_id": task.source_id,
                "parent_task_id": getattr(task, "parent_task_id", None),
            },
        )
    reply_text = str(resume_reply.get("content") or "").strip()
    if reply_text:
        existing = str(initial.get("transcript") or "")
        initial["transcript"] = (
            f"{existing}\n\n--- user_clarification ---\n{reply_text}\n"
        )
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
