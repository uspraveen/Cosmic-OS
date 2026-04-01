"""Diagram Agent — Mermaid, D2, Excalidraw specialist for COSMIC.

Handles diagram.create, diagram.modify, diagram.recall_session.

LangGraph workflow (when enabled): analyze -> render -> finalize
Direct handler fallback: same logic without graph state management.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from shared.agent_runtime import AgentRuntime
from shared.contracts import (
    AgentError,
    AgentResult,
    ArtifactManifest,
    TaskEnvelope,
    utcnow,
)
from shared.sqlite_client import connect_sync

from .config import AGENT_ROOT, BACKEND_ROOT, DiagramAgentConfig
from .internal_llm import (
    analyze_diagram_request,
    modify_diagram,
    regenerate_diagram_with_feedback,
    validate_diagram_render,
)
from .renderers import (
    RenderError,
    explain_render_error,
    render_d2,
    render_excalidraw,
    render_mermaid,
)
from .skills import discover_skills

logger = logging.getLogger(__name__)

_DIAGRAM_SESSIONS_SQL = """
CREATE TABLE IF NOT EXISTS diagram_sessions (
    session_id TEXT,
    task_id TEXT,
    intent TEXT NOT NULL,
    renderer TEXT,
    diagram_type TEXT,
    title TEXT,
    definition_preview TEXT,
    rendered_ok INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_diagram_sessions_session_created
ON diagram_sessions (session_id, created_at DESC);
"""


class DiagramAgent(AgentRuntime):
    """Diagram specialist agent."""

    def __init__(self, redis_client, config: DiagramAgentConfig | None = None):
        self._cfg = config or DiagramAgentConfig.from_env()
        super().__init__(
            agent_card_path=str(AGENT_ROOT / "agent_card.yaml"),
            redis_client=redis_client,
        )
        self.prompts_dir = AGENT_ROOT / "prompts"
        self.learnings_path = AGENT_ROOT / "store" / "learnings.md"
        self.system_prompt: str | None = None
        self.policies: str | None = None
        self.learnings: str | None = None
        self.db = None
        self._http_client: httpx.AsyncClient | None = None
        self.artifacts_root = self._cfg.artifacts_root.resolve()
        self.agent_id = "cosmic/diagram-agent:1.0.0"

    async def on_startup(self):
        """Initialize databases and ensure directories exist."""
        self.learnings_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.learnings_path.exists():
            self.learnings_path.write_text("# Diagram Agent — Learnings\n")

        data_dir = AGENT_ROOT / "store" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db = connect_sync(str(data_dir / "diagram_sessions.db"))
        self.db.executescript(_DIAGRAM_SESSIONS_SQL)
        self.db.commit()

        runtime_dir = AGENT_ROOT / "runtime"
        (runtime_dir / "cache").mkdir(parents=True, exist_ok=True)
        (runtime_dir / "logs").mkdir(parents=True, exist_ok=True)

    def _load_task_context(self):
        """Reload prompt and learnings at task start."""
        self.system_prompt = (self.prompts_dir / "system.md").read_text()
        self.policies = (self.prompts_dir / "policies.md").read_text()
        self.learnings = self.learnings_path.read_text()

    def _http(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=30, http2=False)
        return self._http_client

    def _task_artifact_dir(self, task_id: str) -> Path:
        """Per-task artifact directory: runs/artifacts/<task_id>/diagram_agent/"""
        return self.artifacts_root / task_id / "diagram_agent"

    def _artifact_manifest(
        self,
        *,
        task_id: str,
        path: Path,
        mime: str,
        kind: str = "output",
        audience: str = "deliverable",
    ) -> ArtifactManifest:
        """Create ArtifactManifest matching the shared contract."""
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(self.artifacts_root)
            logical_path = (Path("runs") / "artifacts" / relative).as_posix()
        except ValueError:
            logical_path = resolved.as_posix()
        return ArtifactManifest(
            artifact_id=f"art_{uuid4().hex[:12]}",
            task_id=task_id,
            mime=mime,
            sha256=digest,
            path=logical_path,
            created_by_agent=self.agent_id,
            kind=kind,
            audience=audience,
        )

    async def execute(self, task: TaskEnvelope) -> AgentResult:
        """Core execution. Dispatches via LangGraph or direct handler."""
        self._load_task_context()

        if task.intent != "diagram.recall_session" and self._cfg.diagram_use_langgraph:
            try:
                from .diagram_graph import run_diagram_langgraph
            except ImportError as exc:
                logger.warning("diagram.langgraph_unavailable: %s", exc)
            else:
                return await run_diagram_langgraph(agent=self, task=task)

        # Fallback: direct handler dispatch
        handler_name = f"handle_{task.intent.replace('.', '_')}"
        handler = getattr(self, handler_name, None)
        if not handler:
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="INVALID_INPUT",
                    retryable=False,
                    message=f"Unknown intent: {task.intent}",
                    next_action="escalate",
                ),
            )

        try:
            return await handler(task)
        except Exception as exc:
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="INTERNAL_ERROR",
                    retryable=False,
                    message=str(exc),
                    next_action="escalate",
                ),
            )

    # ── Direct Handlers (fallback when LangGraph unavailable) ─────────

    @staticmethod
    def _normalize_output_format(value: str | None) -> str:
        normalized = str(value or "").strip().lower()
        return "png" if normalized == "png" else "svg"

    async def handle_diagram_create(self, task: TaskEnvelope) -> AgentResult:
        """Direct handler for diagram.create."""
        desc = task.input.get("description", "") or task.input.get("query", "")
        if not desc:
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="INVALID_INPUT",
                    retryable=False,
                    message="diagram.create requires a 'description' field.",
                    next_action="escalate",
                ),
            )

        preferred = task.input.get("preferred_renderer")
        output_format = self._normalize_output_format(task.input.get("output_format"))

        # Skills context for LLM
        skills = discover_skills()
        from .skills import build_skills_context

        skills_ctx = build_skills_context(skills)

        # Analyze with LLM
        llm_result = await analyze_diagram_request(
            cfg=self._cfg,
            http_client=self._http(),
            description=desc,
            preferred_renderer=preferred,
            skills_context=skills_ctx,
            task_id=task.task_id,
            session_id=task.session_id,
            source=task.source,
            source_id=task.source_id,
            channel=task.channel,
        )

        if not llm_result:
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="INTERNAL_ERROR",
                    retryable=True,
                    message="Internal LLM failed to analyze the diagram request.",
                    next_action="retry",
                ),
            )

        renderer = llm_result.get("renderer", "mermaid")
        definition = llm_result.get("definition", "")
        diagram_type = llm_result.get("diagram_type", "other")
        title = llm_result.get("reasoning", "")[:100]

        return await self._render_and_build_result(
            task=task,
            renderer=renderer,
            definition=definition,
            diagram_type=diagram_type,
            title=title,
            output_format=output_format,
        )

    async def handle_diagram_modify(self, task: TaskEnvelope) -> AgentResult:
        """Direct handler for diagram.modify."""
        existing = task.input.get("existing_definition", "")
        renderer = task.input.get("renderer", "mermaid")
        mod_request = task.input.get("modification_request", "")

        if not existing or not mod_request:
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="INVALID_INPUT",
                    retryable=False,
                    message="diagram.modify requires 'existing_definition' and 'modification_request'.",
                    next_action="escalate",
                ),
            )

        # Skills context for LLM
        skills = discover_skills()
        from .skills import build_skills_context

        skills_ctx = build_skills_context(skills)

        llm_result = await modify_diagram(
            cfg=self._cfg,
            http_client=self._http(),
            renderer=renderer,
            existing_definition=existing,
            modification_request=mod_request,
            skills_context=skills_ctx,
            task_id=task.task_id,
            session_id=task.session_id,
            source=task.source,
            source_id=task.source_id,
            channel=task.channel,
        )

        if not llm_result:
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="INTERNAL_ERROR",
                    retryable=True,
                    message="Internal LLM failed to modify the diagram.",
                    next_action="retry",
                ),
            )

        return await self._render_and_build_result(
            task=task,
            renderer=llm_result.get("renderer", renderer),
            definition=llm_result.get("definition", ""),
            diagram_type="modified",
            title=f"Modified: {llm_result.get('changes', '')[:80]}",
            output_format=self._normalize_output_format(
                task.input.get("output_format")
            ),
        )

    async def handle_diagram_recall_session(self, task: TaskEnvelope) -> AgentResult:
        """Recall prior diagram operations from the session ledger."""
        session_id = task.input.get("session_id")
        limit = task.input.get("limit", 10)

        rows = self.db.execute(
            """SELECT * FROM diagram_sessions
               WHERE session_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            [session_id, limit],
        ).fetchall()

        return AgentResult(
            status="completed",
            output={
                "history": [dict(r) for r in rows],
                "count": len(rows),
            },
            artifacts=[],
            error=None,
        )

    # ── Shared rendering helper ───────────────────────────────────────

    async def _render_and_build_result(
        self,
        *,
        task: TaskEnvelope,
        renderer: str,
        definition: str,
        diagram_type: str,
        title: str,
        output_format: str,
    ) -> AgentResult:
        """Render a definition and build the AgentResult with artifacts.

        Files are written to runs/artifacts/<task_id>/diagram_agent/.
        ArtifactManifest matches the shared contract: artifact_id, task_id, mime,
        sha256, path, created_by_agent, kind, audience.
        """
        if not definition:
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="INTERNAL_ERROR",
                    retryable=False,
                    message="No diagram definition was generated.",
                    next_action="escalate",
                ),
            )

        task_artifact_dir = self._task_artifact_dir(task.task_id)
        task_artifact_dir.mkdir(parents=True, exist_ok=True)

        render_ok = False
        render_error = ""
        content = b""

        try:
            if renderer == "mermaid":
                ext = "png" if output_format == "png" else "svg"
                out_path = task_artifact_dir / f"diagram.{ext}"
                result = await render_mermaid(
                    definition,
                    mmdc_path=self._cfg.mmdc_path,
                    output_format=output_format,
                    background=self._cfg.mermaid_background,
                    theme=self._cfg.default_theme,
                    disable_sandbox=self._cfg.mermaid_disable_sandbox,
                    output_path=out_path,
                )
            elif renderer == "d2":
                ext = "png" if output_format == "png" else "svg"
                out_path = task_artifact_dir / f"diagram.{ext}"
                result = await render_d2(
                    definition,
                    d2_path=self._cfg.d2_path,
                    output_format=output_format,
                    sketch=self._cfg.d2_sketch,
                    pad=self._cfg.d2_pad,
                    output_path=out_path,
                )
            elif renderer == "excalidraw":
                out_path = task_artifact_dir / "diagram.excalidraw"
                result = render_excalidraw(definition, output_path=out_path)
            else:
                raise RenderError(renderer, f"Unknown renderer: {renderer}")

            render_ok = True
            content = result.get("content", b"")
        except RenderError as exc:
            render_error = explain_render_error(exc)
            logger.warning("diagram.render_failed: %s", render_error)

        # ── Visual validation loop ────────────────────────────────────
        validation_pass = True
        validation_issues: list[str] = []
        validation_suggestion = ""
        max_validation_attempts = 2

        if render_ok and content and output_format == "png":
            png_for_vision = content
        elif render_ok and content and output_format == "svg":
            png_for_vision = None  # Can't do vision on SVG
        else:
            png_for_vision = None

        if render_ok and self._cfg.enable_internal_llm:
            for attempt in range(1, max_validation_attempts + 1):
                validation = await validate_diagram_render(
                    cfg=self._cfg,
                    http_client=self._http(),
                    renderer=renderer,
                    definition=definition,
                    diagram_type=diagram_type,
                    png_bytes=png_for_vision,
                    task_id=task.task_id,
                    session_id=task.session_id,
                    source=task.source,
                    source_id=task.source_id,
                    channel=task.channel,
                )
                if validation is None:
                    break  # Can't validate — accept
                if bool(validation.get("pass", True)):
                    validation_pass = True
                    break
                # Validation failed
                validation_pass = False
                validation_issues = validation.get("issues", [])
                validation_suggestion = str(validation.get("suggestion", ""))
                if attempt >= max_validation_attempts:
                    logger.warning(
                        "diagram.validation_failed_max_attempts: issues=%s",
                        validation_issues,
                    )
                    break
                # Re-generate: send validation feedback to LLM for corrected definition
                regen_result = await regenerate_diagram_with_feedback(
                    cfg=self._cfg,
                    http_client=self._http(),
                    renderer=renderer,
                    existing_definition=definition,
                    validation_issues=validation_issues,
                    validation_suggestion=validation_suggestion,
                    diagram_type=diagram_type,
                    task_id=task.task_id,
                    session_id=task.session_id,
                    source=task.source,
                    source_id=task.source_id,
                    channel=task.channel,
                )
                if regen_result and regen_result.get("definition"):
                    definition = regen_result["definition"]
                # Re-render with corrected definition
                try:
                    if renderer == "mermaid":
                        ext = "png" if output_format == "png" else "svg"
                        out_path = task_artifact_dir / f"diagram.{ext}"
                        result = await render_mermaid(
                            definition,
                            mmdc_path=self._cfg.mmdc_path,
                            output_format=output_format,
                            background=self._cfg.mermaid_background,
                            theme=self._cfg.default_theme,
                            disable_sandbox=self._cfg.mermaid_disable_sandbox,
                            output_path=out_path,
                        )
                    elif renderer == "d2":
                        ext = "png" if output_format == "png" else "svg"
                        out_path = task_artifact_dir / f"diagram.{ext}"
                        result = await render_d2(
                            definition,
                            d2_path=self._cfg.d2_path,
                            output_format=output_format,
                            sketch=self._cfg.d2_sketch,
                            pad=self._cfg.d2_pad,
                            output_path=out_path,
                        )
                    elif renderer == "excalidraw":
                        out_path = task_artifact_dir / "diagram.excalidraw"
                        result = render_excalidraw(definition, output_path=out_path)
                    content = result.get("content", b"")
                    png_for_vision = content if output_format == "png" else None
                except RenderError:
                    pass  # Keep last good render

        artifacts: list[ArtifactManifest] = []
        if definition:
            source_ext = {"mermaid": "mmd", "d2": "d2", "excalidraw": "excalidraw"}.get(
                renderer, "txt"
            )
            source_mime = (
                "application/json" if renderer == "excalidraw" else "text/plain"
            )
            source_path = task_artifact_dir / f"diagram_source.{source_ext}"
            source_path.write_text(definition, encoding="utf-8")
            artifacts.append(
                self._artifact_manifest(
                    task_id=task.task_id,
                    path=source_path,
                    mime=source_mime,
                    kind="output",
                    audience="supporting",
                )
            )

        if render_ok and content:
            if renderer == "excalidraw":
                out_mime = "application/json"
            elif output_format == "png":
                out_mime = "image/png"
            else:
                out_mime = "image/svg+xml"

            out_path = task_artifact_dir / (
                "diagram.excalidraw"
                if renderer == "excalidraw"
                else f"diagram.{'png' if output_format == 'png' else 'svg'}"
            )
            if out_path.exists():
                artifacts.append(
                    self._artifact_manifest(
                        task_id=task.task_id,
                        path=out_path,
                        mime=out_mime,
                        kind="output",
                        audience="deliverable",
                    )
                )

        # Save session
        try:
            self.db.execute(
                """INSERT OR REPLACE INTO diagram_sessions
                   (session_id, task_id, intent, renderer, diagram_type, title, definition_preview, rendered_ok, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    task.session_id,
                    task.task_id,
                    task.intent,
                    renderer,
                    diagram_type,
                    title,
                    definition[:500],
                    1 if render_ok else 0,
                    utcnow().isoformat(),
                ],
            )
            self.db.commit()
        except Exception as exc:
            logger.warning("Failed to save diagram session: %s", exc)

        return AgentResult(
            status="completed",
            output={
                "renderer": renderer,
                "diagram_type": diagram_type,
                "title": title,
                "definition": definition,
                "rendered": render_ok,
                "render_format": output_format if render_ok else None,
                "render_error": render_error if not render_ok else None,
                "validation_pass": validation_pass,
                "validation_issues": validation_issues if not validation_pass else [],
            },
            artifacts=artifacts,
            error=None,
        )
