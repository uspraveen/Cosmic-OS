from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from agents.slide_agent.agent import SlideAgent
from agents.slide_agent.config import SlideAgentConfig
from shared.contracts import TaskEnvelope, TaskInProgress


TEST_RUNTIME_ROOT = Path(__file__).resolve().parent / "_runtime"


def _runtime_dir() -> Path:
    path = TEST_RUNTIME_ROOT / uuid4().hex
    path.mkdir(parents=True, exist_ok=False)
    return path


def _task(
    *,
    task_id: str,
    input_payload: dict[str, Any],
    input_artifacts: list[dict[str, Any]] | None = None,
    intent: str = "slide.create",
) -> TaskEnvelope:
    return TaskEnvelope(
        task_id=task_id,
        task_list_id="task_list_slide_tests",
        session_id="sess_slide_tests",
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/slide-agent:1.0.0",
        intent=intent,
        input=input_payload,
        input_artifacts=input_artifacts or [],
        idempotency_key=f"idem_{task_id}",
        signature="test-signature",
    )


def _agent(runtime_dir: Path) -> SlideAgent:
    root = runtime_dir / "runs" / "artifacts"
    return SlideAgent(
        None,
        config=SlideAgentConfig(
            artifacts_root=root,
            templates_dir=runtime_dir / "templates",
            catalogs_dir=runtime_dir / "catalogs",
            assets_cache_dir=runtime_dir / "assets" / "cache",
            validate_outputs=False,
        ),
    )


def _write_parsed_page_bundle(runtime_dir: Path, *, page_count: int) -> tuple[Path, dict[str, Any]]:
    bundle_dir = runtime_dir / f"parsed_{page_count}_pages"
    bundle_dir.mkdir()
    pages = []
    parts = []
    cursor = 0
    for page_number in range(1, page_count + 1):
        text = f"[Page {page_number}]\n# Page {page_number} Topic\nImportant source details for page {page_number}."
        start_char = cursor
        end_char = start_char + len(text)
        parts.append(text)
        pages.append(
            {
                "page_id": f"page_{page_number:04d}",
                "page_number": page_number,
                "start_char": start_char,
                "end_char": end_char,
            }
        )
        cursor = end_char + 2
    (bundle_dir / "document.md").write_text("\n\n".join(parts), encoding="utf-8")
    (bundle_dir / "chunk_index.json").write_text(
        json.dumps({"pages": pages, "page_count": page_count}),
        encoding="utf-8",
    )
    manifest_path = bundle_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "doc_id": f"doc_{page_count}_pages",
                "bundle_id": f"bundle_{page_count}_pages",
                "source_artifact_id": "art_source_pages",
                "filename": "source-pages.pdf",
                "mime": "application/pdf",
                "title": "Source Pages",
                "counts": {
                    "section_count": 0,
                    "chunk_count": 0,
                    "table_count": 0,
                    "figure_count": 0,
                    "page_count": page_count,
                },
                "outputs": {
                    "document_md": "document.md",
                    "chunk_index": "chunk_index.json",
                },
            }
        ),
        encoding="utf-8",
    )
    return manifest_path, {
        "artifact_id": "art_manifest_pages",
        "path": str(manifest_path),
        "mime": "application/json",
        "filename": "manifest.json",
        "created_by_agent": "cosmic/docs-parser-agent:1.0.0",
    }


def test_normalize_workflow_requires_known_values() -> None:
    assert SlideAgent._normalize_workflow("html") == "html"
    assert SlideAgent._normalize_workflow("template") == "template"
    assert SlideAgent._normalize_workflow("auto") == "auto"
    assert SlideAgent._normalize_workflow("fast") == ""


def test_mime_for_pptx() -> None:
    assert SlideAgent._mime_for_path(Path("deck.pptx")) == "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def test_slide_edit_regenerates_html_for_noneditable_source(monkeypatch: Any) -> None:
    runtime_dir = _runtime_dir()
    try:
        agent = _agent(runtime_dir)
        source_dir = runtime_dir / "html_source"
        source_dir.mkdir()
        source_deck = source_dir / "deck.pptx"
        source_deck.write_bytes(b"html deck")
        (source_dir / "build_report.json").write_text(
            json.dumps(
                {
                    "workflow": "html",
                    "description": "A 2-slide deck on COSMIC.",
                }
            ),
            encoding="utf-8",
        )
        (source_dir / "plan.json").write_text(
            json.dumps(
                {
                    "deck_title": "COSMIC",
                    "slides": [
                        {"slide_number": 1, "title": "Intro to COSMIC"},
                        {"slide_number": 2, "title": "Capabilities"},
                    ],
                }
            ),
            encoding="utf-8",
        )

        emitted: list[tuple[str, str, dict[str, Any]]] = []
        captured: dict[str, Any] = {}

        async def fake_emit(task_id: str, event_type: str, payload: dict[str, Any]) -> str:
            emitted.append((task_id, event_type, payload))
            return f"evt_{len(emitted)}"

        async def fake_source_context(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"source_artifacts": [], "documents": [], "visual_assets": []}

        def fake_run_html(
            description: str,
            output_dir: Path,
            max_slides: int | None,
            validate: bool,
            content_plan: dict[str, Any] | None,
        ) -> dict[str, Any]:
            captured["description"] = description
            captured["max_slides"] = max_slides
            captured["validate"] = validate
            captured["content_plan"] = content_plan
            output_dir.mkdir(parents=True, exist_ok=True)
            pptx_path = output_dir / "deck.pptx"
            plan_path = output_dir / "plan.json"
            theme_path = output_dir / "theme.json"
            report_path = output_dir / "build_report.json"
            pptx_path.write_bytes(b"new html deck")
            plan_path.write_text(json.dumps({"deck_title": "COSMIC", "slides": [1, 2]}), encoding="utf-8")
            theme_path.write_text(json.dumps({"theme_name": "dark"}), encoding="utf-8")
            report_path.write_text(json.dumps({"workflow": "html"}), encoding="utf-8")
            return {
                "workflow": "html",
                "pptx_path": str(pptx_path),
                "plan_path": str(plan_path),
                "theme_path": str(theme_path),
                "content_plan": {"deck_title": "COSMIC", "slides": [1, 2]},
            }

        def fail_run_template(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("html-backed edits must not use template regeneration")

        monkeypatch.setattr(agent, "emit_event", fake_emit)
        monkeypatch.setattr(agent, "_prepare_source_materials_for_generation", fake_source_context)
        monkeypatch.setattr(agent, "_count_pptx_slides", lambda _path: 2)
        monkeypatch.setattr(agent, "_run_html", fake_run_html)
        monkeypatch.setattr(agent, "_run_template", fail_run_template)

        result = asyncio.run(
            agent.handle_slide_edit(
                _task(
                    task_id="tsk_edit_html",
                    intent="slide.edit",
                    input_payload={
                        "edit_request": "Make slide 2 dark with a full-bleed image background.",
                        "source_pptx_path": str(source_deck),
                        "validate": False,
                    },
                )
            )
        )

        assert result.status == "completed"
        assert result.output["workflow"] == "html"
        assert result.output["editable"] is False
        assert result.output["edit_mode"] == "html_regeneration_from_non_editable_source"
        assert captured["max_slides"] == 2
        assert captured["content_plan"] is None
        assert "COSMIC" in captured["description"]
        assert "full-bleed image background" in captured["description"]
        assert any(payload["message"] == "Preparing the slide edit." for _, event_type, payload in emitted if event_type == "task.progress")
        assert any("Regenerating the non-editable HTML deck" in payload["message"] for _, event_type, payload in emitted if event_type == "task.progress")
    finally:
        shutil.rmtree(runtime_dir, ignore_errors=True)


def test_slide_edit_uses_template_path_for_editable_source(monkeypatch: Any) -> None:
    runtime_dir = _runtime_dir()
    try:
        agent = _agent(runtime_dir)
        source_deck = runtime_dir / "editable_source.pptx"
        source_deck.write_bytes(b"editable deck")

        emitted: list[tuple[str, str, dict[str, Any]]] = []
        captured: dict[str, Any] = {}

        async def fake_emit(task_id: str, event_type: str, payload: dict[str, Any]) -> str:
            emitted.append((task_id, event_type, payload))
            return f"evt_{len(emitted)}"

        async def fake_source_context(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"source_artifacts": [], "documents": [], "visual_assets": []}

        def fake_run_template(
            description: str,
            template_path: Path,
            output_dir: Path,
            max_slides: int | None,
            validate: bool,
            force_catalog: bool,
            content_plan: dict[str, Any] | None,
        ) -> dict[str, Any]:
            captured["description"] = description
            captured["template_path"] = template_path
            captured["max_slides"] = max_slides
            captured["validate"] = validate
            captured["force_catalog"] = force_catalog
            captured["content_plan"] = content_plan
            output_dir.mkdir(parents=True, exist_ok=True)
            pptx_path = output_dir / "deck.pptx"
            plan_path = output_dir / "plan.json"
            pptx_path.write_bytes(b"new template deck")
            plan_path.write_text(json.dumps({"deck_title": "Editable", "slides": [1, 2]}), encoding="utf-8")
            return {
                "workflow": "template",
                "pptx_path": str(pptx_path),
                "plan_path": str(plan_path),
                "content_plan": {"deck_title": "Editable", "slides": [1, 2]},
            }

        monkeypatch.setattr(agent, "emit_event", fake_emit)
        monkeypatch.setattr(agent, "_prepare_source_materials_for_generation", fake_source_context)
        monkeypatch.setattr(agent, "_count_pptx_slides", lambda _path: 2)
        monkeypatch.setattr(agent, "_looks_like_image_backed_deck", lambda _path: False)
        monkeypatch.setattr(agent, "_run_template", fake_run_template)

        result = asyncio.run(
            agent.handle_slide_edit(
                _task(
                    task_id="tsk_edit_template",
                    intent="slide.edit",
                    input_payload={
                        "edit_request": "Make the deck darker.",
                        "source_pptx_path": str(source_deck),
                        "validate": False,
                    },
                )
            )
        )

        assert result.status == "completed"
        assert result.output["workflow"] == "template"
        assert result.output["editable"] is True
        assert result.output["edit_mode"] == "template_backed_regeneration"
        assert captured["template_path"] == source_deck.resolve()
        assert any("template-backed deck regeneration" in payload["message"] for _, event_type, payload in emitted if event_type == "task.progress")
    finally:
        shutil.rmtree(runtime_dir, ignore_errors=True)


def test_slide_create_keeps_workflow_choice_before_document_parse(monkeypatch: Any) -> None:
    runtime_dir = _runtime_dir()
    try:
        agent = _agent(runtime_dir)
        pdf_path = runtime_dir / "source.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        delegated = False

        async def fake_delegate(**_kwargs: Any) -> dict[str, Any]:
            nonlocal delegated
            delegated = True
            return {"reverse_task_id": "rev_docs_parse"}

        monkeypatch.setattr(agent, "request_orchestrator_delegate", fake_delegate)
        result = asyncio.run(
            agent.handle_slide_create(
                _task(
                    task_id="tsk_needs_choice",
                    input_payload={"description": "Make a deck from this PDF."},
                    input_artifacts=[
                        {
                            "artifact_id": "art_pdf_001",
                            "path": str(pdf_path),
                            "mime": "application/pdf",
                            "filename": "source.pdf",
                        }
                    ],
                )
            )
        )

        assert result.status == "failed"
        assert result.error is not None
        assert result.error.code == "NEEDS_WORKFLOW_CHOICE"
        assert delegated is False
    finally:
        shutil.rmtree(runtime_dir, ignore_errors=True)


def test_raw_pdf_source_delegates_to_docs_parse_after_workflow_choice(monkeypatch: Any) -> None:
    runtime_dir = _runtime_dir()
    try:
        agent = _agent(runtime_dir)
        pdf_path = runtime_dir / "source.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        captured: dict[str, Any] = {}
        emitted: list[tuple[Any, ...]] = []

        async def fake_delegate(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"reverse_task_id": "rev_docs_parse"}

        async def fake_emit(*args: Any, **_kwargs: Any) -> None:
            emitted.append(args)

        monkeypatch.setattr(agent, "request_orchestrator_delegate", fake_delegate)
        monkeypatch.setattr(agent, "emit_event", fake_emit)

        result = asyncio.run(
            agent.handle_slide_create(
                _task(
                    task_id="tsk_pdf_parse",
                    input_payload={"description": "Make a deck from this PDF.", "workflow": "html"},
                    input_artifacts=[
                        {
                            "artifact_id": "art_pdf_001",
                            "path": str(pdf_path),
                            "mime": "application/pdf",
                            "filename": "source.pdf",
                        }
                    ],
                )
            )
        )

        assert isinstance(result, TaskInProgress)
        assert captured["target_intent"] == "docs.parse_bundle"
        assert captured["target_agent_id"] == agent.config.docs_parser_agent_id
        assert captured["target_input"]["generate_page_images"] is True
        assert captured["target_input"]["generate_picture_images"] is True
        assert captured["input_artifacts"][0]["artifact_id"] == "art_pdf_001"
        assert captured["resume_payload"]["pending_asset_request"]["request_kind"] == "docs_parse"
        assert emitted[0][1] == "task.suspended"
    finally:
        shutil.rmtree(runtime_dir, ignore_errors=True)


def test_parsed_document_bundle_context_is_injected_into_html_generation(monkeypatch: Any) -> None:
    runtime_dir = _runtime_dir()
    try:
        agent = _agent(runtime_dir)
        bundle_dir = runtime_dir / "parsed_bundle"
        bundle_dir.mkdir()
        (bundle_dir / "document.md").write_text(
            "# Market Overview\n\nIndia EV adoption accelerated after FAME incentives and state policies.",
            encoding="utf-8",
        )
        (bundle_dir / "chunk_index.json").write_text(
            json.dumps(
                {
                    "sections": [
                        {
                            "section_id": "sec_market",
                            "title": "Market Overview",
                            "summary": "India EV adoption accelerated after FAME incentives and state policies.",
                            "page_number": 2,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        (bundle_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "doc_id": "doc_pdf_001",
                    "bundle_id": "bundle_pdf_001",
                    "source_artifact_id": "art_pdf_001",
                    "filename": "ev-report.pdf",
                    "mime": "application/pdf",
                    "title": "India EV Report",
                    "counts": {
                        "section_count": 1,
                        "chunk_count": 1,
                        "table_count": 0,
                        "figure_count": 0,
                        "page_count": 8,
                    },
                    "outputs": {
                        "document_md": "document.md",
                        "chunk_index": "chunk_index.json",
                    },
                }
            ),
            encoding="utf-8",
        )

        captured: dict[str, str] = {}

        def fake_run_html(
            description: str,
            output_dir: Path,
            _max_slides: int | None,
            _validate: bool,
            _content_plan: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            captured["description"] = description
            pptx_path = output_dir / "deck.pptx"
            pptx_path.write_bytes(b"fake-pptx")
            return {
                "pptx_path": str(pptx_path),
                "content_plan": {
                    "deck_title": "India EV Report",
                    "slides": [{"title": "Market Overview"}],
                },
            }

        monkeypatch.setattr(agent, "_run_html", fake_run_html)

        result = asyncio.run(
            agent.handle_slide_create(
                _task(
                    task_id="tsk_parsed_bundle",
                    input_payload={"description": "Make a deck from this report.", "workflow": "html"},
                    input_artifacts=[
                        {
                            "artifact_id": "art_manifest_001",
                            "path": str(bundle_dir / "manifest.json"),
                            "mime": "application/json",
                            "filename": "manifest.json",
                            "created_by_agent": agent.config.docs_parser_agent_id,
                        }
                    ],
                )
            )
        )

        assert result.status == "completed"
        assert "Source document context:" in captured["description"]
        assert "India EV Report" in captured["description"]
        assert "India EV adoption accelerated" in captured["description"]
    finally:
        shutil.rmtree(runtime_dir, ignore_errors=True)


def test_docs_parse_resume_result_is_consumed_without_redelegating(monkeypatch: Any) -> None:
    runtime_dir = _runtime_dir()
    try:
        agent = _agent(runtime_dir)
        raw_pdf_path = runtime_dir / "source.pdf"
        raw_pdf_path.write_bytes(b"%PDF-1.4")
        bundle_dir = runtime_dir / "parsed_bundle"
        bundle_dir.mkdir()
        (bundle_dir / "document.md").write_text(
            "Battery supply chains and charging infrastructure are key risks for India EV growth.",
            encoding="utf-8",
        )
        (bundle_dir / "chunk_index.json").write_text(
            json.dumps(
                {
                    "chunks": [
                        {
                            "chunk_id": "chunk_1",
                            "title": "Risks",
                            "text": "Battery supply chains and charging infrastructure are key risks.",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        manifest_path = bundle_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "doc_id": "doc_pdf_002",
                    "bundle_id": "bundle_pdf_002",
                    "source_artifact_id": "art_pdf_002",
                    "filename": "ev-risks.pdf",
                    "mime": "application/pdf",
                    "title": "EV Risks",
                    "counts": {
                        "section_count": 0,
                        "chunk_count": 1,
                        "table_count": 0,
                        "figure_count": 0,
                        "page_count": 3,
                    },
                    "outputs": {
                        "document_md": "document.md",
                        "chunk_index": "chunk_index.json",
                    },
                }
            ),
            encoding="utf-8",
        )
        raw_artifact = {
            "artifact_id": "art_pdf_002",
            "path": str(raw_pdf_path),
            "mime": "application/pdf",
            "filename": "ev-risks.pdf",
        }
        parsed_artifact = {
            "artifact_id": "art_manifest_002",
            "path": str(manifest_path),
            "mime": "application/json",
            "filename": "manifest.json",
            "created_by_agent": agent.config.docs_parser_agent_id,
        }
        captured: dict[str, str] = {}
        delegated = False

        async def fake_delegate(**_kwargs: Any) -> dict[str, Any]:
            nonlocal delegated
            delegated = True
            return {"reverse_task_id": "unexpected"}

        def fake_run_html(
            description: str,
            output_dir: Path,
            _max_slides: int | None,
            _validate: bool,
            _content_plan: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            captured["description"] = description
            pptx_path = output_dir / "deck.pptx"
            pptx_path.write_bytes(b"fake-pptx")
            return {
                "pptx_path": str(pptx_path),
                "content_plan": {
                    "deck_title": "EV Risks",
                    "slides": [{"title": "Risks"}],
                },
            }

        monkeypatch.setattr(agent, "request_orchestrator_delegate", fake_delegate)
        monkeypatch.setattr(agent, "_run_html", fake_run_html)

        result = asyncio.run(
            agent.handle_slide_create(
                _task(
                    task_id="tsk_resume_parse",
                    input_payload={
                        "description": "Make a deck from this report.",
                        "workflow": "html",
                        "_resume": {
                            "resume_state": {
                                "pending_asset_request": {"request_kind": "docs_parse"},
                                "source_artifacts": [raw_artifact],
                                "source_documents": [],
                            },
                            "reverse_result": {
                                "status": "completed",
                                "output": {"documents": []},
                                "artifacts": [parsed_artifact],
                            },
                        },
                    },
                    input_artifacts=[raw_artifact],
                )
            )
        )

        assert result.status == "completed"
        assert delegated is False
        assert "EV Risks" in captured["description"]
        assert "Battery supply chains" in captured["description"]
    finally:
        shutil.rmtree(runtime_dir, ignore_errors=True)


def test_one_slide_per_page_html_splits_at_deck_limit(monkeypatch: Any) -> None:
    runtime_dir = _runtime_dir()
    try:
        agent = _agent(runtime_dir)
        _manifest_path, parsed_artifact = _write_parsed_page_bundle(runtime_dir, page_count=51)
        calls: list[dict[str, Any]] = []

        def fake_run_html(
            description: str,
            output_dir: Path,
            max_slides: int | None,
            _validate: bool,
            content_plan: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            calls.append(
                {
                    "description": description,
                    "output_dir": output_dir,
                    "max_slides": max_slides,
                    "content_plan": content_plan,
                }
            )
            output_dir.mkdir(parents=True, exist_ok=True)
            pptx_path = output_dir / "deck.pptx"
            pptx_path.write_bytes(b"fake-pptx")
            plan_path = output_dir / "plan.json"
            plan_path.write_text(json.dumps(content_plan or {"slides": []}), encoding="utf-8")
            return {
                "pptx_path": str(pptx_path),
                "plan_path": str(plan_path),
                "content_plan": content_plan or {"deck_title": "Source Pages", "slides": []},
            }

        monkeypatch.setattr(agent, "_run_html", fake_run_html)

        result = asyncio.run(
            agent.handle_slide_create(
                _task(
                    task_id="tsk_split_html",
                    input_payload={
                        "description": "Make a slide deck from this PDF with one slide per page.",
                        "workflow": "html",
                    },
                    input_artifacts=[parsed_artifact],
                )
            )
        )

        assert result.status == "completed"
        assert result.output["split"] is True
        assert result.output["slide_count"] == 51
        assert len(result.output["deck_parts"]) == 2
        assert len(calls) == 2
        assert calls[0]["max_slides"] == 50
        assert calls[1]["max_slides"] == 1
        assert len(calls[0]["content_plan"]["slides"]) == 50
        assert len(calls[1]["content_plan"]["slides"]) == 1
        assert "Retrieved source window: source units 1-50 of 51" in calls[0]["description"]
        assert "Retrieved source window: source units 51-51 of 51" in calls[1]["description"]
        assert result.output["deck_parts"][0]["start_slide"] == 1
        assert result.output["deck_parts"][1]["start_slide"] == 51
    finally:
        shutil.rmtree(runtime_dir, ignore_errors=True)


def test_one_slide_per_page_template_splits_with_content_plans(monkeypatch: Any) -> None:
    runtime_dir = _runtime_dir()
    try:
        agent = _agent(runtime_dir)
        _manifest_path, parsed_artifact = _write_parsed_page_bundle(runtime_dir, page_count=51)
        template_path = runtime_dir / "template.pptx"
        template_path.write_bytes(b"fake-template")
        calls: list[dict[str, Any]] = []

        def fake_select_template_for_plan(plan: dict[str, Any], _force_catalog: bool) -> Path:
            assert len(plan["slides"]) == 50
            return template_path

        def fake_run_template(
            description: str,
            selected_template: Path,
            output_dir: Path,
            max_slides: int | None,
            _validate: bool,
            _force_catalog: bool,
            content_plan: dict[str, Any] | None,
        ) -> dict[str, Any]:
            calls.append(
                {
                    "description": description,
                    "template": selected_template,
                    "output_dir": output_dir,
                    "max_slides": max_slides,
                    "content_plan": content_plan,
                }
            )
            output_dir.mkdir(parents=True, exist_ok=True)
            pptx_path = output_dir / "deck.pptx"
            pptx_path.write_bytes(b"fake-pptx")
            build_spec_path = output_dir / "build_spec.json"
            build_spec_path.write_text(json.dumps({"slides": content_plan["slides"]}), encoding="utf-8")
            return {
                "pptx_path": str(pptx_path),
                "content_plan": content_plan,
                "build_spec": {"deck_title": content_plan["deck_title"], "slides": content_plan["slides"]},
            }

        monkeypatch.setattr(agent, "_select_template_for_plan", fake_select_template_for_plan)
        monkeypatch.setattr(agent, "_run_template", fake_run_template)

        result = asyncio.run(
            agent.handle_slide_create(
                _task(
                    task_id="tsk_split_template",
                    input_payload={
                        "description": "Make a slide deck from this PDF with one slide per page.",
                        "workflow": "template",
                    },
                    input_artifacts=[parsed_artifact],
                )
            )
        )

        assert result.status == "completed"
        assert result.output["split"] is True
        assert result.output["slide_count"] == 51
        assert result.output["template_path"] == str(template_path)
        assert len(calls) == 2
        assert calls[0]["max_slides"] == 50
        assert calls[1]["max_slides"] == 1
        assert len(calls[0]["content_plan"]["slides"]) == 50
        assert len(calls[1]["content_plan"]["slides"]) == 1
        assert calls[0]["template"] == template_path
        assert calls[1]["template"] == template_path
    finally:
        shutil.rmtree(runtime_dir, ignore_errors=True)
