"""Slide Agent — Presentation creation and editing specialist for COSMIC.

Handles slide.create, slide.edit, slide.recall_session.

LangGraph workflow (when enabled): analyze → assets → build → validate → finalize
Direct handler fallback: core build/validate/export path without graph state management.
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
    TaskInProgress,
    utcnow,
)
from shared.sqlite_client import connect_sync

from .config import AGENT_ROOT, BACKEND_ROOT, SlideAgentConfig
from .slide_builder import SlideBuilder, export_to_pdf, render_slides_to_png

logger = logging.getLogger(__name__)

_SLIDE_SESSIONS_SQL = """
CREATE TABLE IF NOT EXISTS slide_sessions (
    session_id TEXT,
    task_id TEXT,
    intent TEXT NOT NULL,
    slide_count INTEGER,
    template TEXT,
    title TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_slide_sessions_session_created
ON slide_sessions (session_id, created_at DESC);
"""


class SlideAgent(AgentRuntime):
    """Slide deck specialist agent."""

    def __init__(self, redis_client, config: SlideAgentConfig | None = None):
        self._cfg = config or SlideAgentConfig.from_env()
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
        self.agent_id = "cosmic/slide-agent:1.0.0"

    async def on_startup(self):
        self.learnings_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.learnings_path.exists():
            self.learnings_path.write_text("# Slide Agent — Learnings\n")

        data_dir = AGENT_ROOT / "store" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db = connect_sync(str(data_dir / "slide_sessions.db"))
        self.db.executescript(_SLIDE_SESSIONS_SQL)
        self.db.commit()

        runtime_dir = AGENT_ROOT / "runtime"
        (runtime_dir / "cache").mkdir(parents=True, exist_ok=True)
        (runtime_dir / "logs").mkdir(parents=True, exist_ok=True)

    async def _postprocess_rendered_deck(
        self,
        *,
        task: TaskEnvelope,
        output_path: Path,
        artifacts: list[ArtifactManifest],
        slide_plans: list[dict[str, Any]] | None = None,
    ) -> tuple[bool, list[str], str | None, list[ArtifactManifest]]:
        from .internal_llm import validate_slide

        validation_issues: list[str] = []
        validation_pass = True

        if output_path.exists() and self._cfg.enable_internal_llm:
            pngs = render_slides_to_png(
                output_path,
                libreoffice_path=self._cfg.libreoffice_path,
                pdftoppm_path=self._cfg.pdftoppm_path,
                dpi=150,
                output_dir=output_path.parent / "previews",
            )
            from .asset_manager import resize_image as _resize_img

            for png_path in pngs:
                try:
                    slide_num = int(png_path.stem.split("-")[1])
                except (IndexError, ValueError):
                    slide_num = 1
                png_bytes = png_path.read_bytes()
                if not png_bytes or len(png_bytes) < 100:
                    continue
                try:
                    png_bytes = _resize_img(
                        png_bytes, target_width_px=960, target_height_px=540
                    )
                except Exception:
                    pass
                slide_plan = None
                if slide_plans and 0 < slide_num <= len(slide_plans):
                    slide_plan = slide_plans[slide_num - 1]
                result = await validate_slide(
                    cfg=self._cfg,
                    http_client=self._http(),
                    slide_number=slide_num,
                    png_bytes=png_bytes,
                    slide_plan=slide_plan,
                    task_id=task.task_id,
                    session_id=task.session_id,
                    source=task.source,
                    source_id=task.source_id,
                    channel=task.channel,
                )
                if result and not result.get("pass", True):
                    validation_pass = False
                    for issue in result.get("issues", []):
                        validation_issues.append(f"Slide {slide_num}: {issue}")

            for png in pngs:
                if png.exists():
                    artifacts.append(
                        self._artifact_manifest(
                            task_id=task.task_id,
                            path=png,
                            mime="image/png",
                            kind="output",
                            audience="supporting",
                        )
                    )

        pdf_path = None
        if self._cfg.export_pdf and output_path.exists():
            pdf_path = export_to_pdf(
                output_path, libreoffice_path=self._cfg.libreoffice_path
            )
            if pdf_path and pdf_path.exists():
                artifacts.append(
                    self._artifact_manifest(
                        task_id=task.task_id,
                        path=pdf_path,
                        mime="application/pdf",
                    )
                )
                return validation_pass, validation_issues, str(pdf_path), artifacts

        return validation_pass, validation_issues, None, artifacts

    def _load_task_context(self):
        self.system_prompt = (self.prompts_dir / "system.md").read_text()
        self.policies = (self.prompts_dir / "policies.md").read_text()
        self.learnings = self.learnings_path.read_text()

    def _http(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=30, http2=False)
        return self._http_client

    def _task_artifact_dir(self, task_id: str) -> Path:
        return self.artifacts_root / task_id / "slide_agent"

    def _artifact_manifest(
        self,
        *,
        task_id: str,
        path: Path,
        mime: str,
        kind: str = "output",
        audience: str = "deliverable",
    ) -> ArtifactManifest:
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

    async def execute(self, task: TaskEnvelope) -> AgentResult | TaskInProgress:
        self._load_task_context()

        if self._cfg.slide_use_langgraph:
            try:
                from .slide_graph import run_slide_langgraph
            except ImportError as exc:
                logger.warning("slide.langgraph_unavailable: %s", exc)
            else:
                return await run_slide_langgraph(agent=self, task=task)

        # Fallback: direct handler
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

    async def handle_slide_create(self, task: TaskEnvelope) -> AgentResult:
        """Direct handler for slide.create — plans, builds, validates, exports."""
        from .internal_llm import plan_deck
        from .layout_engine import BoundingBox, LayoutEngine, SlideBounds
        from .slide_graph import (
            _build_source_material_prompt_payload,
            _collect_task_source_artifacts,
            _document_summaries_from_artifacts,
            _extract_visual_assets_from_artifacts,
            _hydrate_deck_plan_sources,
            _is_docs_parser_artifact,
            _is_document_artifact,
        )

        desc = task.input.get("description", "") or task.input.get("query", "")
        if not desc:
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="INVALID_INPUT",
                    retryable=False,
                    message="slide.create requires 'description'.",
                    next_action="escalate",
                ),
            )

        # Read learnings
        learnings_context = ""
        if self.learnings_path.exists():
            try:
                lc = self.learnings_path.read_text(encoding="utf-8").strip()
                if lc and len(lc) > 30:
                    learnings_context = f"\n\nUser preferences:\n{lc[:3000]}\n"
            except Exception:
                pass

        template = task.input.get("template", self._cfg.default_template)
        source_artifacts = _collect_task_source_artifacts(task)
        if any(
            _is_document_artifact(artifact) and not _is_docs_parser_artifact(artifact)
            for artifact in source_artifacts
        ):
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="FALLBACK_SOURCE_ARTIFACT_PREPARATION_UNAVAILABLE",
                    retryable=False,
                    message=(
                        "slide.create with raw uploaded documents requires the LangGraph "
                        "workflow so the slide agent can delegate docs.parse_bundle first."
                    ),
                    next_action="retry",
                ),
            )
        source_documents = _document_summaries_from_artifacts(source_artifacts)
        source_visual_assets = _extract_visual_assets_from_artifacts(source_artifacts)
        input_data = dict(task.input.get("data") or {})
        source_materials = _build_source_material_prompt_payload(
            documents=source_documents,
            visual_assets=source_visual_assets,
        )
        if source_materials["documents"] or source_materials["visual_assets"]:
            input_data["_source_materials"] = source_materials
        deck_plan = await plan_deck(
            cfg=self._cfg,
            http_client=self._http(),
            description=desc,
            template=template,
            input_data=input_data or None,
            learnings_context=learnings_context,
            task_id=task.task_id,
            session_id=task.session_id,
            source=task.source,
            source_id=task.source_id,
            channel=task.channel,
        )

        if deck_plan and deck_plan.get("action") == "request_doc_context":
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="FALLBACK_DOC_CONTEXT_DELEGATION_UNAVAILABLE",
                    retryable=False,
                    message=(
                        "slide.create needs a docs specialist lookup for this request, "
                        "which requires the LangGraph workflow."
                    ),
                    next_action="retry",
                ),
            )
        if deck_plan and deck_plan.get("action") == "create_plan":
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="FALLBACK_STEPPLAN_UNAVAILABLE",
                    retryable=False,
                    message=(
                        "slide.create returned a multi-step plan, which requires the "
                        "LangGraph workflow."
                    ),
                    next_action="retry",
                ),
            )

        if not deck_plan or not deck_plan.get("slides"):
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="INTERNAL_ERROR",
                    retryable=True,
                    message="Failed to generate deck plan.",
                    next_action="retry",
                ),
            )

        try:
            if source_visual_assets:
                deck_plan = _hydrate_deck_plan_sources(
                    deck_plan,
                    visual_assets=source_visual_assets,
                )
        except Exception as exc:
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="SOURCE_ASSET_RESOLUTION_FAILED",
                    retryable=False,
                    message=str(exc),
                    next_action="revise_input",
                ),
            )

        for slide_def in deck_plan.get("slides", []):
            for slot_name in ("image", "content", "left_content", "right_content"):
                content = slide_def.get(slot_name, {})
                if not isinstance(content, dict):
                    continue
                source = content.get("source", {})
                if isinstance(source, dict) and source.get("kind") == "generate":
                    return AgentResult(
                        status="failed",
                        output={},
                        artifacts=[],
                        error=AgentError(
                            code="FALLBACK_ASSET_DELEGATION_UNAVAILABLE",
                            retryable=False,
                            message="slide.create with generated images/diagrams requires the LangGraph workflow.",
                            next_action="retry",
                        ),
                    )

        task_artifacts_dir = self._task_artifact_dir(task.task_id)
        task_artifacts_dir.mkdir(parents=True, exist_ok=True)
        output_path = task_artifacts_dir / "presentation.pptx"

        # Pre-build layout validation
        deck_def = deck_plan.get("deck", {})
        layout_engine = LayoutEngine(
            SlideBounds(
                width=deck_def.get("dimensions", {}).get("width", 13.333),
                height=deck_def.get("dimensions", {}).get("height", 7.5),
            )
        )
        from .slide_graph import _extract_bounding_boxes

        for slide_def in deck_plan.get("slides", []):
            report = layout_engine.validate(_extract_bounding_boxes(slide_def))
            if not report.valid:
                logger.warning(
                    "Layout issues on slide %s: %s",
                    slide_def.get("slide_number"),
                    report.summary(),
                )

        # Build PPTX
        builder = SlideBuilder(self._cfg.templates_dir)
        builder.build_deck(deck_plan, output_path)

        artifacts: list[ArtifactManifest] = []
        if output_path.exists():
            artifacts.append(
                self._artifact_manifest(
                    task_id=task.task_id,
                    path=output_path,
                    mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                )
            )

        validation_pass, validation_issues, pdf_path, artifacts = (
            await self._postprocess_rendered_deck(
                task=task,
                output_path=output_path,
                artifacts=artifacts,
                slide_plans=list(deck_plan.get("slides", [])),
            )
        )

        # Save session
        try:
            self.db.execute(
                """INSERT OR REPLACE INTO slide_sessions
                   (session_id, task_id, intent, slide_count, template, title, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    task.session_id,
                    task.task_id,
                    task.intent,
                    len(deck_plan.get("slides", [])),
                    template,
                    deck_plan.get("deck", {}).get("title", ""),
                    utcnow().isoformat(),
                ],
            )
            self.db.commit()
        except Exception:
            pass

        return AgentResult(
            status="completed",
            output={
                "pptx_path": str(output_path),
                "pdf_path": pdf_path,
                "slide_count": len(deck_plan.get("slides", [])),
                "template": template,
                "title": deck_plan.get("deck", {}).get("title", ""),
                "validation_pass": validation_pass,
                "validation_issues": validation_issues,
            },
            artifacts=artifacts,
            error=None,
        )

    async def handle_slide_edit(self, task: TaskEnvelope) -> AgentResult:
        """Direct handler for slide.edit with the same build/validate/export finish path."""
        from .internal_llm import plan_edit
        from .slide_graph import (
            _build_source_material_prompt_payload,
            _collect_task_source_artifacts,
            _document_summaries_from_artifacts,
            _extract_visual_assets_from_artifacts,
            _hydrate_edit_operation_sources,
            _is_docs_parser_artifact,
            _is_document_artifact,
        )

        source_path = task.input.get("source_pptx_path", "")
        edit_request = task.input.get("edit_request", "")
        if not source_path or not edit_request:
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="INVALID_INPUT",
                    retryable=False,
                    message="slide.edit requires 'source_pptx_path' and 'edit_request'.",
                    next_action="escalate",
                ),
            )

        builder = SlideBuilder(self._cfg.templates_dir)
        prs = builder.load_existing(Path(source_path))
        structure = builder.extract_structure(prs)
        source_artifacts = _collect_task_source_artifacts(task)
        if any(
            _is_document_artifact(artifact) and not _is_docs_parser_artifact(artifact)
            for artifact in source_artifacts
        ):
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="FALLBACK_SOURCE_ARTIFACT_PREPARATION_UNAVAILABLE",
                    retryable=False,
                    message=(
                        "slide.edit with raw uploaded documents requires the LangGraph "
                        "workflow so the slide agent can delegate docs.parse_bundle first."
                    ),
                    next_action="retry",
                ),
            )
        source_documents = _document_summaries_from_artifacts(source_artifacts)
        source_visual_assets = _extract_visual_assets_from_artifacts(source_artifacts)
        source_materials = _build_source_material_prompt_payload(
            documents=source_documents,
            visual_assets=source_visual_assets,
        )

        edit_plan = await plan_edit(
            cfg=self._cfg,
            http_client=self._http(),
            existing_structure=structure,
            edit_request=edit_request,
            source_materials=source_materials,
            task_id=task.task_id,
            session_id=task.session_id,
            source=task.source,
            source_id=task.source_id,
            channel=task.channel,
        )

        if edit_plan and edit_plan.get("action") == "request_doc_context":
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="FALLBACK_DOC_CONTEXT_DELEGATION_UNAVAILABLE",
                    retryable=False,
                    message=(
                        "slide.edit needs a docs specialist lookup for this request, "
                        "which requires the LangGraph workflow."
                    ),
                    next_action="retry",
                ),
            )
        if edit_plan and edit_plan.get("action") == "create_plan":
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="FALLBACK_STEPPLAN_UNAVAILABLE",
                    retryable=False,
                    message=(
                        "slide.edit returned a multi-step plan, which requires the "
                        "LangGraph workflow."
                    ),
                    next_action="retry",
                ),
            )

        if not edit_plan:
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="INTERNAL_ERROR",
                    retryable=True,
                    message="Failed to plan edits.",
                    next_action="retry",
                ),
            )

        operations = edit_plan.get("operations", [])
        try:
            if source_visual_assets:
                operations = _hydrate_edit_operation_sources(
                    operations,
                    visual_assets=source_visual_assets,
                )
        except Exception as exc:
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="SOURCE_ASSET_RESOLUTION_FAILED",
                    retryable=False,
                    message=str(exc),
                    next_action="revise_input",
                ),
            )
        for operation in operations:
            if not isinstance(operation, dict):
                continue
            if str(operation.get("action") or "").strip().lower() != "replace_image":
                continue
            new_image = operation.get("new_image")
            if not isinstance(new_image, dict):
                continue
            source = new_image.get("source")
            if isinstance(source, dict) and source.get("kind") == "generate":
                return AgentResult(
                    status="failed",
                    output={},
                    artifacts=[],
                    error=AgentError(
                        code="FALLBACK_ASSET_DELEGATION_UNAVAILABLE",
                        retryable=False,
                        message=(
                            "slide.edit with generated replacement images requires "
                            "the LangGraph workflow."
                        ),
                        next_action="retry",
                    ),
                )
        prs = builder.apply_edits(prs, operations)

        task_artifacts_dir = self._task_artifact_dir(task.task_id)
        task_artifacts_dir.mkdir(parents=True, exist_ok=True)
        output_path = task_artifacts_dir / "presentation.pptx"
        prs.save(str(output_path))

        artifacts: list[ArtifactManifest] = []
        if output_path.exists():
            artifacts.append(
                self._artifact_manifest(
                    task_id=task.task_id,
                    path=output_path,
                    mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                )
            )

        validation_pass, validation_issues, pdf_path, artifacts = (
            await self._postprocess_rendered_deck(
                task=task,
                output_path=output_path,
                artifacts=artifacts,
                slide_plans=None,
            )
        )

        return AgentResult(
            status="completed",
            output={
                "pptx_path": str(output_path),
                "pdf_path": pdf_path,
                "operations_applied": len(operations),
                "validation_pass": validation_pass,
                "validation_issues": validation_issues,
            },
            artifacts=artifacts,
            error=None,
        )

    async def handle_slide_recall_session(self, task: TaskEnvelope) -> AgentResult:
        session_id = task.input.get("session_id")
        limit = task.input.get("limit", 10)
        rows = self.db.execute(
            """SELECT * FROM slide_sessions WHERE session_id = ? ORDER BY created_at DESC LIMIT ?""",
            [session_id, limit],
        ).fetchall()
        return AgentResult(
            status="completed",
            output={"history": [dict(r) for r in rows], "count": len(rows)},
            artifacts=[],
            error=None,
        )
