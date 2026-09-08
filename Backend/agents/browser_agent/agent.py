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
from shared.contracts import AgentError
from shared.sqlite_client import connect_sync

from .config import AGENT_ROOT, BrowserAgentConfig

logger = logging.getLogger(__name__)

BROWSER_AGENT_ID = "cosmic/browser-agent:1.0.0"

_SCREENSHOT_MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

# Cheap keyword sniff on the AskUser question text — cosmetic only, drives
# whether the desktop card shows a masked password input, a plain text input,
# or a generic one. Mirrors the wording cosmic-browser-use's own credential
# governor uses (main.py:_detect_credential_handoff_reason) so the two stay
# in sync without importing across the repo boundary.
_PASSWORD_HINT_WORDS = ("password",)
_CODE_HINT_WORDS = ("verification code", "security code", "one-time code", "otp", "mfa", "2fa")

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

        async def on_progress(info: dict[str, Any]) -> None:
            await self._on_run_progress(task, info, live_state)

        async def ask_user_handler(question: str) -> str:
            return await self._ask_user_bridge(task, question, live_state)

        live_frame_last_sent = {"at": 0.0}

        async def on_live_frame(frame_b64: str) -> None:
            await self._live_frame_bridge(task, frame_b64, live_frame_last_sent)

        try:
            result = await run_goal(
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
            )
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
            return self._failed("INTERNAL_ERROR", f"Browser run failed: {exc}", retryable=False, next_action="escalate")

        self._post_run_usage(task, result)

        status = str(result.get("status") or "incomplete")
        needs_credentials = result.get("needs_credentials")
        answer = str(result.get("answer") or "")
        run_dir = str(result.get("run_dir") or "")
        output: dict[str, Any] = {
            "goal": goal,
            "status": status,
            "answer": answer,
            "steps_taken": int(result.get("steps_taken") or 0),
            "duration_sec": result.get("duration_sec"),
            "run_dir": run_dir,
        }
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
        await self._emit_progress(
            task.task_id,
            f"Browser run finished: {status} ({output['steps_taken']} steps)",
        )
        return AgentResult(status="completed", output=output, artifacts=[], error=None)

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
            resolved = destination.resolve()
            try:
                relative = resolved.relative_to(self.artifacts_root)
                logical_path = (Path("runs") / "artifacts" / relative).as_posix()
            except ValueError:
                logical_path = resolved.as_posix()
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

    @staticmethod
    def _classify_ask_user_kind(question: str) -> str:
        lowered = question.lower()
        if any(word in lowered for word in _PASSWORD_HINT_WORDS):
            return "password"
        if any(word in lowered for word in _CODE_HINT_WORDS):
            return "verification_code"
        return "generic"

    async def _ask_user_bridge(
        self,
        task: TaskEnvelope,
        question: str,
        live_state: dict[str, Any],
    ) -> str:
        """Bridges cosmic-browser-use's AskUser action to the desktop.

        Makes ONE blocking call to the gateway, which holds the connection
        open until the user answers/skips on the live BrowserRunCard or the
        wait times out. This is the one synchronous, two-way exception to the
        otherwise one-way task.progress live-view channel — everything else
        (OTP, CAPTCHA, phone-approval, ambiguous forms) already funnels
        through this single AskUser action on the cosmic-browser-use side.
        """
        question = str(question or "").strip()
        request_id = f"bwi_{uuid4().hex[:12]}"
        kind = self._classify_ask_user_kind(question)
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
            raise RuntimeError(f"Could not reach the user for this question: {exc}") from exc

        await _clear_interrupt()
        status = str(result.get("status") or "")
        if status == "answered":
            answer = str(result.get("answer") or "").strip()
            if answer:
                return answer
            raise RuntimeError("The user submitted an empty answer.")
        if status == "skipped":
            raise RuntimeError("The user skipped this question — continue without it if possible.")
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
