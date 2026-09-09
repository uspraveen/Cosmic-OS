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

import asyncio
import hashlib
import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from shared.agent_runtime import AgentResult, AgentRuntime, TaskEnvelope
from shared.contracts import AgentError, ArtifactManifest
from shared.sqlite_client import connect_sync

from .config import AGENT_ROOT, BrowserAgentConfig

logger = logging.getLogger(__name__)

BROWSER_AGENT_ID = "cosmic/browser-agent:1.0.0"


class _BrowserRunCancelled(Exception):
    """Raised internally when the orchestrator's Stop button fires while a
    browser.run is in flight. Caught in handle_browser_run to report a clean
    CANCELLED result instead of an internal error."""

_SCREENSHOT_MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

# Cheap keyword sniff on the AskUser question text — deterministic, no LLM
# call and no extra context on any model anywhere (cosmic-browser-use's own
# internal model just keeps calling one generic AskUser(question); this is
# pure post-hoc string matching in Python). Picks which of a small, fixed set
# of desktop card templates (kind -> widget, see BrowserRunCard on the
# frontend) fits the question, always falling back to a plain text field
# (`generic`) when nothing matches — so an unrecognized phrasing degrades to
# today's behavior rather than breaking.
#
# `password`/`verification_code` mirror the wording cosmic-browser-use's own
# credential governor uses (main.py:_detect_credential_handoff_reason) so the
# two stay in sync without importing across the repo boundary.
_PASSWORD_HINT_WORDS = ("password",)
_CODE_HINT_WORDS = ("verification code", "security code", "one-time code", "otp", "mfa", "2fa")
# `confirm`: nothing to type — the user does something themselves (approve on
# a phone, sign in in another window, solve a puzzle) and just acknowledges
# it. These get a single "Done — continue" button instead of a text field.
_CONFIRM_HINT_PHRASES = (
    "let me know when",
    "let me know once",
    "once you've",
    "once you have",
    "as soon as you",
    "when you're done",
    "when you are done",
    "when it's done",
    "when that's done",
    "reply that it's done",
    "reply that you're done",
    "tell me when",
    "reply when",
    "reply once",
    "approve this sign-in",
    "approve the sign-in",
    "confirm on your phone",
    "on your phone",
)
# `blocked`: a CAPTCHA/bot-check/manual-verification wall the agent
# explicitly cannot solve itself. Same free-text widget as `generic` (there's
# nothing more actionable to offer without full remote control of the
# browser, which this deliberately doesn't do) — just distinct card copy so
# the user understands why they're being asked instead of it looking like a
# stuck run.
_BLOCKED_HINT_PHRASES = (
    "captcha",
    "bot check",
    "not a robot",
    "verify you're human",
    "verify you are human",
    "sign in to confirm",
    "not allowed to bypass",
    "cannot bypass",
    "can't bypass",
    "blocked by",
    "security check",
)

# Live view frame rate cap. CDP pushes a frame on every repaint, which can be
# much faster than this during animations — we drop frames rather than slow
# Chrome down, so the browser is never throttled by how fast the gateway/
# desktop can keep up. ~2.5fps is plenty for "watch what it's doing", far
# below anything that would stress the gateway broadcast path.
_LIVE_FRAME_MIN_INTERVAL_SEC = 0.4

# Session recall ledger (browser.recall_session) — mirrors the Firecrawl
# specialist's firecrawl_session_runs table exactly (same columns' spirit,
# adapted for a goal-oriented run instead of a URL-keyed scrape). Indexes
# already-existing run artifacts; never duplicates their content.
_SESSION_RUNS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS browser_session_runs (
    task_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    goal TEXT NOT NULL,
    status TEXT NOT NULL,
    target_url TEXT,
    summary TEXT NOT NULL,
    artifact_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_browser_session_runs_session_created
ON browser_session_runs (session_id, created_at DESC);
"""


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

        # Accumulates the latest step/screenshot/interrupt so every progress
        # emit (including AskUser interrupts, which don't have a new step of
        # their own) carries the full live-view snapshot the desktop needs —
        # otherwise the live screenshot would blink out while a question is
        # pending. Mirrors the Slide Agent's LiveDeckProgress accumulator.
        live_state: dict[str, Any] = {}
        # Every AskUser question this run makes, with its outcome — surfaced
        # to the orchestrator in the final result (see `user_interrupts`
        # below) so it has full knowledge of what its specialist needed from
        # the user, even though it isn't in the loop for resolving any of
        # them in real time (that happens directly between the browser agent
        # and the desktop — see _ask_user_bridge). Secrets (password/
        # verification_code answers) are never included, only whether one
        # was provided.
        interrupt_log: list[dict[str, Any]] = []

        async def on_progress(info: dict[str, Any]) -> None:
            await self._on_run_progress(task, info, live_state)

        async def ask_user_handler(question: str, model_kind: str) -> str:
            return await self._ask_user_bridge(task, question, model_kind, live_state, interrupt_log)

        live_frame_last_sent = {"at": 0.0}

        async def on_live_frame(frame_b64: str) -> None:
            await self._live_frame_bridge(task, frame_b64, live_frame_last_sent)

        try:
            result = await self._run_goal_with_cancel_watch(
                task,
                run_goal(
                    goal,
                    initial_url=initial_url,
                    max_steps=max_steps,
                    memory_mode=memory_mode,
                    headless=headless,
                    credentials=credentials,
                    on_progress=on_progress,
                    ask_user_handler=ask_user_handler,
                    on_live_frame=on_live_frame,
                    working_dir_root=str(self.config.working_dir_root),
                    run_timeout_sec=min(self.config.run_timeout_sec, self.max_task_duration_sec),
                ),
            )
        except _BrowserRunCancelled:
            self._record_session_run(
                task,
                goal=goal,
                status="cancelled",
                summary=f"Cancelled by user: {goal[:150]}",
                target_url=live_state.get("url"),
                screenshot=live_state.get("screenshot"),
                run_dir=None,
            )
            await self._emit_terminal_progress(
                task,
                live_state,
                phase="cancelled",
                status="cancelled",
                message="Browser run stopped by the user.",
            )
            return self._failed("CANCELLED", "Browser run was stopped by the user.", retryable=False, next_action="skip")
        except BrowserRunError as exc:
            self._record_session_run(
                task,
                goal=goal,
                status="failed",
                summary=str(exc).strip()[:400] or f"Failed: {goal[:150]}",
                target_url=live_state.get("url"),
                screenshot=live_state.get("screenshot"),
                run_dir=None,
            )
            await self._emit_terminal_progress(
                task,
                live_state,
                phase="failed",
                status="failed",
                message="Browser run failed.",
            )
            return self._failed("BROWSER_RUN_FAILED", str(exc), retryable=False, next_action="escalate")
        except Exception as exc:
            logger.exception("browser_agent.run_failed task_id=%s", task.task_id)
            self._record_session_run(
                task,
                goal=goal,
                status="failed",
                summary=f"Failed: {goal[:150]}",
                target_url=live_state.get("url"),
                screenshot=live_state.get("screenshot"),
                run_dir=None,
            )
            await self._emit_terminal_progress(
                task,
                live_state,
                phase="failed",
                status="failed",
                message="Browser run failed.",
            )
            return self._failed("INTERNAL_ERROR", f"Browser run failed: {exc}", retryable=False, next_action="escalate")

        self._post_run_usage(task, result)

        status = str(result.get("status") or "incomplete")
        needs_credentials = result.get("needs_credentials")
        answer = str(result.get("answer") or "")
        run_dir = str(result.get("run_dir") or "")
        # Anything the run offloaded to its own large-note store becomes a
        # real artifact here, and the dangling pointers the answer may carry
        # are rewritten to name it — see _persist_large_notes.
        note_manifests, note_refs, note_paths = self._persist_large_notes(task, run_dir)
        answer = self._resolve_large_note_pointers(answer, note_paths)
        output: dict[str, Any] = {
            "goal": goal,
            "status": status,
            "answer": answer,
            "steps_taken": int(result.get("steps_taken") or 0),
            "duration_sec": result.get("duration_sec"),
            "run_dir": run_dir,
        }
        if note_refs:
            output["artifacts"] = note_refs
            output["artifact_hint"] = (
                "The run offloaded long extracts to these files. Load one with "
                "artifact_read using its path before answering from the summary alone."
            )
        if interrupt_log:
            # Post-hoc visibility only — the orchestrator was never in the
            # real-time loop for any of these (they're resolved directly
            # between the browser agent and the desktop). This lets it know
            # what its specialist needed from the user and how that went,
            # e.g. to explain an "incomplete" status or follow up on a skip.
            output["user_interrupts"] = interrupt_log
        if needs_credentials:
            output["needs_credentials"] = needs_credentials
            output["next_action"] = "provision_credentials"
            output["credential_hint"] = (
                "No vault credentials were available for this site. Ask the user via the "
                "credential request flow (browser_credential_request), then re-run browser.run "
                "with the resulting credential_ref."
            )
        recall_summary = str(result.get("recall_summary") or "").strip()
        self._record_session_run(
            task,
            goal=goal,
            status=status,
            summary=recall_summary or answer[:400] or f"{status.capitalize()}: {goal[:150]}",
            target_url=live_state.get("url"),
            screenshot=live_state.get("screenshot"),
            run_dir=run_dir or None,
        )
        await self._emit_terminal_progress(
            task,
            live_state,
            phase="finished",
            status=status,
            message=f"Browser run finished: {status} ({output['steps_taken']} steps)",
        )
        return AgentResult(status="completed", output=output, artifacts=note_manifests, error=None)

    async def handle_browser_recall_session(self, task: TaskEnvelope) -> AgentResult:
        """Look up prior browser.run work for a session. No browser is launched —
        this only reads the session ledger _record_session_run writes to."""
        session_id = str(task.input.get("session_id") or "").strip()
        if not session_id:
            return self._failed("INVALID_INPUT", "browser.recall_session requires a session_id.")
        query = str(task.input.get("query") or "").strip()
        limit = self._safe_int(task.input.get("limit"), 10)
        limit = min(max(limit, 1), 50)
        entries = self._load_session_entries(session_id=session_id, query=query, limit=limit)
        if entries:
            response = f"Found {len(entries)} browser run{'s' if len(entries) != 1 else ''} for {session_id}."
        elif query:
            response = f"No browser runs matching {query!r} were recorded for {session_id}."
        else:
            response = f"No browser runs were recorded for {session_id}."
        return AgentResult(
            status="completed",
            output={"response": response, "session_id": session_id, "entries": entries},
            artifacts=[],
            error=None,
        )

    def _record_session_run(
        self,
        task: TaskEnvelope,
        *,
        goal: str,
        status: str,
        summary: str,
        target_url: str | None,
        screenshot: dict[str, Any] | None,
        run_dir: str | None,
    ) -> None:
        """Index this run into the session recall ledger. Never raises — a
        broken ledger write must not fail an otherwise-successful browser run,
        same non-fatal-by-design philosophy as the rest of this file."""
        try:
            session_id = str(task.session_id or "").strip() or "no_session"
            artifact_refs: list[dict[str, Any]] = []
            if isinstance(screenshot, dict) and screenshot.get("artifact_id"):
                artifact_refs.append(
                    {
                        "kind": "screenshot",
                        "artifact_id": screenshot.get("artifact_id"),
                        "path": screenshot.get("path"),
                    }
                )
            if run_dir:
                artifact_refs.append({"kind": "run_dir", "path": run_dir})
            created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            with connect_sync(self.config.session_db_path) as connection:
                connection.executescript(_SESSION_RUNS_TABLE_SQL)
                connection.execute(
                    """
                    INSERT OR REPLACE INTO browser_session_runs (
                        task_id, session_id, goal, status, target_url,
                        summary, artifact_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task.task_id,
                        session_id,
                        goal.strip()[:2000],
                        status,
                        (target_url or "").strip() or None,
                        summary.strip()[:2000],
                        json.dumps(artifact_refs, ensure_ascii=False),
                        created_at,
                    ),
                )
        except Exception:
            logger.debug(
                "browser_agent.session_run_record_failed task_id=%s", task.task_id, exc_info=True
            )

    def _load_session_entries(
        self, *, session_id: str, query: str, limit: int
    ) -> list[dict[str, Any]]:
        try:
            with connect_sync(self.config.session_db_path) as connection:
                connection.executescript(_SESSION_RUNS_TABLE_SQL)
                if query:
                    like = f"%{query.lower()}%"
                    rows = connection.execute(
                        """
                        SELECT task_id, goal, status, summary, target_url, artifact_json, created_at
                        FROM browser_session_runs
                        WHERE session_id = ?
                          AND (
                            lower(goal) LIKE ? OR lower(summary) LIKE ? OR lower(coalesce(target_url, '')) LIKE ?
                          )
                        ORDER BY created_at DESC, task_id DESC
                        LIMIT ?
                        """,
                        (session_id, like, like, like, limit),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        """
                        SELECT task_id, goal, status, summary, target_url, artifact_json, created_at
                        FROM browser_session_runs
                        WHERE session_id = ?
                        ORDER BY created_at DESC, task_id DESC
                        LIMIT ?
                        """,
                        (session_id, limit),
                    ).fetchall()
        except Exception:
            logger.debug("browser_agent.session_run_load_failed session_id=%s", session_id, exc_info=True)
            return []
        entries: list[dict[str, Any]] = []
        for row in rows:
            try:
                artifact_refs = json.loads(row["artifact_json"] or "[]")
            except (TypeError, ValueError):
                artifact_refs = []
            entries.append(
                {
                    "task_id": row["task_id"],
                    "goal": row["goal"],
                    "status": row["status"],
                    "summary": row["summary"],
                    "target_url": row["target_url"],
                    "artifact_refs": artifact_refs if isinstance(artifact_refs, list) else [],
                    "created_at": row["created_at"],
                }
            )
        return entries

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

    async def _on_run_progress(
        self,
        task: TaskEnvelope,
        info: dict[str, Any],
        live_state: dict[str, Any],
    ) -> None:
        """cosmic-browser-use's per-step hook — feeds the live BrowserRunCard."""
        step = info.get("step")
        description = str(info.get("description") or info.get("action_type") or "Working").strip()
        live_state.update(
            {
                "step": step,
                "max_steps": info.get("max_steps"),
                "action_type": info.get("action_type"),
                "description": description,
                "url": info.get("url"),
                "page_title": info.get("page_title"),
                "elapsed_sec": info.get("elapsed_sec"),
            }
        )
        screenshot = self._attach_progress_screenshot(task, info)
        if screenshot:
            live_state["screenshot"] = screenshot
        live_state.pop("interrupt", None)
        await self._emit_progress(
            task.task_id,
            f"Step {step}: {description}" if step else description,
            step=step,
            action_type=info.get("action_type"),
            estimated_completion=info.get("estimated_completion"),
            browser_progress=dict(live_state),
        )

    async def _emit_terminal_progress(
        self,
        task: TaskEnvelope,
        live_state: dict[str, Any],
        *,
        phase: str,
        status: str,
        message: str,
    ) -> None:
        """Stamp the run as over on the live-progress channel.

        Without this the desktop card has no way to know: its only other
        signal is whether the *assistant's whole response* is still streaming,
        and the orchestrator keeps writing long after its specialist finished
        — so the card sat at "Running" with a ticking clock while the answer
        was already being written. Reuses the same browser_progress channel
        the per-step readings ride on, so nothing new has to be plumbed."""
        live_state["phase"] = phase
        live_state["status"] = status
        live_state.pop("interrupt", None)
        await self._emit_progress(
            task.task_id,
            message,
            browser_progress=dict(live_state),
        )

    def _persist_large_notes(
        self,
        task: TaskEnvelope,
        run_dir: str,
    ) -> tuple[list[ArtifactManifest], list[dict[str, str]], dict[str, str]]:
        """Surface the run's offloaded large notes as real COSMIC artifacts.

        cosmic-browser-use parks big extracts (long tables, article bodies,
        DOM dumps) in its own `large_notes.jsonl` under the run directory and
        leaves a `[LargeNote:ln_...]` pointer in the agent's in-context notes.
        Those pointers are meaningful only inside that run's own process — but
        they leak outward, because the final answer is lifted from the last
        note, so a run whose report was offloaded hands the orchestrator a
        pointer to a store it cannot reach. It then tries `artifact_read` on
        the pointer text and gets nothing.

        Writing each note into the task's artifact dir the way the Firecrawl
        agent writes scrape bodies closes that gap with the pipeline that
        already exists: the orchestrator reads them with the same
        `artifact_read(path=...)` it uses for every other specialist's files.

        Returns (manifests, compact refs, {note_id: logical_path}).
        """
        manifests: list[ArtifactManifest] = []
        refs: list[dict[str, str]] = []
        paths_by_id: dict[str, str] = {}
        if not run_dir:
            return manifests, refs, paths_by_id
        source = Path(run_dir) / "large_notes.jsonl"
        if not source.is_file():
            return manifests, refs, paths_by_id
        try:
            lines = source.read_text(encoding="utf-8").splitlines()
        except OSError:
            logger.debug("browser_agent.large_notes_read_failed task_id=%s", task.task_id, exc_info=True)
            return manifests, refs, paths_by_id

        notes_dir = self.artifacts_root / task.task_id / "browser_agent" / "notes"
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            note_id = str(entry.get("id") or "").strip()
            content = str(entry.get("content") or "")
            if not note_id or not content.strip():
                continue
            body = self._render_large_note(entry)
            try:
                notes_dir.mkdir(parents=True, exist_ok=True)
                destination = notes_dir / f"{self._safe_filename(note_id)}.md"
                destination.write_text(body, encoding="utf-8")
            except OSError:
                logger.debug(
                    "browser_agent.large_note_write_failed task_id=%s note_id=%s",
                    task.task_id,
                    note_id,
                    exc_info=True,
                )
                continue
            logical_path = self._logical_artifact_path(destination)
            manifest = ArtifactManifest(
                artifact_id=f"art_{uuid4().hex[:12]}",
                task_id=task.task_id,
                mime="text/markdown",
                sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
                path=logical_path,
                source_url=str(entry.get("url") or "") or None,
                created_by_agent=self.agent_id,
                audience="supporting",
            )
            manifests.append(manifest)
            ref: dict[str, str] = {
                "artifact_id": manifest.artifact_id,
                "path": logical_path,
                "mime": "text/markdown",
                "filename": destination.name,
                "audience": "supporting",
                "note_id": note_id,
            }
            title = str(entry.get("title") or "").strip()
            contains = str(entry.get("contains") or "").strip()
            if title:
                ref["title"] = title[:160]
            if contains:
                ref["contains"] = contains[:200]
            refs.append(ref)
            paths_by_id[note_id] = logical_path
        return manifests, refs, paths_by_id

    @staticmethod
    def _render_large_note(entry: dict[str, Any]) -> str:
        """The note's own metadata as a small front-matter block, then the
        body — so a reader landing on the file alone knows what it is and
        where it came from."""
        header = [f"# {str(entry.get('title') or entry.get('id') or 'Browser note').strip()}"]
        for label, key in (("Contains", "contains"), ("Why", "why"), ("Summary", "summary"), ("Source", "url")):
            value = str(entry.get(key) or "").strip()
            if value:
                header.append(f"- **{label}:** {value}")
        header.append("")
        return "\n".join(header) + "\n" + str(entry.get("content") or "")

    @staticmethod
    def _safe_filename(value: str) -> str:
        cleaned = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in str(value))
        return cleaned.strip("-") or "note"

    def _logical_artifact_path(self, path: Path) -> str:
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(self.artifacts_root.resolve())
        except ValueError:
            return resolved.as_posix()
        return (Path("runs") / "artifacts" / relative).as_posix()

    def _resolve_large_note_pointers(
        self,
        answer: str,
        paths_by_id: dict[str, str],
    ) -> str:
        """Rewrite `[LargeNote:ln_...]` pointers into the artifact path that
        now holds the same text, so the orchestrator can follow them.

        The pointer's own descriptive tail is kept — it is a genuinely useful
        one-line summary of what was offloaded — and only the dangling id is
        replaced with something readable.
        """
        if not answer or not paths_by_id:
            return answer
        rewritten = answer
        for note_id, logical_path in paths_by_id.items():
            rewritten = rewritten.replace(
                f"[LargeNote:{note_id}",
                f"[Saved to artifact {logical_path} (read it with artifact_read) | note {note_id}",
            )
        return rewritten

    def _attach_progress_screenshot(
        self,
        task: TaskEnvelope,
        info: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Copy this step's screenshot into the task artifact dir and stamp it
        with an artifact id — mirrors SlideAgent._attach_preview_files /
        _artifact_manifest so the gateway's existing artifact-preview pipeline
        (mint_artifact_access_url) can serve it to the desktop unchanged."""
        raw_path = info.get("screenshot_path")
        if not raw_path:
            return None
        source = Path(str(raw_path))
        if not source.is_file():
            return None
        try:
            preview_dir = self.artifacts_root / task.task_id / "browser_agent" / "previews"
            preview_dir.mkdir(parents=True, exist_ok=True)
            step = self._safe_int(info.get("step"), 0)
            suffix = source.suffix.lower() or ".jpg"
            destination = preview_dir / f"step-{step:03d}{suffix}"
            if source.resolve() != destination.resolve():
                shutil.copy2(source, destination)
            digest = hashlib.sha256(destination.read_bytes()).hexdigest()
            logical_path = self._logical_artifact_path(destination)
            return {
                "artifact_id": f"art_{uuid4().hex[:12]}",
                "path": logical_path,
                "mime": _SCREENSHOT_MIME_BY_SUFFIX.get(suffix, "image/jpeg"),
                "filename": destination.name,
                "sha256": digest,
            }
        except OSError:
            logger.debug(
                "browser_agent.screenshot_attach_failed task_id=%s step=%s",
                task.task_id,
                info.get("step"),
                exc_info=True,
            )
            return None

    async def _live_frame_bridge(
        self,
        task: TaskEnvelope,
        frame_b64: str,
        last_sent: dict[str, float],
    ) -> None:
        """Forwards one CDP screencast frame to the gateway's live-frame relay.

        Throttled client-side (frames arrive on Chrome's own repaint cadence,
        which can far exceed what's worth shipping over the network) and
        fire-and-forget: a slow or unreachable gateway just means this frame
        (and maybe the next few) get dropped, never a stall in the browser
        loop — start_live_screencast() already acks every CDP frame before
        this is even called.
        """
        if not frame_b64:
            return
        now = time.monotonic()
        if now - last_sent["at"] < _LIVE_FRAME_MIN_INTERVAL_SEC:
            return
        last_sent["at"] = now
        try:
            await self._http_client.post(
                f"{self.gateway_url.rstrip('/')}/internal/browser/live-frame",
                json={
                    "task_id": task.task_id,
                    "frame": f"data:image/jpeg;base64,{frame_b64}",
                },
                headers={"X-Internal-Token": self.gateway_internal_token},
                timeout=5.0,
            )
        except Exception:
            logger.debug("browser_agent.live_frame_send_failed task_id=%s", task.task_id, exc_info=True)

    async def _run_goal_with_cancel_watch(self, task: TaskEnvelope, coro: Any) -> Any:
        """Races `coro` (the run_goal(...) call) against the orchestrator's
        Stop-button cancel signal.

        The orchestrator's cancel_task() sets `task_cancel:{task_id}` in
        Redis (Backend/orchestrator/runtime.py:_request_agent_task_cancel) —
        it does not push a cancellation into the specialist itself, and
        cosmic-browser-use's step loop has no cancellation hook of its own
        (unlike Alpha, which polls this same key between CLI turns), so
        without this a Stop click only ends the orchestrator's own turn: the
        Chromium session, LLM calls, and CDP screencast all keep running
        server-side until the goal finishes or times out on its own.

        On cancel we asyncio.Task.cancel() the run — cosmic-browser-use's own
        `finally` cleanup (stop_live_screencast/close) still executes for a
        CancelledError exactly like any other exception, so the browser is
        torn down either way.
        """
        run_future = asyncio.ensure_future(coro)
        watch_future = asyncio.ensure_future(self._watch_for_cancel(task.task_id))
        try:
            done, _pending = await asyncio.wait(
                {run_future, watch_future}, return_when=asyncio.FIRST_COMPLETED
            )
            if run_future in done:
                return run_future.result()
            run_future.cancel()
            try:
                await run_future
            except (asyncio.CancelledError, Exception):
                pass
            raise _BrowserRunCancelled()
        finally:
            if not watch_future.done():
                watch_future.cancel()
                try:
                    await watch_future
                except (asyncio.CancelledError, Exception):
                    pass

    async def _watch_for_cancel(self, task_id: str) -> None:
        """Polls the orchestrator's cancel flag every 2s. Returns as soon as
        it's set; runs until externally cancelled otherwise (i.e. once the
        real run finishes first)."""
        while True:
            try:
                raw = await self.redis.get(f"task_cancel:{task_id}")
                if raw:
                    return
            except Exception:
                logger.debug("browser_agent.cancel_watch_failed task_id=%s", task_id, exc_info=True)
            await asyncio.sleep(2.0)

    @staticmethod
    def _classify_ask_user_kind(question: str) -> str:
        """Picks a card template for this AskUser question. Order matters:
        password/code checks are most specific (never want to mistake either
        for a plain confirmation), confirm is checked before blocked because
        it pins down the actual widget (button vs. text) while blocked is
        only about framing/copy on the same text widget generic uses."""
        lowered = question.lower()
        if any(word in lowered for word in _PASSWORD_HINT_WORDS):
            return "password"
        if any(word in lowered for word in _CODE_HINT_WORDS):
            return "verification_code"
        if any(phrase in lowered for phrase in _CONFIRM_HINT_PHRASES):
            return "confirm"
        if any(phrase in lowered for phrase in _BLOCKED_HINT_PHRASES):
            return "blocked"
        return "generic"

    # Recognized card templates (see BrowserRunCard on the frontend). Must
    # match cosmic-browser-use's own _ASK_USER_MODEL_KINDS + "password".
    _KNOWN_ASK_USER_KINDS = {"password", "verification_code", "confirm", "blocked", "generic"}

    async def _ask_user_bridge(
        self,
        task: TaskEnvelope,
        question: str,
        model_kind: str,
        live_state: dict[str, Any],
        interrupt_log: list[dict[str, Any]],
    ) -> str:
        """Bridges cosmic-browser-use's AskUser action to the desktop.

        Makes ONE blocking call to the gateway, which holds the connection
        open until the user answers/skips on the live BrowserRunCard or the
        wait times out. This is the one synchronous, two-way exception to the
        otherwise one-way task.progress live-view channel — everything else
        (OTP, CAPTCHA, phone-approval, ambiguous forms) already funnels
        through this single AskUser action on the cosmic-browser-use side.

        `model_kind` is cosmic-browser-use's own hint for which card fits —
        set by its model when it decides to ask, or deterministically by its
        credential governor for password/OTP fields. Trusted first, since
        the agent already knows why it's asking; that beats us re-guessing
        intent from the question text after the fact. The keyword classifier
        (_classify_ask_user_kind) only runs as a fallback when this is
        missing or not a value we recognize, so nothing breaks if a model
        skips the field or an older bridge doesn't set it.
        """
        question = str(question or "").strip()
        request_id = f"bwi_{uuid4().hex[:12]}"
        normalized_model_kind = str(model_kind or "").strip().lower()
        kind = (
            normalized_model_kind
            if normalized_model_kind in self._KNOWN_ASK_USER_KINDS
            else self._classify_ask_user_kind(question)
        )
        timeout_sec = max(5.0, float(self.config.ask_user_wait_sec))

        live_state["interrupt"] = {
            "request_id": request_id,
            "question": question,
            "kind": kind,
            "status": "pending",
        }
        await self._emit_progress(task.task_id, question, browser_progress=dict(live_state))

        async def _clear_interrupt() -> None:
            live_state.pop("interrupt", None)
            await self._emit_progress(task.task_id, "", browser_progress=dict(live_state))

        # Recorded for the orchestrator's post-hoc visibility (see
        # `user_interrupts` in handle_browser_run) regardless of outcome.
        # Secret-bearing kinds never carry the actual answer text — only
        # whether one was provided.
        log_entry: dict[str, Any] = {"question": question, "kind": kind}

        try:
            response = await self._http_client.post(
                f"{self.gateway_url.rstrip('/')}/internal/browser/ask-user",
                json={
                    "request_id": request_id,
                    "question": question,
                    "kind": kind,
                    "task_id": task.task_id,
                    "session_id": task.session_id,
                    "timeout_sec": timeout_sec,
                },
                headers={"X-Internal-Token": self.gateway_internal_token},
                timeout=timeout_sec + 15.0,
            )
            response.raise_for_status()
            result = response.json()
        except Exception as exc:
            await _clear_interrupt()
            log_entry["status"] = "error"
            interrupt_log.append(log_entry)
            raise RuntimeError(f"Could not reach the user for this question: {exc}") from exc

        await _clear_interrupt()
        status = str(result.get("status") or "")
        if status == "answered":
            answer = str(result.get("answer") or "").strip()
            if answer:
                log_entry["status"] = "answered"
                if kind not in ("password", "verification_code"):
                    log_entry["answer"] = answer[:200]
                interrupt_log.append(log_entry)
                return answer
            log_entry["status"] = "empty_answer"
            interrupt_log.append(log_entry)
            raise RuntimeError("The user submitted an empty answer.")
        if status == "skipped":
            log_entry["status"] = "skipped"
            interrupt_log.append(log_entry)
            raise RuntimeError("The user skipped this question — continue without it if possible.")
        log_entry["status"] = "timeout"
        interrupt_log.append(log_entry)
        raise RuntimeError(f"No response from the user within {int(timeout_sec)}s.")

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
