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
import sys
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
