from __future__ import annotations

import logging
import asyncio
import shutil
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

from shared.agent_runtime import AgentRuntime
from shared.archive_safety import ArchiveRejected, safe_extract_zip
from shared.bundle_artifacts import is_supported_bundle_artifact
from shared.contracts import AgentError, AgentResult, TaskEnvelope

from .artifact_promoter import promote_alpha_artifacts
from .codex_runner import CodexRunResult, CodexWorkspaceRunner
from .config import AGENT_ROOT, AlphaAgentConfig
from .cursor_runner import CursorRunResult, CursorWorkspaceRunner
from .docker_runner import DockerWorkspaceRunner
from .instructions import seed_workspace_instructions
from .project_registry import HarnessSessionRecord, ProjectCandidate, ProjectRecord, ProjectRegistry
from .workspace_manager import WorkspaceManager, WorkspacePaths

logger = logging.getLogger(__name__)


class AlphaAgent(AgentRuntime):
    """Alpha-stage COSMIC project operator."""

    def __init__(
        self,
        redis_client: Any,
        *,
        config: AlphaAgentConfig | None = None,
    ) -> None:
        self.config = config or AlphaAgentConfig.from_env()
        self.registry = ProjectRegistry(self.config.project_db_path)
        self.workspace_manager = WorkspaceManager(
            self.config.alpha_root,
            codex_home=self.config.codex_home,
            cursor_home=self.config.cursor_home,
        )
        self.docker_runner = DockerWorkspaceRunner(self.config)
        self.codex_runner = CodexWorkspaceRunner(self.config)
        self.cursor_runner = CursorWorkspaceRunner(self.config)
        super().__init__(
            agent_card_path=AGENT_ROOT / "agent_card.yaml",
            redis_client=redis_client,
            gateway_url=self.config.gateway_url,
            gateway_internal_token=self.config.gateway_internal_token,
            orchestrator_url=self.config.orchestrator_url,
            orchestrator_internal_token=self.config.orchestrator_internal_token,
        )

    async def on_startup(self) -> None:
        self.registry.initialize()
        self.workspace_manager.ensure_base_layout()
        (AGENT_ROOT / "store").mkdir(parents=True, exist_ok=True)
        (AGENT_ROOT / "runtime").mkdir(parents=True, exist_ok=True)
        learnings_path = AGENT_ROOT / "store" / "learnings.md"
        if not learnings_path.exists():
            learnings_path.write_text("# Alpha Agent Learnings\n", encoding="utf-8")

    async def execute(self, task: TaskEnvelope) -> AgentResult:
        try:
            self._load_runtime_text()
            handler = getattr(self, f"handle_{task.intent.replace('.', '_')}", None)
            if handler is None:
                return self._fail(
                    code="INVALID_INPUT",
                    message=f"Unknown intent: {task.intent}",
                    next_action="escalate",
                )
            return await handler(task)
        except Exception as exc:
            logger.exception("alpha_agent.task_failed task_id=%s intent=%s", task.task_id, task.intent)
            return self._fail(
                code="INTERNAL_ERROR",
                message=str(exc)[:500] or "Alpha agent execution failed.",
                next_action="escalate",
            )

    async def handle_alpha_execute(self, task: TaskEnvelope) -> AgentResult:
        goal = str(task.input.get("goal") or "").strip()
        if not goal:
            return self._fail(
                code="INVALID_INPUT",
                message="alpha.execute requires a non-empty goal.",
                next_action="escalate",
            )

        mode = str(task.input.get("mode") or "auto").strip().lower()
        if mode not in {"auto", "new_project", "existing_project"}:
            return self._fail(
                code="INVALID_INPUT",
                message="mode must be one of: auto, new_project, existing_project.",
                next_action="escalate",
            )

        if self.step_plan is not None:
            await self.step_plan.create(
                [
                    "Resolve or create Alpha project",
                    "Prepare isolated workspace directories",
                    "Inspect Docker runner readiness",
                    "Verify Alpha CLI auth and execution readiness",
                    "Execute task with selected Alpha CLI",
                ]
            )

        context_brief = self._task_context_brief(task)
        requested_harness = self._optional_string(task.input.get("preferred_harness"))
        if requested_harness and requested_harness.lower() == "auto":
            requested_harness = None

        project = self._resolve_project(task, mode=mode)
        if project is None and mode == "existing_project":
            return self._fail(
                code="PROJECT_NOT_FOUND",
                message="No Alpha project matched the supplied project_ref or current session.",
                next_action="ask_user",
            )
        if project is None:
            aliases = self._coerce_string_list(task.input.get("aliases"))
            project = self.registry.create_project(
                aliases=aliases,
                repo_url=self._optional_string(task.input.get("repo_url")),
                deployment_url=self._optional_string(task.input.get("deployment_url")),
                last_task_id=task.task_id,
                last_session_id=task.session_id,
                summary=goal[:500],
                status="prepared",
                goal=goal[:2000],
                context_brief=context_brief,
                preferred_harness=requested_harness,
            )

        if self.step_plan is not None:
            await self.step_plan.update(1, "completed", f"Using Alpha project {project.project_id}")

        paths = self.workspace_manager.prepare(project_id=project.project_id, task_id=task.task_id)
        project = self.registry.mark_task(
            project.project_id,
            task_id=task.task_id,
            session_id=task.session_id,
            local_path=str(paths.workspace),
            summary=goal[:500],
            status="workspace_prepared",
            goal=goal[:2000],
            context_brief=context_brief,
            preferred_harness=requested_harness,
            repo_url=self._optional_string(task.input.get("repo_url")),
            deployment_url=self._optional_string(task.input.get("deployment_url")),
        )

        # Drop a project-aware AGENTS.md into the workspace so Codex/Cursor
        # pick it up via cwd-walk. Idempotent: same content => no rewrite.
        # Best-effort — never block task execution on instruction seeding.
        try:
            seed_workspace_instructions(
                workspace_path=paths.workspace,
                artifacts_path=paths.artifacts,
                project=project,
            )
        except Exception:
            logger.exception(
                "alpha.workspace_instructions_seed_failed task_id=%s project_id=%s",
                task.task_id,
                project.project_id,
            )

        if self.step_plan is not None:
            await self.step_plan.update(2, "completed", f"Workspace prepared at {paths.workspace}")

        docker_available = self.docker_runner.is_available()
        docker_report: dict[str, Any] = {
            "available": docker_available,
            "image": self.config.docker_image,
            "network": self.config.docker_network,
            "socket_mounted": False,
            "smoke_ran": False,
        }
        if bool(task.input.get("run_workspace_smoke")) and self.config.allow_docker_smoke:
            smoke = await self.docker_runner.run(
                paths=paths,
                command=["sh", "-lc", "pwd && test -d /workspace && test -d /artifacts"],
                timeout_sec=min(self.config.docker_timeout_sec, 60.0),
            )
            docker_report["smoke_ran"] = True
            docker_report["smoke"] = smoke.as_dict()

        if self.step_plan is not None:
            await self.step_plan.update(
                3,
                "completed",
                "Docker runner is available." if docker_available else "Docker executable not found.",
            )

        if bool(task.input.get("prepare_only")):
            if self.step_plan is not None:
                await self.step_plan.update(4, "completed", "Skipped Codex execution by request.")
                await self.step_plan.update(5, "completed", "Returned workspace preparation report.")
            return AgentResult(
                status="completed",
                output={
                    "status": "workspace_prepared",
                    "project": project.as_dict(),
                    "workspace": paths.as_dict(),
                    "docker": docker_report,
                    "harness": {"skipped": True, "reason": "prepare_only"},
                    "next_action": "Call alpha.execute again without prepare_only to execute with the selected Alpha CLI.",
                },
                artifacts=[],
                error=None,
            )

        preferred_harness = await self._resolve_preferred_harness(task)
        candidate_harnesses = self._candidate_harnesses(preferred_harness, task)
        if not candidate_harnesses:
            return self._fail(
                code="UNSUPPORTED_OPERATION",
                message="Alpha currently supports Codex and Cursor CLI execution. OpenCode is planned but not wired.",
                next_action="ask_user",
            )

        # Off the event loop: this now unpacks archives, not just copies files,
        # and the size ceiling is measured in hundreds of megabytes.
        staged_inputs = await asyncio.to_thread(
            self._stage_input_artifacts, task, workspace=paths.workspace
        )
        base_prompt = self._build_cli_prompt(
            task=task,
            project=project,
            workspace=str(paths.workspace),
            artifacts_dir=str(paths.artifacts),
            staged_inputs=staged_inputs,
        )

        async def cancel_check() -> bool:
            raw = await self.redis.get(f"task_cancel:{task.task_id}")
            return bool(raw)

        all_artifacts: list[Any] = []
        attempt_outputs: dict[str, Any] = {}
        fallback_from: dict[str, Any] | None = None
        last_result: CodexRunResult | CursorRunResult | None = None
        last_provider = candidate_harnesses[0]
        last_provider_status: dict[str, Any] = {}
        last_provider_label = self._harness_label(last_provider)

        for attempt_index, active_harness in enumerate(candidate_harnesses):
            provider_status = await self._safe_fetch_provider_status(active_harness)
            provider_ready = self._provider_status_is_ready(provider_status)
            provider_label = self._harness_label(active_harness)
            last_provider = active_harness
            last_provider_status = provider_status
            last_provider_label = provider_label

            if self.step_plan is not None and (attempt_index == 0 or provider_ready):
                await self.step_plan.update(
                    4,
                    "completed" if provider_ready else "failed",
                    f"{provider_label} is authenticated." if provider_ready else f"{provider_label} is not authenticated.",
                )

            emit_alpha_terminal = self._terminal_emitter(
                task=task,
                project_id=project.project_id,
                workspace=str(paths.workspace),
                provider=active_harness,
            )

            if not provider_ready:
                await emit_alpha_terminal(
                    {
                        "stream": "system",
                        "event_type": f"{active_harness}.login_required",
                        "text": f"{provider_label} is not authenticated.",
                        "detail": self._provider_login_message(active_harness, provider_status),
                    }
                )
                if last_result is not None:
                    break
                if attempt_index + 1 < len(candidate_harnesses) and candidate_harnesses[attempt_index + 1] != active_harness:
                    next_harness = candidate_harnesses[attempt_index + 1]
                    await self._emit_harness_fallback(
                        task=task,
                        project_id=project.project_id,
                        workspace=str(paths.workspace),
                        from_provider=active_harness,
                        to_provider=next_harness,
                        reason=f"{provider_label} is not authenticated.",
                    )
                    fallback_from = {
                        "provider": active_harness,
                        "reason": "login_required",
                    }
                    continue

                project = self.registry.mark_task(
                    project.project_id,
                    task_id=task.task_id,
                    session_id=task.session_id,
                    local_path=str(paths.workspace),
                    summary=goal[:500],
                    status=f"{active_harness}_login_required",
                    goal=goal[:2000],
                    context_brief=context_brief,
                    preferred_harness=active_harness,
                    repo_url=self._optional_string(task.input.get("repo_url")),
                    deployment_url=self._optional_string(task.input.get("deployment_url")),
                )
                return self._fail(
                    code=f"{active_harness.upper()}_LOGIN_REQUIRED",
                    message=self._provider_login_message(active_harness, provider_status),
                    next_action="ask_user",
                )

            await self.emit_event(
                task.task_id,
                "task.progress",
                {
                    "stage": f"alpha.{active_harness}.started",
                    "project_id": project.project_id,
                    "workspace": str(paths.workspace),
                    "attempt": attempt_index + 1,
                },
            )
            await emit_alpha_terminal(
                {
                    "stream": "system",
                    "event_type": f"{active_harness}.exec.started",
                    "text": self._harness_start_message(active_harness),
                    "detail": str(paths.workspace),
                }
            )

            prompt = self._prompt_for_attempt(
                base_prompt,
                fallback_from=fallback_from,
                previous_result=last_result,
            )
            selected_model: str | None = None
            native_session: HarnessSessionRecord | None = None
            if active_harness == "cursor":
                selected_model = self._select_cursor_model(task, provider_status)
                native_session = await self._prepare_cursor_native_session(
                    task=task,
                    project=project,
                    paths=paths,
                    model=selected_model,
                    emit=emit_alpha_terminal,
                )
                await emit_alpha_terminal(
                    {
                        "stream": "system",
                        "event_type": "cursor.model.selected",
                        "text": f"Cursor model selected: {selected_model or 'auto'}",
                        "detail": "COSMIC will fail this run if Cursor initializes a Fast model when a non-Fast model was requested.",
                    }
                )
                run_result = await self.cursor_runner.run(
                    paths=paths,
                    prompt=prompt,
                    model=selected_model,
                    resume_chat_id=native_session.native_session_id if native_session else None,
                    timeout_sec=self.config.cursor_timeout_sec,
                    event_callback=emit_alpha_terminal,
                    cancel_check=cancel_check,
                )
            else:
                selected_model = self._select_codex_model(task, provider_status)
                selected_reasoning_effort = self._select_codex_reasoning_effort(task, provider_status)
                await emit_alpha_terminal(
                    {
                        "stream": "system",
                        "event_type": "codex.reasoning.selected",
                        "text": f"Codex reasoning effort selected: {selected_reasoning_effort or 'auto'}",
                    }
                )
                run_result = await self.codex_runner.run(
                    paths=paths,
                    prompt=prompt,
                    model=selected_model,
                    reasoning_effort=selected_reasoning_effort,
                    sandbox=self._select_codex_sandbox(task),
                    timeout_sec=self.config.codex_timeout_sec,
                    event_callback=emit_alpha_terminal,
                    cancel_check=cancel_check,
                )
            native_session = self._record_observed_native_session(
                project=project,
                harness=active_harness,
                paths=paths,
                task=task,
                model=selected_model,
                result=run_result,
                existing=native_session,
            )

            last_result = run_result
            artifact_batch = promote_alpha_artifacts(
                task_id=task.task_id,
                paths=paths,
                text_hints=(
                    getattr(run_result, "last_message", ""),
                    getattr(run_result, "stdout", ""),
                    getattr(run_result, "stderr", ""),
                ),
            )
            all_artifacts = self._merge_artifacts(all_artifacts, artifact_batch)
            attempt_key = f"{active_harness}_{attempt_index + 1}"
            attempt_outputs[attempt_key] = run_result.as_dict()
            if fallback_from:
                attempt_outputs.setdefault(
                    "retry_from" if fallback_from.get("retrying_same_provider") else "fallback_from",
                    fallback_from,
                )

            project = self.registry.mark_task(
                project.project_id,
                task_id=task.task_id,
                session_id=task.session_id,
                local_path=str(paths.workspace),
                summary=(run_result.last_message or goal)[:500],
                status="completed" if run_result.ok else f"{active_harness}_failed",
                goal=goal[:2000],
                context_brief=context_brief,
                preferred_harness=active_harness,
                artifact_ids=[item.artifact_id for item in all_artifacts],
                repo_url=self._optional_string(task.input.get("repo_url")),
                deployment_url=self._optional_string(task.input.get("deployment_url")),
            )
            native_session = self._finalize_native_session(
                session=native_session,
                task=task,
                result=run_result,
                provider=active_harness,
            )
            await self.emit_event(
                task.task_id,
                "task.progress",
                {
                    "stage": f"alpha.{active_harness}.completed" if run_result.ok else f"alpha.{active_harness}.failed",
                    "project_id": project.project_id,
                    "returncode": run_result.returncode,
                    "timed_out": run_result.timed_out,
                    "artifact_ids": [item.artifact_id for item in all_artifacts],
                    "attempt": attempt_index + 1,
                    "native_session_id": native_session.native_session_id if native_session else None,
                },
            )

            if run_result.ok:
                if self.step_plan is not None:
                    await self.step_plan.update(5, "completed", f"{provider_label} completed the task.")
                output: dict[str, Any] = {
                    "status": "completed",
                    "project": project.as_dict(),
                    "workspace": paths.as_dict(),
                    "docker": docker_report,
                    "harness": active_harness,
                    active_harness: run_result.as_dict(),
                    "attempts": attempt_outputs,
                    "next_action": f"Review {provider_label} output and continue with the same project_ref for follow-up work.",
                }
                if native_session:
                    output["native_session"] = native_session.as_dict()
                if fallback_from:
                    output["retry_from" if fallback_from.get("retrying_same_provider") else "fallback_from"] = fallback_from
                return AgentResult(
                    status="completed",
                    output=output,
                    artifacts=all_artifacts,
                    error=None,
                )

            if attempt_index + 1 >= len(candidate_harnesses) or not self._should_try_next_harness(run_result):
                break

            next_harness = candidate_harnesses[attempt_index + 1]
            reason = self._failure_reason(active_harness, run_result)
            await self._emit_harness_fallback(
                task=task,
                project_id=project.project_id,
                workspace=str(paths.workspace),
                from_provider=active_harness,
                to_provider=next_harness,
                reason=reason,
            )
            fallback_from = {
                "provider": active_harness,
                "retrying_same_provider": next_harness == active_harness,
                "model": selected_model,
                "returncode": run_result.returncode,
                "timed_out": run_result.timed_out,
                "reason": reason,
            }

        if self.step_plan is not None:
            await self.step_plan.update(5, "failed", f"{last_provider_label} execution failed.")

        if last_result is None:
            project = self.registry.mark_task(
                project.project_id,
                task_id=task.task_id,
                session_id=task.session_id,
                local_path=str(paths.workspace),
                summary=goal[:500],
                status=f"{last_provider}_login_required",
                goal=goal[:2000],
                context_brief=context_brief,
                preferred_harness=last_provider,
                repo_url=self._optional_string(task.input.get("repo_url")),
                deployment_url=self._optional_string(task.input.get("deployment_url")),
            )
            return self._fail(
                code=f"{last_provider.upper()}_LOGIN_REQUIRED",
                message=self._provider_login_message(last_provider, last_provider_status),
                next_action="ask_user",
            )
        failure = self._cli_failure(last_provider, last_result, artifacts=all_artifacts)
        if fallback_from:
            failure.output = {
                "retry_from" if fallback_from.get("retrying_same_provider") else "fallback_from": fallback_from,
                "attempts": attempt_outputs,
            }
        return failure

    async def handle_alpha_recall_project(self, task: TaskEnvelope) -> AgentResult:
        project_query = self._optional_string(task.input.get("project_ref")) or self._optional_string(
            task.input.get("query")
        )
        limit = int(task.input.get("limit") or 5)
        candidates = self.registry.search_projects(
            project_query,
            session_id=task.session_id,
            limit=limit,
        )
        projects = [candidate.project for candidate in candidates]
        ambiguous = self._project_candidates_are_ambiguous(candidates, has_query=bool(project_query))
        selected_project = projects[0] if projects and not ambiguous else None
        if not projects:
            next_action = "No Alpha project matched. Create a new Alpha project or provide a stronger reference."
        elif ambiguous:
            next_action = "Multiple Alpha projects look plausible. Resolve with orchestrator memory or ask the user."
        else:
            next_action = f"Use project_ref={selected_project.project_id} for follow-up Alpha work."
        return AgentResult(
            status="completed",
            output={
                "query": project_query,
                "projects": [project.as_dict() for project in projects],
                "count": len(projects),
                "candidates": [candidate.as_dict() for candidate in candidates],
                "ambiguous": ambiguous,
                "selected_project": selected_project.as_dict() if selected_project else None,
                "next_action": next_action,
            },
            artifacts=[],
            error=None,
        )

    def _resolve_project(self, task: TaskEnvelope, *, mode: str) -> ProjectRecord | None:
        if mode == "new_project":
            return None
        project_ref = self._optional_string(task.input.get("project_ref"))
        if project_ref:
            project = self.registry.find_project(project_ref)
            if project is not None:
                return project
        recent = self.registry.recent_for_session(task.session_id, limit=1)
        return recent[0] if recent else None

    def _load_runtime_text(self) -> None:
        for relative in ("prompts/system.md", "prompts/policies.md", "store/learnings.md"):
            path = AGENT_ROOT / relative
            if path.exists():
                path.read_text(encoding="utf-8")

    async def _resolve_preferred_harness(self, task: TaskEnvelope) -> str:
        explicit = str(task.input.get("preferred_harness") or "").strip().lower()
        if explicit:
            return explicit
        settings = await self._fetch_alpha_settings()
        return str(settings.get("preferred_harness") or "codex").strip().lower() or "codex"

    def _candidate_harnesses(self, preferred_harness: str, task: TaskEnvelope) -> list[str]:
        normalized = str(preferred_harness or "").strip().lower()
        if normalized == "auto":
            normalized = "codex" if self._task_prefers_codex_first(task) else "cursor"
        if normalized not in {"codex", "cursor"}:
            return []
        candidates = [normalized] * self._max_same_harness_attempts(task)
        if self._allow_cross_harness_fallback(task):
            alternate = "codex" if normalized == "cursor" else "cursor"
            candidates.append(alternate)
        return candidates

    def _task_prefers_codex_first(self, task: TaskEnvelope) -> bool:
        goal = str(task.input.get("goal") or "")
        text = " ".join(
            [
                goal,
                str(task.input.get("context_brief") or ""),
                str(task.input.get("project_context") or ""),
            ]
        ).casefold()
        if len(goal) > 6000:
            return True
        broad_generation_terms = (
            "build and deploy",
            "full redesign",
            "complete redesign",
            "complete rewrite",
            "from scratch",
            "write the complete",
            "generate the complete",
            "fine-tune",
            "training",
            "train a model",
            "create a new app",
            "build a website",
            "host a website",
            "deploy",
        )
        return any(term in text for term in broad_generation_terms)

    def _max_same_harness_attempts(self, task: TaskEnvelope) -> int:
        raw = task.input.get("max_harness_attempts")
        if raw in (None, ""):
            return 2
        try:
            return min(3, max(1, int(raw)))
        except (TypeError, ValueError):
            return 2

    def _allow_cross_harness_fallback(self, task: TaskEnvelope) -> bool:
        if self._coerce_bool(task.input.get("require_preferred_harness"), default=False):
            return False
        if "allow_cross_harness_fallback" in task.input:
            return self._coerce_bool(task.input.get("allow_cross_harness_fallback"), default=False)
        if "allow_harness_fallback" in task.input:
            return self._coerce_bool(task.input.get("allow_harness_fallback"), default=False)
        return False

    async def _safe_fetch_provider_status(self, provider: str) -> dict[str, Any]:
        try:
            return await self._fetch_provider_status(provider)
        except Exception as exc:
            return {
                "status": "error",
                "login_required_reason": f"{provider}_status_lookup_failed: {str(exc)[:200]}",
                "cli": {"authenticated": False},
            }

    def _terminal_emitter(
        self,
        *,
        task: TaskEnvelope,
        project_id: str,
        workspace: str,
        provider: str,
    ) -> Any:
        async def emit_alpha_terminal(entry: dict[str, Any]) -> None:
            text = str(entry.get("text") or "").strip()
            if not text:
                return
            await self.emit_event(
                task.task_id,
                "task.progress",
                {
                    "stage": f"alpha.{provider}.terminal",
                    "project_id": project_id,
                    "workspace": workspace,
                    "codex_terminal": {
                        "id": f"alpha_terminal_{uuid4().hex}",
                        "task_id": task.task_id,
                        "provider": provider,
                        "stream": str(entry.get("stream") or "stdout"),
                        "event_type": str(entry.get("event_type") or f"{provider}.event"),
                        "text": text[:2000],
                        "detail": str(entry.get("detail") or "").strip()[:2000] or None,
                    },
                },
            )

        return emit_alpha_terminal

    async def _emit_harness_fallback(
        self,
        *,
        task: TaskEnvelope,
        project_id: str,
        workspace: str,
        from_provider: str,
        to_provider: str,
        reason: str,
    ) -> None:
        await self.emit_event(
            task.task_id,
            "task.progress",
            {
                "stage": "alpha.harness_retry" if from_provider == to_provider else "alpha.harness_fallback",
                "project_id": project_id,
                "workspace": workspace,
                "codex_terminal": {
                    "id": f"alpha_terminal_{uuid4().hex}",
                    "task_id": task.task_id,
                    "provider": to_provider,
                    "stream": "system",
                    "event_type": "alpha.harness_retry" if from_provider == to_provider else "alpha.harness_fallback",
                    "text": (
                        f"{self._harness_label(from_provider)} did not complete cleanly; retrying in the same workspace."
                        if from_provider == to_provider
                        else (
                            f"{self._harness_label(from_provider)} did not complete cleanly; "
                            f"continuing in the same workspace with {self._harness_label(to_provider)}."
                        )
                    ),
                    "detail": reason[:2000],
                },
            },
        )

    def _prompt_for_attempt(
        self,
        base_prompt: str,
        *,
        fallback_from: dict[str, Any] | None,
        previous_result: CodexRunResult | CursorRunResult | None,
    ) -> str:
        if not fallback_from:
            return base_prompt
        details = [
            "",
            "## Previous CLI Attempt",
            (
                f"{self._harness_label(str(fallback_from.get('provider') or ''))} did not complete cleanly. "
                "Continue from the current workspace and artifact directory; do not discard partial files that look useful."
            ),
            f"- reason: {fallback_from.get('reason') or ''}",
            f"- returncode: {fallback_from.get('returncode')}",
            f"- timed_out: {fallback_from.get('timed_out')}",
            "- Finish the original user goal, repair any partial work, produce the requested artifacts, and run the verification commands.",
        ]
        if previous_result is not None:
            stderr = str(getattr(previous_result, "stderr", "") or "").strip()
            last_message = str(getattr(previous_result, "last_message", "") or "").strip()
            if stderr:
                details.append(f"- stderr_tail: {stderr[-2000:]}")
            if last_message:
                details.append(f"- previous_last_message_tail: {last_message[-2000:]}")
        return base_prompt + "\n" + "\n".join(details)

    def _should_try_next_harness(self, result: CodexRunResult | CursorRunResult) -> bool:
        if getattr(result, "cancelled", False):
            return False
        return not result.ok

    def _failure_reason(self, provider: str, result: CodexRunResult | CursorRunResult) -> str:
        if getattr(result, "cancelled", False):
            return f"{self._harness_label(provider)} was cancelled."
        if getattr(result, "init_timed_out", False):
            return f"{self._harness_label(provider)} did not initialize before the init timeout."
        if getattr(result, "timed_out", False):
            return f"{self._harness_label(provider)} exceeded the execution timeout."
        if getattr(result, "model_mismatch", False):
            return f"{self._harness_label(provider)} initialized the wrong model."
        stderr = str(getattr(result, "stderr", "") or "").strip()
        stdout = str(getattr(result, "stdout", "") or "").strip()
        combined = f"{stderr}\n{stdout}"
        if getattr(result, "returncode", None) == 143 and "pkill -f" in combined and "http.server" in combined:
            return (
                f"{self._harness_label(provider)} was terminated by SIGTERM while running a broad "
                "`pkill -f` http.server cleanup command. On retry, avoid self-matching kill patterns; use "
                "`pgrep -f '[p]ython.*http.server' | xargs -r kill` or a port-specific cleanup before restart."
            )
        tail = (stderr or stdout)[-500:]
        return tail or f"{self._harness_label(provider)} exited without a clean final result."

    def _merge_artifacts(self, first: list[Any], second: list[Any]) -> list[Any]:
        merged: list[Any] = []
        seen: set[str] = set()
        for item in [*first, *second]:
            artifact_id = str(getattr(item, "artifact_id", "") or "").strip()
            key = artifact_id or str(getattr(item, "path", "") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(item)
        return merged

    async def _fetch_alpha_settings(self) -> dict[str, Any]:
        if not self.gateway_internal_token:
            return {"preferred_harness": "codex"}
        try:
            response = await self._http_client.get(
                f"{self.gateway_url.rstrip('/')}/internal/agents/alpha/config",
                headers={"Authorization": f"Bearer {self.gateway_internal_token}"},
                timeout=15.0,
            )
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else {"preferred_harness": "codex"}
        except Exception:
            return {"preferred_harness": "codex"}

    async def _fetch_provider_status(self, provider: str) -> dict[str, Any]:
        if provider == "cursor":
            return await self._fetch_cursor_status()
        return await self._fetch_codex_status()

    async def _fetch_codex_status(self) -> dict[str, Any]:
        if not self.gateway_internal_token:
            return {
                "status": "unknown",
                "login_required_reason": "gateway_internal_token_missing",
                "cli": {"authenticated": False},
            }
        response = await self._http_client.get(
            f"{self.gateway_url.rstrip('/')}/internal/agents/codex/status",
            headers={"Authorization": f"Bearer {self.gateway_internal_token}"},
            timeout=15.0,
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    async def _fetch_cursor_status(self) -> dict[str, Any]:
        if not self.gateway_internal_token:
            return {
                "status": "unknown",
                "login_required_reason": "gateway_internal_token_missing",
                "cli": {"authenticated": False},
            }
        response = await self._http_client.get(
            f"{self.gateway_url.rstrip('/')}/internal/agents/cursor/status",
            headers={"Authorization": f"Bearer {self.gateway_internal_token}"},
            timeout=15.0,
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    def _provider_status_is_ready(self, payload: dict[str, Any]) -> bool:
        cli = payload.get("cli") if isinstance(payload.get("cli"), dict) else {}
        return payload.get("status") == "authenticated" and bool(cli.get("authenticated"))

    def _provider_login_message(self, provider: str, payload: dict[str, Any]) -> str:
        label = self._harness_label(provider)
        reason = self._optional_string(payload.get("login_required_reason")) or f"{provider}_login_required"
        settings_page = "Codex" if provider == "codex" else "Cursor"
        return (
            f"{label} is not ready for Alpha execution. "
            f"Open Settings > Agents > {settings_page} and complete login. Reason: {reason}."
        )

    def _harness_label(self, provider: str) -> str:
        return "Cursor CLI" if provider == "cursor" else "Codex"

    def _harness_start_message(self, provider: str) -> str:
        if provider == "cursor":
            return "cursor-agent --print --force --trust --sandbox disabled --output-format stream-json started"
        return "codex exec --json started"

    async def _prepare_cursor_native_session(
        self,
        *,
        task: TaskEnvelope,
        project: ProjectRecord,
        paths: WorkspacePaths,
        model: str | None,
        emit: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> HarnessSessionRecord | None:
        policy = self._native_resume_policy(task)
        if policy == "disabled":
            return None

        forced_native_id = self._forced_native_session_id(task, harness="cursor")
        if forced_native_id:
            session = self.registry.record_harness_session(
                project_id=project.project_id,
                harness="cursor",
                native_session_id=forced_native_id,
                workspace_path=str(paths.workspace),
                task_id=task.task_id,
                model=model,
                status="active",
                metadata={"source": "task_input", "policy": policy},
            )
            await emit(
                {
                    "stream": "system",
                    "event_type": "cursor.native_session.resuming",
                    "text": f"Resuming Cursor chat {forced_native_id}.",
                    "detail": f"project_id={project.project_id}; session_id={session.session_id}",
                }
            )
            return session

        if policy != "fresh":
            existing = self.registry.best_harness_session(
                project.project_id,
                harness="cursor",
                workspace_path=str(paths.workspace),
                model=model,
            )
            if existing is not None:
                session = self.registry.record_harness_session(
                    project_id=project.project_id,
                    harness="cursor",
                    native_session_id=existing.native_session_id,
                    workspace_path=str(paths.workspace),
                    task_id=task.task_id,
                    model=model or existing.model,
                    status="active",
                    metadata={**existing.metadata, "source": "registry_resume", "policy": policy},
                )
                await emit(
                    {
                        "stream": "system",
                        "event_type": "cursor.native_session.resuming",
                        "text": f"Resuming Cursor chat {session.native_session_id}.",
                        "detail": f"project_id={project.project_id}; session_id={session.session_id}",
                    }
                )
                return session

        create_chat = getattr(self.cursor_runner, "create_chat", None)
        if not callable(create_chat):
            return None
        try:
            created_chat_id = await create_chat(paths=paths)
        except Exception as exc:
            await emit(
                {
                    "stream": "system",
                    "event_type": "cursor.native_session.create_failed",
                    "text": "Could not create a Cursor chat; continuing with a fresh headless run.",
                    "detail": str(exc)[:500],
                }
            )
            return None
        if not created_chat_id:
            await emit(
                {
                    "stream": "system",
                    "event_type": "cursor.native_session.unavailable",
                    "text": "Cursor native chat id unavailable; continuing with a fresh headless run.",
                }
            )
            return None
        session = self.registry.record_harness_session(
            project_id=project.project_id,
            harness="cursor",
            native_session_id=created_chat_id,
            workspace_path=str(paths.workspace),
            task_id=task.task_id,
            model=model,
            status="active",
            metadata={"source": "cursor.create-chat", "policy": policy},
        )
        await emit(
            {
                "stream": "system",
                "event_type": "cursor.native_session.created",
                "text": f"Created Cursor chat {created_chat_id}.",
                "detail": f"project_id={project.project_id}; session_id={session.session_id}",
            }
        )
        return session

    def _record_observed_native_session(
        self,
        *,
        project: ProjectRecord,
        harness: str,
        paths: WorkspacePaths,
        task: TaskEnvelope,
        model: str | None,
        result: CodexRunResult | CursorRunResult,
        existing: HarnessSessionRecord | None,
    ) -> HarnessSessionRecord | None:
        observed = self._optional_string(getattr(result, "native_session_id", None))
        if not observed:
            return existing
        metadata = {
            "source": "cli_observed",
            "resume_used": bool(getattr(result, "resume_used", False)),
            "resume_session_id": self._optional_string(getattr(result, "resume_session_id", None)) or "",
        }
        if existing is not None:
            metadata = {**existing.metadata, **metadata}
        return self.registry.record_harness_session(
            project_id=project.project_id,
            harness=harness,
            native_session_id=observed,
            workspace_path=str(paths.workspace),
            task_id=task.task_id,
            model=model,
            status="active",
            metadata=metadata,
        )

    def _finalize_native_session(
        self,
        *,
        session: HarnessSessionRecord | None,
        task: TaskEnvelope,
        result: CodexRunResult | CursorRunResult,
        provider: str,
    ) -> HarnessSessionRecord | None:
        if session is None:
            return None
        if result.ok:
            return session
        reason = self._failure_reason(provider, result)
        return self.registry.mark_harness_session_failed(
            session.session_id,
            task_id=task.task_id,
            reason=reason,
        )

    def _native_resume_policy(self, task: TaskEnvelope) -> str:
        if self._coerce_bool(task.input.get("fresh_native_session"), default=False):
            return "fresh"
        if self._coerce_bool(task.input.get("disable_native_resume"), default=False):
            return "disabled"
        raw = self._optional_string(
            task.input.get("native_resume_policy")
            or task.input.get("harness_session_policy")
            or task.input.get("native_session_policy")
        )
        normalized = (raw or "auto").lower().replace("-", "_")
        if normalized in {"off", "none", "disabled", "disable", "no_resume"}:
            return "disabled"
        if normalized in {"fresh", "new", "new_session", "fresh_session"}:
            return "fresh"
        if normalized in {"force", "required", "resume_required"}:
            return "force"
        return "auto"

    def _forced_native_session_id(self, task: TaskEnvelope, *, harness: str) -> str | None:
        keys = ["native_session_id", "harness_native_session_id"]
        if harness == "cursor":
            keys.extend(["cursor_chat_id", "chat_id"])
        elif harness == "codex":
            keys.extend(["codex_session_id"])
        for key in keys:
            value = self._optional_string(task.input.get(key))
            if value:
                return value
        return None

    def _select_codex_model(self, task: TaskEnvelope, codex_status: dict[str, Any]) -> str | None:
        for value in (
            task.input.get("model"),
            task.input.get("preferred_model"),
            codex_status.get("preferred_model"),
            self.config.codex_default_model,
        ):
            normalized = self._optional_string(value)
            if normalized and normalized.lower() != "auto":
                return normalized
        return None

    def _select_codex_reasoning_effort(
        self,
        task: TaskEnvelope,
        codex_status: dict[str, Any],
    ) -> str | None:
        for value in (
            task.input.get("reasoning_effort"),
            task.input.get("codex_reasoning_effort"),
            codex_status.get("reasoning_effort"),
        ):
            normalized = self._optional_string(value)
            if normalized and normalized.lower() != "auto":
                lowered = normalized.lower()
                if lowered in {"low", "medium", "high", "xhigh"}:
                    return lowered
        return None

    def _select_cursor_model(self, task: TaskEnvelope, cursor_status: dict[str, Any]) -> str | None:
        for value in (
            task.input.get("model"),
            task.input.get("preferred_model"),
            cursor_status.get("preferred_model"),
            self.config.cursor_default_model,
        ):
            normalized = self._optional_string(value)
            if normalized and normalized.lower() != "auto":
                return normalized
        return None

    def _select_codex_sandbox(self, task: TaskEnvelope) -> str:
        normalized = str(task.input.get("codex_sandbox") or self.config.codex_sandbox).strip()
        if normalized in {"read-only", "workspace-write", "danger-full-access"}:
            return normalized
        return self.config.codex_sandbox

    def _build_cli_prompt(
        self,
        *,
        task: TaskEnvelope,
        project: ProjectRecord,
        workspace: str,
        artifacts_dir: str,
        staged_inputs: list[dict[str, str]] | None = None,
    ) -> str:
        deliverables = self._coerce_string_list(task.input.get("deliverables"))
        constraints = task.input.get("constraints") if isinstance(task.input.get("constraints"), dict) else {}
        goal = str(task.input.get("goal") or "").strip()
        goal_reference = self._externalize_large_goal(goal, artifacts_dir=artifacts_dir)
        goal_for_prompt = goal
        if goal_reference:
            goal_for_prompt = (
                goal[:4000].rstrip()
                + "\n\n[The full user goal/context was too large for safe CLI argv transport. "
                + f"Read the full content from: {goal_reference}]"
            )
        lines = [
            "You are the COSMIC Alpha Agent execution harness.",
            "",
            "Treat the COSMIC orchestrator as the human operator. Complete the user's high-level task end to end inside the provided workspace. Ask for clarification in your final message when the task cannot be completed safely or needs missing credentials.",
            "",
            "## User Goal",
            goal_for_prompt,
            "",
            "## Project Context",
            f"- project_id: {project.project_id}",
            f"- workspace: {workspace}",
            f"- repo_url: {project.repo_url or self._optional_string(task.input.get('repo_url')) or ''}",
            f"- deployment_url: {project.deployment_url or self._optional_string(task.input.get('deployment_url')) or ''}",
            "",
            "## Operating Rules",
            "- Work only inside the workspace unless the task explicitly requires external setup.",
            "- Do not alter the COSMIC production repo or services unless the user goal explicitly asks for that.",
            f"- Put every user-facing deliverable file in this artifact directory: {artifacts_dir}",
            "- If you create a deliverable elsewhere in the workspace, mention its absolute path in your final report.",
            "- Prefer small, verifiable changes and run relevant checks when the project provides them.",
            "- Leave a concise final report with what changed, where it is, checks run, and any blocker.",
        ]
        if deliverables:
            lines.extend(["", "## Requested Deliverables"])
            lines.extend(f"- {item}" for item in deliverables)
        if staged_inputs:
            lines.extend(
                [
                    "",
                    "## Input Artifacts",
                    "The orchestrator passed files and large context by reference. Inspect these staged files directly instead of asking for pasted content.",
                    "Parsed document bundles are staged as concrete files such as document.md, chunk_index.json, document.json, and manifest.json when available.",
                    "Uploaded archives are already unpacked: an entry marked staged_kind=directory is a real directory tree, not a zip to extract. Its mime describes the original upload.",
                ]
            )
            for index, item in enumerate(staged_inputs, 1):
                details = [
                    f"path={item.get('staged_path') or item.get('path') or ''}",
                    f"staged_kind={item.get('staged_kind') or ''}",
                    f"file_count={item.get('file_count') or ''}",
                    f"staging_error={item.get('staging_error') or ''}",
                    f"mime={item.get('mime') or ''}",
                    f"artifact_id={item.get('artifact_id') or ''}",
                    f"parse_bundle_id={item.get('parse_bundle_id') or ''}",
                    f"doc_id={item.get('doc_id') or ''}",
                    f"bundle_path_key={item.get('bundle_path_key') or ''}",
                    f"source_artifact_id={item.get('source_artifact_id') or ''}",
                ]
                details = [part for part in details if not part.endswith("=")]
                lines.append(f"{index}. " + "; ".join(details))
        if constraints:
            lines.extend(["", "## Constraints"])
            lines.extend(f"- {key}: {value}" for key, value in sorted(constraints.items()))
        return "\n".join(lines)

    def _externalize_large_goal(self, goal: str, *, artifacts_dir: str) -> str | None:
        if len(goal) <= 8000:
            return None
        try:
            path = Path(artifacts_dir) / "alpha-full-goal.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(goal, encoding="utf-8")
            return str(path)
        except OSError:
            return None

    def _stage_input_artifacts(self, task: TaskEnvelope, *, workspace: Path) -> list[dict[str, str]]:
        raw_artifacts = task.input_artifacts if isinstance(task.input_artifacts, list) else []
        if not raw_artifacts:
            return []
        input_dir = workspace / "_cosmic_inputs"
        summaries: list[dict[str, str]] = []
        for index, artifact in enumerate(raw_artifacts, 1):
            if not isinstance(artifact, dict):
                continue
            artifact_id = self._optional_string(artifact.get("artifact_id")) or f"input_{index}"
            filename = self._safe_filename(
                self._optional_string(artifact.get("filename"))
                or Path(str(artifact.get("path") or "")).name
                or f"{artifact_id}.bin"
            )
            summary = {
                "artifact_id": artifact_id,
                "filename": filename,
                "mime": self._optional_string(artifact.get("mime") or artifact.get("mime_type")) or "",
                "path": self._optional_string(artifact.get("path")) or "",
                "parse_bundle_id": self._optional_string(artifact.get("parse_bundle_id")) or "",
                "doc_id": self._parsed_doc_id(artifact) or "",
                "bundle_path_key": self._optional_string(artifact.get("bundle_path_key")) or "",
                "source_artifact_id": self._optional_string(artifact.get("source_artifact_id")) or "",
            }
            source_path = self._artifact_source_path(artifact)
            if source_path is not None and source_path.is_file():
                if is_supported_bundle_artifact(artifact):
                    # An archive is only useful here once it is a tree. Unpacked
                    # at the moment of staging, in the process that owns the
                    # workspace, through the shared extractor that re-validates
                    # every entry -- the harness never sees a zip to unpack, and
                    # never has to be trusted to unpack one safely.
                    stem = Path(filename).stem or f"bundle_{index}"
                    target_dir = input_dir / f"{index:02d}_{self._safe_filename(stem)}"
                    try:
                        input_dir.mkdir(parents=True, exist_ok=True)
                        written = safe_extract_zip(source_path, target_dir)
                        summary["staged_path"] = str(target_dir)
                        summary["staged_kind"] = "directory"
                        summary["file_count"] = str(len(written))
                    except (ArchiveRejected, OSError) as exc:
                        shutil.rmtree(target_dir, ignore_errors=True)
                        summary["staged_path"] = ""
                        summary["staging_error"] = str(exc)
                else:
                    try:
                        input_dir.mkdir(parents=True, exist_ok=True)
                        target = input_dir / f"{index:02d}_{filename}"
                        if source_path.resolve() != target.resolve():
                            shutil.copy2(source_path, target)
                        summary["staged_path"] = str(target)
                        summary["staged_kind"] = "file"
                    except OSError:
                        summary["staged_path"] = ""
            summaries.append(summary)
        return summaries

    def _artifact_source_path(self, artifact: dict[str, Any]) -> Path | None:
        raw_path = self._optional_string(artifact.get("path"))
        if not raw_path:
            return None
        candidate = Path(raw_path).expanduser()
        backend_root = AGENT_ROOT.parent.parent
        artifacts_root = backend_root / "runs" / "artifacts"
        candidates = [candidate]
        if not candidate.is_absolute():
            candidates.extend(
                [
                    Path.cwd() / candidate,
                    backend_root / candidate,
                    artifacts_root / candidate,
                ]
            )
            parts = candidate.parts
            if len(parts) >= 3 and parts[0] == "runs" and parts[1] == "artifacts":
                candidates.append(artifacts_root / Path(*parts[2:]))
        for item in candidates:
            try:
                resolved = item.resolve()
            except OSError:
                continue
            if resolved.is_file():
                return resolved
        return None

    def _parsed_doc_id(self, artifact: dict[str, Any]) -> str | None:
        parsed_summary = artifact.get("parsed_summary") if isinstance(artifact.get("parsed_summary"), dict) else {}
        return self._optional_string(artifact.get("doc_id")) or self._optional_string(parsed_summary.get("doc_id"))

    def _safe_filename(self, value: str) -> str:
        normalized = "".join(ch if ch.isalnum() or ch in {".", "-", "_"} else "_" for ch in value.strip())
        return normalized[:160].strip("._") or "artifact.bin"

    def _cli_failure(
        self,
        provider: str,
        result: CodexRunResult | CursorRunResult,
        *,
        artifacts: list[Any] | None = None,
    ) -> AgentResult:
        if getattr(result, "cancelled", False):
            code = "CANCELLED"
        elif getattr(result, "init_timed_out", False):
            code = "CLI_INIT_TIMEOUT"
        else:
            code = "TIMEOUT" if result.timed_out else "CODEX_EXECUTION_FAILED"
        if provider == "cursor" and code != "TIMEOUT":
            code = "CURSOR_EXECUTION_FAILED"
        if getattr(result, "cancelled", False):
            code = "CANCELLED"
        elif getattr(result, "init_timed_out", False):
            code = "CURSOR_INIT_TIMEOUT" if provider == "cursor" else "CLI_INIT_TIMEOUT"
        fallback = f"{self._harness_label(provider)} execution failed without output."
        stderr = result.stderr.strip() or result.stdout.strip() or fallback
        return AgentResult(
            status="failed",
            output={},
            artifacts=artifacts or [],
            error=AgentError(
                code=code,
                retryable=code in {"TIMEOUT", "CURSOR_INIT_TIMEOUT", "CLI_INIT_TIMEOUT", "DOCKER_UNAVAILABLE", "WORKSPACE_BUSY"},
                message=stderr[-1000:],
                next_action="skip" if code == "CANCELLED" else "retry" if result.timed_out else "escalate",
            ),
        )

    def _optional_string(self, value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    def _coerce_bool(self, value: Any, *, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
        return default

    def _coerce_string_list(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    def _task_context_brief(self, task: TaskEnvelope) -> str | None:
        parts: list[str] = []
        for key in ("context_brief", "project_context", "context"):
            value = task.input.get(key)
            if isinstance(value, dict):
                rendered = "; ".join(
                    f"{item_key}: {item_value}"
                    for item_key, item_value in sorted(value.items())
                    if str(item_value).strip()
                )
            elif isinstance(value, list):
                rendered = "; ".join(str(item).strip() for item in value if str(item).strip())
            else:
                rendered = str(value or "").strip()
            if rendered:
                parts.append(f"{key}: {rendered}")

        deliverables = self._coerce_string_list(task.input.get("deliverables"))
        if deliverables:
            parts.append("deliverables: " + "; ".join(deliverables))

        constraints = task.input.get("constraints")
        if isinstance(constraints, dict):
            rendered_constraints = "; ".join(
                f"{key}: {value}" for key, value in sorted(constraints.items()) if str(value).strip()
            )
            if rendered_constraints:
                parts.append("constraints: " + rendered_constraints)

        brief = "\n".join(parts).strip()
        return brief[:2000] or None

    def _project_candidates_are_ambiguous(
        self,
        candidates: list[ProjectCandidate],
        *,
        has_query: bool,
    ) -> bool:
        if not has_query or len(candidates) < 2:
            return False
        top = candidates[0]
        runner_up = candidates[1]
        if top.score <= 0:
            return False
        if top.match_type == "exact" and top.score >= 900 and (top.score - runner_up.score) > 100:
            return False
        return runner_up.score >= top.score * 0.88 or (top.score - runner_up.score) <= 35

    def _fail(self, *, code: str, message: str, next_action: str) -> AgentResult:
        return AgentResult(
            status="failed",
            output={},
            artifacts=[],
            error=AgentError(
                code=code,
                retryable=code in {"TIMEOUT", "DOCKER_UNAVAILABLE", "WORKSPACE_BUSY"},
                message=message,
                next_action=next_action,
            ),
        )
