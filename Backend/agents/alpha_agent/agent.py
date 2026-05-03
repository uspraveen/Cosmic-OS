from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from shared.agent_runtime import AgentRuntime
from shared.contracts import AgentError, AgentResult, TaskEnvelope

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
        self.workspace_manager = WorkspaceManager(self.config.alpha_root)
        self.docker_runner = DockerWorkspaceRunner(self.config)
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
                    "Return V1 workspace preparation report",
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
            await self.step_plan.update(4, "completed", "Returned V1 workspace preparation report.")

        return AgentResult(
            status="completed",
            output={
                "status": "workspace_prepared",
                "v1_scope": "workspace_prepare_only",
                "project": project.as_dict(),
                "workspace": paths.as_dict(),
                "docker": docker_report,
                "next_action": "Implement Codex/OpenCode/Cursor harness before executing project goals.",
            },
            artifacts=[],
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

