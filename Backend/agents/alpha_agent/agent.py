from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

from shared.agent_runtime import AgentRuntime
from shared.contracts import AgentError, AgentResult, TaskEnvelope

from .codex_runner import CodexRunResult, CodexWorkspaceRunner
from .config import AGENT_ROOT, AlphaAgentConfig
from .docker_runner import DockerWorkspaceRunner
from .project_registry import ProjectRecord, ProjectRegistry
from .workspace_manager import WorkspaceManager

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
        )
        self.docker_runner = DockerWorkspaceRunner(self.config)
        self.codex_runner = CodexWorkspaceRunner(self.config)
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
                    "Verify Codex auth and execution readiness",
                    "Execute task with Codex",
                ]
            )

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
                    "codex": {"skipped": True, "reason": "prepare_only"},
                    "next_action": "Call alpha.execute again without prepare_only to execute with Codex.",
                },
                artifacts=[],
                error=None,
            )

        preferred_harness = str(task.input.get("preferred_harness") or "codex").strip().lower()
        if preferred_harness not in {"auto", "codex"}:
            return self._fail(
                code="UNSUPPORTED_OPERATION",
                message="Alpha currently supports Codex execution. OpenCode and Cursor harnesses are planned but not wired.",
                next_action="ask_user",
            )

        codex_status = await self._fetch_codex_status()
        codex_ready = self._codex_status_is_ready(codex_status)
        if self.step_plan is not None:
            await self.step_plan.update(
                4,
                "completed" if codex_ready else "failed",
                "Codex is authenticated." if codex_ready else "Codex is not authenticated.",
            )
        if not codex_ready:
            project = self.registry.mark_task(
                project.project_id,
                task_id=task.task_id,
                session_id=task.session_id,
                local_path=str(paths.workspace),
                summary=goal[:500],
                status="codex_login_required",
            )
            return self._fail(
                code="CODEX_LOGIN_REQUIRED",
                message=self._codex_login_message(codex_status),
                next_action="ask_user",
            )

        await self.emit_event(
            task.task_id,
            "task.progress",
            {
                "stage": "alpha.codex.started",
                "project_id": project.project_id,
                "workspace": str(paths.workspace),
            },
        )

        async def emit_codex_terminal(entry: dict[str, Any]) -> None:
            text = str(entry.get("text") or "").strip()
            if not text:
                return
            await self.emit_event(
                task.task_id,
                "task.progress",
                {
                    "stage": "alpha.codex.terminal",
                    "project_id": project.project_id,
                    "workspace": str(paths.workspace),
                    "codex_terminal": {
                        "id": f"alpha_terminal_{uuid4().hex}",
                        "task_id": task.task_id,
                        "stream": str(entry.get("stream") or "stdout"),
                        "event_type": str(entry.get("event_type") or "codex.event"),
                        "text": text[:2000],
                        "detail": str(entry.get("detail") or "").strip()[:2000] or None,
                    },
                },
            )

        await emit_codex_terminal(
            {
                "stream": "system",
                "event_type": "codex.exec.started",
                "text": "codex exec --json started",
                "detail": str(paths.workspace),
            }
        )
        codex_result = await self.codex_runner.run(
            paths=paths,
            prompt=self._build_codex_prompt(task=task, project=project, workspace=str(paths.workspace)),
            model=self._select_codex_model(task, codex_status),
            sandbox=self._select_codex_sandbox(task),
            timeout_sec=self.config.codex_timeout_sec,
            event_callback=emit_codex_terminal,
        )
        artifact = self.codex_runner.artifact_for_last_message(task_id=task.task_id, result=codex_result)
        artifacts = [artifact] if artifact is not None else []
        project = self.registry.mark_task(
            project.project_id,
            task_id=task.task_id,
            session_id=task.session_id,
            local_path=str(paths.workspace),
            summary=(codex_result.last_message or goal)[:500],
            status="completed" if codex_result.ok else "codex_failed",
        )
        await self.emit_event(
            task.task_id,
            "task.progress",
            {
                "stage": "alpha.codex.completed" if codex_result.ok else "alpha.codex.failed",
                "project_id": project.project_id,
                "returncode": codex_result.returncode,
                "timed_out": codex_result.timed_out,
                "artifact_ids": [item.artifact_id for item in artifacts],
            },
        )
        if self.step_plan is not None:
            await self.step_plan.update(
                5,
                "completed" if codex_result.ok else "failed",
                "Codex completed the task." if codex_result.ok else "Codex execution failed.",
            )

        if not codex_result.ok:
            return self._codex_failure(codex_result)

        return AgentResult(
            status="completed",
            output={
                "status": "completed",
                "project": project.as_dict(),
                "workspace": paths.as_dict(),
                "docker": docker_report,
                "codex": codex_result.as_dict(),
                "next_action": "Review Codex output and continue with the same project_ref for follow-up work.",
            },
            artifacts=artifacts,
            error=None,
        )

    async def handle_alpha_recall_project(self, task: TaskEnvelope) -> AgentResult:
        project_ref = self._optional_string(task.input.get("project_ref"))
        limit = int(task.input.get("limit") or 5)
        projects: list[ProjectRecord] = []
        if project_ref:
            project = self.registry.find_project(project_ref)
            if project is not None:
                projects = [project]
        if not projects:
            projects = self.registry.recent_for_session(task.session_id, limit=limit)
        return AgentResult(
            status="completed",
            output={
                "projects": [project.as_dict() for project in projects],
                "count": len(projects),
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

    def _codex_status_is_ready(self, payload: dict[str, Any]) -> bool:
        cli = payload.get("cli") if isinstance(payload.get("cli"), dict) else {}
        return payload.get("status") == "authenticated" and bool(cli.get("authenticated"))

    def _codex_login_message(self, payload: dict[str, Any]) -> str:
        reason = self._optional_string(payload.get("login_required_reason")) or "codex_login_required"
        return (
            "Codex is not ready for Alpha execution. "
            f"Open Settings > Agents > Codex and complete login or API-key sync. Reason: {reason}."
        )

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

    def _select_codex_sandbox(self, task: TaskEnvelope) -> str:
        normalized = str(task.input.get("codex_sandbox") or self.config.codex_sandbox).strip()
        if normalized in {"read-only", "workspace-write", "danger-full-access"}:
            return normalized
        return self.config.codex_sandbox

    def _build_codex_prompt(
        self,
        *,
        task: TaskEnvelope,
        project: ProjectRecord,
        workspace: str,
    ) -> str:
        deliverables = self._coerce_string_list(task.input.get("deliverables"))
        constraints = task.input.get("constraints") if isinstance(task.input.get("constraints"), dict) else {}
        lines = [
            "You are the COSMIC Alpha Agent execution harness.",
            "",
            "Treat the COSMIC orchestrator as the human operator. Complete the user's high-level task end to end inside the provided workspace. Ask for clarification in your final message when the task cannot be completed safely or needs missing credentials.",
            "",
            "## User Goal",
            str(task.input.get("goal") or "").strip(),
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
            "- Prefer small, verifiable changes and run relevant checks when the project provides them.",
            "- Leave a concise final report with what changed, where it is, checks run, and any blocker.",
        ]
        if deliverables:
            lines.extend(["", "## Requested Deliverables"])
            lines.extend(f"- {item}" for item in deliverables)
        if constraints:
            lines.extend(["", "## Constraints"])
            lines.extend(f"- {key}: {value}" for key, value in sorted(constraints.items()))
        return "\n".join(lines)

    def _codex_failure(self, result: CodexRunResult) -> AgentResult:
        code = "TIMEOUT" if result.timed_out else "CODEX_EXECUTION_FAILED"
        stderr = result.stderr.strip() or result.stdout.strip() or "Codex execution failed without output."
        return self._fail(
            code=code,
            message=stderr[-1000:],
            next_action="retry" if result.timed_out else "escalate",
        )

    def _optional_string(self, value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    def _coerce_string_list(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

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
