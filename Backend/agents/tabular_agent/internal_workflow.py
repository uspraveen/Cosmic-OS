"""
Internal agentic workflow for the tabular specialist (``tabular.reason_workbook``).

Default path: **LangGraph** multi-step loop (``tabular_reason_graph``) — bounded tool rounds, internal
tools (browse / schema / preview / sql / python / **clarify**), then summarize. **clarify** calls the
orchestrator ``/internal/tasks/{parent_task_id}/request-input`` relay (``user_input:requests`` /
``user_input:replies``); on success the specialist suspends and the orchestrator later resumes it as a
second invocation via ``agent.resume``; see ``orchestrator_clarify.py`` and ``INTEROP.md``.

Fallback: **legacy** single-shot plan → execute → validate → summarize when
``TABULAR_AGENT_REASON_USE_LANGGRAPH=false`` or LangGraph is unavailable.

This is **not** a new COSMIC runtime stage. Deterministic DuckDB / Parquet / filesystem operations remain
source of truth; MiMo proposes JSON actions only.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from uuid import uuid4

import httpx

from shared.contracts import TaskEnvelope

from .config import TabularAgentConfig
from .internal_llm import invoke_tabular_mimo
from .prompt_assets import build_internal_context
from .sandbox import persist_bundle_python_script, provision_venv, run_python_script, validate_pip_packages, write_execution_receipt

logger = logging.getLogger(__name__)

_PLAN_INSTRUCTION = """\
Respond with **one JSON object only** (no markdown fences). Keys:
- "mode": "sql" or "python"
- "sql": string or null — a single read-only SELECT when mode is "sql" (DuckDB; views are named s_<sheet_id>)
- "python_code": string or null — when mode is "python", short script using duckdb and/or pandas only; working directory is the bundle root (use relative paths like bundle.duckdb or sheets/*.parquet)
- "rationale": one short sentence

If the goal can be answered with SQL, prefer mode "sql". Use "python" only when non-SQL logic is truly needed.
"""


def extract_json_object(text: str) -> dict[str, Any] | None:
    t = (text or "").strip()
    if not t:
        return None
    if "```" in t:
        inner = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", t, re.I)
        if inner:
            t = inner.group(1).strip()
    start = t.find("{")
    end = t.rfind("}")
    if start < 0 or end <= start:
        return None
    frag = t[start : end + 1]
    try:
        out = json.loads(frag)
    except json.JSONDecodeError:
        return None
    return out if isinstance(out, dict) else None


async def run_tabular_reason_workbook(
    *,
    agent: Any,
    task: TaskEnvelope,
    http_client: httpx.AsyncClient,
    cfg: TabularAgentConfig,
) -> dict[str, Any]:
    """Run tabular internal reasoning: LangGraph multi-step (default) or legacy single-shot."""
    bundle_id = agent._require(agent._safe(task.input.get("bundle_id")))  # noqa: SLF001
    artifact_id = agent._require(agent._safe(task.input.get("artifact_id")))  # noqa: SLF001
    goal = agent._require(agent._safe(task.input.get("goal")))  # noqa: SLF001
    allow_python = True
    if isinstance(task.input, dict) and "allow_python" in task.input:
        allow_python = bool(task.input.get("allow_python"))

    request_id = agent._safe(task.input.get("request_id")) if isinstance(task.input, dict) else None  # noqa: SLF001

    if not cfg.enable_internal_llm or not cfg.mimo_api_key or not cfg.mimo_base_url:
        return {
            "response": "Internal tabular reasoning requires TABULAR_AGENT_ENABLE_INTERNAL_LLM and MiMo credentials.",
            "bundle_id": bundle_id,
            "artifact_id": artifact_id,
            "error": "internal_llm_disabled",
            "error_code": "FEATURE_DISABLED",
            "steps": [],
        }

    if getattr(cfg, "tabular_reason_use_langgraph", True):
        try:
            from .tabular_reason_graph import run_tabular_reason_langgraph

            return await run_tabular_reason_langgraph(
                agent=agent,
                task=task,
                http_client=http_client,
                cfg=cfg,
                bundle_id=bundle_id,
                artifact_id=artifact_id,
                goal=goal,
                allow_python=allow_python,
            )
        except ImportError as exc:
            logger.warning("tabular.langgraph_unavailable: %s", exc)

    return await _run_tabular_reason_legacy(
        agent=agent,
        task=task,
        http_client=http_client,
        cfg=cfg,
        bundle_id=bundle_id,
        artifact_id=artifact_id,
        goal=goal,
        allow_python=allow_python,
        request_id=request_id,
    )


async def _run_tabular_reason_legacy(
    *,
    agent: Any,
    task: TaskEnvelope,
    http_client: httpx.AsyncClient,
    cfg: TabularAgentConfig,
    bundle_id: str,
    artifact_id: str,
    goal: str,
    allow_python: bool,
    request_id: str | None,
) -> dict[str, Any]:
    """Legacy single-shot: inspect → plan → execute → validate → summarize."""
    steps: list[dict[str, Any]] = []

    await agent._emit_stage(task.task_id, "reason_inspect", "Inspecting bundle context for internal reasoning.")  # noqa: SLF001

    root = agent._bundle_disk_path(bundle_id, artifact_id)  # noqa: SLF001
    preview_path = root / "preview.md"
    cat_path = root / "sheet_catalog.json"
    preview_excerpt = preview_path.read_text(encoding="utf-8")[:6000] if preview_path.is_file() else ""
    catalog_excerpt = cat_path.read_text(encoding="utf-8")[:9000] if cat_path.is_file() else "{}"

    inspect_context = (
        f"## User goal\n{goal}\n\n"
        f"## sheet_catalog.json (truncated)\n```\n{catalog_excerpt}\n```\n\n"
        f"## preview.md (truncated)\n```\n{preview_excerpt}\n```\n"
    )

    steps.append({"step": "inspect", "ok": True, "bundle_root": str(root)})

    await agent._emit_stage(task.task_id, "reason_plan", "Planning deterministic query or sandbox step.")  # noqa: SLF001

    plan_system = "\n\n".join(
        [
            "You are the internal planner for the COSMIC tabular specialist. "
            "Propose one execution step. Never invent sheet_ids — use names from sheet_catalog.",
            _PLAN_INSTRUCTION,
            build_internal_context("plan", include_fpna=cfg.include_financial_fpna_prompt),
        ]
    )

    plan_raw = await invoke_tabular_mimo(
        cfg=cfg,
        http_client=http_client,
        system_content=plan_system,
        user_message=inspect_context,
        task_id=task.task_id,
        session_id=task.session_id,
        request_id=request_id or None,
        source=task.source,
        source_id=task.source_id,
        channel=task.channel,
        operation="tabular.internal_llm.reason_plan",
        max_output_chars=12_000,
        temperature=0.1,
    )
    plan = extract_json_object(plan_raw or "") if plan_raw else None
    if not plan:
        return {
            "response": "Tabular specialist could not parse an execution plan from the internal model.",
            "bundle_id": bundle_id,
            "artifact_id": artifact_id,
            "goal": goal,
            "raw_plan": plan_raw,
            "steps": steps + [{"step": "plan", "ok": False}],
        }

    mode = str(plan.get("mode") or "").strip().lower()
    sql = plan.get("sql")
    py_code = plan.get("python_code")
    rationale = str(plan.get("rationale") or "").strip()

    steps.append({"step": "plan", "ok": True, "mode": mode, "rationale": rationale})

    execution_payload: dict[str, Any] = {}

    await agent._emit_stage(task.task_id, "reason_execute", "Executing planned deterministic or sandbox step.")  # noqa: SLF001

    if mode == "sql":
        if not isinstance(sql, str) or not sql.strip():
            steps.append({"step": "execute", "ok": False, "error": "missing_sql"})
            execution_payload = {"error": "Plan mode sql but sql missing."}
        else:
            try:
                execution_payload = agent.sync_run_select(bundle_id, artifact_id, sql.strip())  # noqa: SLF001
                steps.append({"step": "execute", "ok": True, "kind": "sql"})
            except Exception as exc:  # noqa: BLE001
                logger.warning("tabular.reason.sql_failed: %s", exc)
                execution_payload = {"error": str(exc)[:500]}
                steps.append({"step": "execute", "ok": False, "kind": "sql", "error": str(exc)[:300]})
    elif mode == "python":
        if not allow_python:
            execution_payload = {"error": "python execution disabled for this task (allow_python=false)."}
            steps.append({"step": "execute", "ok": False, "kind": "python", "error": "disabled"})
        elif not isinstance(py_code, str) or not py_code.strip():
            execution_payload = {"error": "Plan mode python but python_code missing."}
            steps.append({"step": "execute", "ok": False, "kind": "python"})
        else:
            execution_id = f"exec_{uuid4().hex[:14]}"
            network_enabled = bool(getattr(cfg, "sandbox_allow_network", False))
            pip_enabled = bool(getattr(cfg, "sandbox_allow_pip", False))
            raw_pip_pkgs = plan.get("pip_install") if isinstance(plan.get("pip_install"), list) else []
            requested_packages = [str(p).strip() for p in raw_pip_pkgs if str(p).strip()]
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
                receipt_path = write_execution_receipt(
                    bundle_root=root,
                    execution_id=execution_id,
                    task_id=task.task_id,
                    session_id=task.session_id,
                    artifact_id=artifact_id,
                    receipt={
                        "kind": "tabular_sandbox",
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
                execution_payload = {
                    "execution_id": execution_id,
                    "exit_code": run_out.get("exit_code"),
                    "stdout": (run_out.get("stdout") or "")[:8000],
                    "stderr": (run_out.get("stderr") or "")[:4000],
                    "receipt_path": str(receipt_path.relative_to(root)).replace("\\", "/"),
                    "network_enabled": network_enabled,
                    "packages_installed": installed_packages,
                }
                ok = int(run_out.get("exit_code") or -1) == 0
                steps.append({"step": "execute", "ok": ok, "kind": "python", "execution_id": execution_id})
            except ValueError as exc:
                execution_payload = {"error": str(exc)}
                steps.append({"step": "execute", "ok": False, "kind": "python", "error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                logger.exception("tabular.reason.python_failed")
                execution_payload = {"error": str(exc)[:500]}
                steps.append({"step": "execute", "ok": False, "kind": "python", "error": str(exc)[:300]})
    else:
        execution_payload = {"error": f"Unsupported mode: {mode}"}
        steps.append({"step": "execute", "ok": False, "error": "bad_mode"})

    await agent._emit_stage(task.task_id, "reason_validate", "Validating execution output.")  # noqa: SLF001

    validation_notes: list[str] = []
    if isinstance(execution_payload.get("row_count"), int):
        if execution_payload.get("truncated"):
            validation_notes.append("SQL result truncated to configured max rows.")
    if execution_payload.get("exit_code") not in (None, 0):
        validation_notes.append(f"Non-zero exit_code: {execution_payload.get('exit_code')}")

    steps.append({"step": "validate", "ok": not execution_payload.get("error"), "notes": validation_notes})

    await agent._emit_stage(task.task_id, "reason_summarize", "Summarizing results for the orchestrator.")  # noqa: SLF001

    summary_system = "\n\n".join(
        [
            "You are the COSMIC tabular specialist. Summarize the execution result for the orchestrator. "
            "Be concise. Cite only facts present in the goal, catalog excerpt, or execution payload.",
            build_internal_context("summarize", include_fpna=cfg.include_financial_fpna_prompt),
        ]
    )
    summary_user = (
        f"## Goal\n{goal}\n\n"
        f"## Plan\n{json.dumps(plan, ensure_ascii=False)[:6000]}\n\n"
        f"## Execution\n{json.dumps(execution_payload, ensure_ascii=False)[:10000]}\n"
    )
    final_text = await invoke_tabular_mimo(
        cfg=cfg,
        http_client=http_client,
        system_content=summary_system,
        user_message=summary_user,
        task_id=task.task_id,
        session_id=task.session_id,
        request_id=request_id or None,
        source=task.source,
        source_id=task.source_id,
        channel=task.channel,
        operation="tabular.internal_llm.reason_answer",
        max_output_chars=8000,
        temperature=0.2,
    )

    steps.append({"step": "summarize", "ok": bool(final_text)})

    return {
        "response": final_text or "Tabular reasoning completed without a textual summary.",
        "bundle_id": bundle_id,
        "artifact_id": artifact_id,
        "goal": goal,
        "plan": plan,
        "execution": execution_payload,
        "validation_notes": validation_notes,
        "summary": final_text,
        "steps": steps,
    }
