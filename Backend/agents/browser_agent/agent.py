"""COSMIC Browser Agent — specialist wrapper around cosmic-browser-use.

Triggered only by the orchestrator via the `browser.run` intent (Redis task
stream), exactly like the other specialist agents. Each task runs one
vision-based browser session to completion and returns a structured result.

Vault credentials: the orchestrator resolves credential refs at dispatch time
and injects them into the task envelope's auth field; they never exist in any
model context. They are passed to the browser run's in-memory credential
store, where only the deterministic CredentialFill action consumes them.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

from shared.agent_runtime import AgentResult, AgentRuntime, TaskEnvelope
from shared.contracts import AgentError

from .config import AGENT_ROOT, BrowserAgentConfig

logger = logging.getLogger(__name__)

BROWSER_AGENT_ID = "cosmic/browser-agent:1.0.0"


class BrowserAgent(AgentRuntime):
    """Browser specialist: runs the cosmic-browser-use agent for one goal."""

    def __init__(self, redis_client, config: BrowserAgentConfig | None = None):
        self.config = config or BrowserAgentConfig.from_env()
        # Make the cosmic-browser-use checkout importable before any browser
        # module import happens inside execute().
        self.browser_use_home = self.config.ensure_import_path()

        super().__init__(
            agent_card_path=AGENT_ROOT / "agent_card.yaml",
            redis_client=redis_client,
            gateway_url=self.config.gateway_url,
            gateway_internal_token=self.config.gateway_internal_token,
        )
        self.artifacts_root = self.config.artifacts_root.resolve()
        self._usage_post_tasks: set = set()

    async def execute(self, task: TaskEnvelope) -> AgentResult | TaskInProgress:
        handler = getattr(self, f"handle_{task.intent.replace('.', '_')}", None)
        if handler is None:
            return self._failed(
                "INVALID_INPUT",
                f"Unknown intent: {task.intent}",
                next_action="escalate",
            )
        try:
            return await handler(task)
        except Exception as exc:
            logger.exception("browser_agent.task_failed task_id=%s intent=%s", task.task_id, task.intent)
            return self._failed(
                "INTERNAL_ERROR",
                str(exc).strip()[:500] or "Browser Agent execution failed.",
                retryable=False,
                next_action="escalate",
            )

    async def handle_browser_run(self, task: TaskEnvelope) -> AgentResult:
        goal = str(task.input.get("goal") or "").strip()
        if not goal:
            return self._failed("INVALID_INPUT", "browser.run requires a goal.")

        initial_url = str(task.input.get("initial_url") or "").strip() or None
        memory_mode = str(task.input.get("memory_mode") or "off").strip().lower()
        if memory_mode not in {"off", "learn", "recall", "auto"}:
            memory_mode = "off"
        max_steps = self._safe_int(task.input.get("max_steps"), self.config.default_max_steps)
        headless = self.config.headless
        if isinstance(task.input.get("headless"), bool):
            headless = task.input["headless"]

        # Input artifacts (files the orchestrator attached via artifact_ids /
        # input_artifacts): text-like artifacts are read (bounded) and injected
        # into the run context so the browser agent can use their content —
        # e.g. a CSV of URLs to visit or account notes to act on.
        goal = self._goal_with_artifact_context(goal, task)

        credentials = self._credentials_from_auth()

        await self._emit_progress(task.task_id, f"Starting browser run: {goal[:120]}")

        # Import late — pulls playwright + the whole cosmic-browser-use stack.
        from cosmic_browser_use.api import BrowserRunError, run_goal

        try:
            result = await run_goal(
                goal,
                initial_url=initial_url,
                max_steps=max_steps,
                memory_mode=memory_mode,
                headless=headless,
                credentials=credentials,
                on_progress=lambda info: self._emit_progress(
                    task.task_id,
                    f"Step {info.get('step')}: {info.get('description') or info.get('action_type')}",
                    step=info.get("step"),
                    action_type=info.get("action_type"),
                    estimated_completion=info.get("estimated_completion"),
                ),
                working_dir_root=str(self.config.working_dir_root),
                run_timeout_sec=min(self.config.run_timeout_sec, self.max_task_duration_sec),
            )
        except BrowserRunError as exc:
            return self._failed("BROWSER_RUN_FAILED", str(exc), retryable=False, next_action="escalate")
        except Exception as exc:
            logger.exception("browser_agent.run_failed task_id=%s", task.task_id)
            return self._failed("INTERNAL_ERROR", f"Browser run failed: {exc}", retryable=False, next_action="escalate")

        self._post_run_usage(task, result)

        status = str(result.get("status") or "incomplete")
        needs_credentials = result.get("needs_credentials")
        output: dict[str, Any] = {
            "goal": goal,
            "status": status,
            "answer": str(result.get("answer") or ""),
            "steps_taken": int(result.get("steps_taken") or 0),
            "duration_sec": result.get("duration_sec"),
            "run_dir": str(result.get("run_dir") or ""),
        }
        if needs_credentials:
            output["needs_credentials"] = needs_credentials
            output["next_action"] = "provision_credentials"
            output["credential_hint"] = (
                "No vault credentials were available for this site. Ask the user via the "
                "credential request flow (browser_credential_request), then re-run browser.run "
                "with the resulting credential_ref."
            )
        await self._emit_progress(
            task.task_id,
            f"Browser run finished: {status} ({output['steps_taken']} steps)",
        )
        return AgentResult(status="completed", output=output, artifacts=[], error=None)

    _ARTIFACT_CONTEXT_MAX_CHARS = 20000
    _TEXT_SUFFIXES = (
        ".txt", ".md", ".csv", ".tsv", ".json", ".jsonl", ".log",
        ".yaml", ".yml", ".html", ".xml", ".py", ".js", ".ts",
    )

    def _goal_with_artifact_context(self, goal: str, task: TaskEnvelope) -> str:
        """Consume input_artifacts the orchestrator attached (same channel the
        other specialists use). Text-like artifacts are read (bounded) and
        appended to the goal context; anything unreadable is noted by path."""
        artifacts = task.input_artifacts if isinstance(task.input_artifacts, list) else []
        if not artifacts:
            return goal
        sections: list[str] = []
        for item in artifacts[:8]:
            if not isinstance(item, dict):
                continue
            logical_path = str(item.get("path") or item.get("logical_path") or "").strip()
            label = str(item.get("filename") or item.get("artifact_id") or logical_path or "file").strip()
            local_path = self._resolve_artifact_path(logical_path)
            if not logical_path:
                continue
            text = self._read_bounded_text(local_path) if local_path else None
            if text:
                sections.append(f"--- {label} ({logical_path}) ---\n{text}")
            else:
                sections.append(f"--- {label} ({logical_path}) --- [binary or unreadable file; path only]")
        if not sections:
            return goal
        return (
            f"{goal}\n\n"
            "INPUT FILES attached to this task (content below; use it as part of the goal context):\n"
            + "\n\n".join(sections)
        )

    def _resolve_artifact_path(self, logical_path: str) -> Path | None:
        if not logical_path:
            return None
        candidates = [Path(logical_path)]
        # Logical artifact paths look like runs/artifacts/<task_id>/<...>;
        # resolve against the backend root and the artifacts root.
        for root in (self.config.working_dir_root, self.config.artifacts_root.parent):
            candidates.append(root / logical_path)
            candidates.append(root / logical_path.replace("runs/artifacts/", "runs/artifacts/"))
        for candidate in candidates:
            try:
                resolved = candidate.expanduser().resolve()
                if resolved.is_file():
                    return resolved
            except OSError:
                continue
        return None

    def _read_bounded_text(self, path: Path | None) -> str | None:
        if path is None:
            return None
        try:
            if path.suffix.lower() not in self._TEXT_SUFFIXES:
                return None
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        limit = self._ARTIFACT_CONTEXT_MAX_CHARS
        if len(text) > limit:
            text = text[:limit] + f"\n... [truncated, {len(text) - limit} more chars]"
        return text.strip() or None

    def _post_run_usage(self, task: TaskEnvelope, result: dict[str, Any]) -> None:
        """Report one aggregated usage event per brain (Cosmic-OS usage/cost
        tracking convention, shared/usage.py). The browser run makes many
        LLM calls internally, so usage arrives as a run-level rollup."""
        llm_usage = result.get("llm_usage") if isinstance(result.get("llm_usage"), dict) else {}
        if not llm_usage:
            return
        try:
            from shared.usage import build_model_key, build_usage_event, begin_metered_call, post_usage_event

            brains = (
                ("base", "fireworks", str(os.getenv("BROWSER_AGENT_MODEL") or os.getenv("FIREWORKS_DEFAULT_MODEL") or "accounts/fireworks/models/glm-5p3-flash")),
                ("frontier", "xai", str(os.getenv("XAI_MODEL") or "grok-4.6")),
            )
            for tier, provider, model in brains:
                usage = llm_usage.get(tier) if isinstance(llm_usage.get(tier), dict) else {}
                total_tokens = int(usage.get("total_tokens") or 0)
                requests = int(usage.get("requests") or 0)
                if total_tokens <= 0 and requests <= 0:
                    continue
                model_key = build_model_key(provider, model)
                raw_usage = {
                    "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                    "completion_tokens": int(usage.get("completion_tokens") or 0),
                    "total_tokens": total_tokens,
                }
                event = build_usage_event(
                    metered_call=begin_metered_call(prefix="browser_run"),
                    source_component="agent",
                    source_id=self.agent_id,
                    task_id=task.task_id,
                    parent_task_id=task.parent_task_id,
                    session_id=task.session_id,
                    route="specialist",
                    operation="agent.browser.run",
                    model_key=model_key,
                    raw_usage=raw_usage,
                    success=True,
                    metadata_json={
                        "aggregation": "run_rollup",
                        "llm_requests": requests,
                        "steps_taken": result.get("steps_taken"),
                    },
                )
                try:
                    import asyncio as _asyncio

                    task_ref = _asyncio.get_running_loop().create_task(
                        post_usage_event(
                            client=self._http_client,
                            gateway_url=self.gateway_url,
                            internal_token=self.gateway_internal_token,
                            event=event,
                        )
                    )
                    # Hold a reference — unreferenced tasks can be GC'd.
                    self._usage_post_tasks.add(task_ref)
                    task_ref.add_done_callback(self._usage_post_tasks.discard)
                except Exception:
                    logger.debug("browser_agent.usage_post_failed task_id=%s", task.task_id, exc_info=True)
        except Exception:
            logger.debug("browser_agent.usage_build_failed task_id=%s", task.task_id, exc_info=True)

    def _credentials_from_auth(self) -> dict[str, dict[str, str]] | None:
        """Map dispatch-injected vault credentials to the run's secure store.

        auth.vault is the decrypted vault entry the orchestrator resolved at
        dispatch time — it never came from a model context.
        """
        auth = self.auth
        if not isinstance(auth, dict):
            return None
        vault = auth.get("vault")
        if not isinstance(vault, dict):
            return None
        site = str(vault.get("site_domain") or vault.get("site_url") or "").strip()
        username = str(vault.get("username") or "")
        password = str(vault.get("password") or "")
        if not site or not (username or password):
            return None
        return {
            site: {
                "username": username,
                "password": password,
                "totp_seed": str(vault.get("totp_seed") or ""),
                "notes": str(vault.get("notes") or ""),
                "site_url": str(vault.get("site_url") or ""),
            }
        }

    async def _emit_progress(self, task_id: str, message: str, **payload: Any) -> None:
        try:
            await self.emit_event(task_id, "task.progress", {"message": message, **payload})
        except Exception:
            logger.debug("browser_agent.progress_emit_failed task_id=%s", task_id, exc_info=True)

    @staticmethod
    def _safe_int(value: Any, default: int) -> int:
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _failed(
        code: str,
        message: str,
        *,
        retryable: bool = False,
        next_action: str = "escalate",
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
