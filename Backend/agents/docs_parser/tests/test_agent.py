from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from agents.docs_parser.agent import DocsParserAgent
from agents.docs_parser.config import DocsParserConfig
from agents.docs_parser.docling_adapter import DoclingAdapter, ParseRequest, ParsedDocument
from agents.docs_parser.office_renderer import RenderedOfficeDocument
from shared import TaskEnvelope, sign_task_envelope, utcnow


class FakeRedis:
    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.lists: dict[str, list[str]] = {}
        self.expirations: dict[str, int] = {}
        self._counter = 0
        self._sequence = 0

    async def incr(self, key: str) -> int:
        self._counter += 1
        return self._counter

    async def xadd(
        self,
        stream: str,
        fields: dict[str, str],
        *,
        maxlen: int | None = None,
        approximate: bool | None = None,
    ) -> str:
        del maxlen, approximate
        self._sequence += 1
        message_id = f"{self._sequence}-0"
        self.streams.setdefault(stream, []).append((message_id, dict(fields)))
        return message_id

    async def rpush(self, key: str, value: str) -> int:
        bucket = self.lists.setdefault(key, [])
        bucket.append(value)
        return len(bucket)

    async def expire(self, key: str, ttl: int) -> bool:
        self.expirations[key] = ttl
        return True


class StubParser:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def parse_file(
        self,
        *,
        file_path: Path,
        artifact_id: str,
        mime_type: str,
        request: ParseRequest,
        source_filename: str | None = None,
    ) -> ParsedDocument:
        del source_filename
        self.calls.append(
            {
                "file_path": str(file_path),
                "artifact_id": artifact_id,
                "mime_type": mime_type,
                "request": request,
            }
        )
        markdown = (
            "[PAGE 1]\n\n"
            "# Quarterly Strategy\n\n"
            "Overview paragraph.\n\n"
            "[PAGE 2]\n\n"
            "[TABLE id=tbl_001 asset_id=asset_tbl_001 page=2 title=\"Ownership Table\"]\n\n"
            "[FIGURE id=fig_001 asset_id=asset_fig_001 page=2 caption=\"Architecture\"]\n\n"
            "## Key Changes\n\n"
            "Shift focus to enterprise.\n"
        )
        page_two_start = markdown.index("[PAGE 2]")
        return ParsedDocument(
            title="Quarterly Strategy",
            markdown=markdown,
            document_json={"kind": "docling_document", "source": file_path.name},
            chunk_index={
                "sections": [
                    {
                        "section_id": "sec_1",
                        "index": 1,
                        "title": "Quarterly Strategy",
                        "level": 1,
                        "text": "# Quarterly Strategy\n\nOverview paragraph.",
                        "start_char": markdown.index("# Quarterly Strategy"),
                        "end_char": page_two_start - 2,
                    },
                    {
                        "section_id": "sec_2",
                        "index": 2,
                        "title": "Key Changes",
                        "level": 2,
                        "text": "## Key Changes\n\nShift focus to enterprise.",
                        "start_char": markdown.index("## Key Changes"),
                        "end_char": len(markdown),
                    },
                ],
                "pages": [
                    {"page_id": "page_0001", "page_number": 1, "start_char": 0, "end_char": page_two_start - 2},
                    {"page_id": "page_0002", "page_number": 2, "start_char": page_two_start, "end_char": len(markdown)},
                ],
                "slides": [],
                "tables": [
                    {
                        "table_id": "tbl_001",
                        "asset_id": "asset_tbl_001",
                        "title": "Ownership Table",
                        "page_number": 2,
                        "start_char": markdown.index("[TABLE id=tbl_001"),
                        "end_char": markdown.index("[FIGURE id=fig_001") - 2,
                    }
                ],
                "figures": [
                    {
                        "figure_id": "fig_001",
                        "asset_id": "asset_fig_001",
                        "caption": "Architecture",
                        "description": "A systems architecture diagram connecting gateway, orchestrator, and memory services.",
                        "classification": {"label": "flow_chart", "confidence": 0.98},
                        "page_number": 2,
                        "start_char": markdown.index("[FIGURE id=fig_001"),
                        "end_char": markdown.index("## Key Changes") - 2,
                    }
                ],
                "assets": [
                    {
                        "asset_id": "asset_tbl_001",
                        "kind": "table_markdown",
                        "table_id": "tbl_001",
                        "path": "assets/tables/tbl_001.md",
                        "mime": "text/markdown",
                    },
                    {
                        "asset_id": "asset_fig_001",
                        "kind": "figure_image",
                        "figure_id": "fig_001",
                        "path": "assets/figures/fig_001.png",
                        "mime": "image/png",
                        "description": "A systems architecture diagram connecting gateway, orchestrator, and memory services.",
                        "classification": {"label": "flow_chart", "confidence": 0.98},
                    },
                ],
                "chunks": [
                    {
                        "chunk_id": "chk_1",
                        "section_id": "sec_1",
                        "section_title": "Quarterly Strategy",
                        "text": "Overview paragraph.",
                        "search_text": "Quarterly Strategy Overview paragraph.",
                        "doc_start_char": markdown.index("Overview paragraph."),
                        "doc_end_char": markdown.index("Overview paragraph.") + len("Overview paragraph."),
                        "prev_chunk_id": None,
                        "next_chunk_id": "chk_2",
                    },
                    {
                        "chunk_id": "chk_2",
                        "section_id": "sec_2",
                        "section_title": "Key Changes",
                        "text": "Shift focus to enterprise.",
                        "search_text": (
                            "Key Changes Shift focus to enterprise. "
                            "Architecture systems architecture diagram gateway orchestrator memory services."
                        ),
                        "doc_start_char": markdown.index("Shift focus to enterprise."),
                        "doc_end_char": markdown.index("Shift focus to enterprise.") + len("Shift focus to enterprise."),
                        "prev_chunk_id": "chk_1",
                        "next_chunk_id": None,
                    },
                ],
                "chunk_count": 2,
                "section_count": 2,
                "page_count": 2,
                "slide_count": 0,
                "table_count": 1,
                "figure_count": 1,
                "asset_count": 2,
            },
            page_count=2,
            slide_count=None,
            table_count=1,
            figure_count=1,
            section_count=2,
            asset_files=[
                ("assets/tables/tbl_001.md", b"| Owner |\n| --- |\n| Alice |\n", "text/markdown"),
                ("assets/figures/fig_001.png", b"\x89PNG\r\n\x1a\n", "image/png"),
            ],
        )


class FailingParser:
    def __init__(self, message: str) -> None:
        self.message = message

    def parse_file(
        self,
        *,
        file_path: Path,
        artifact_id: str,
        mime_type: str,
        request: ParseRequest,
        source_filename: str | None = None,
    ) -> ParsedDocument:
        del file_path, artifact_id, mime_type, request, source_filename
        raise RuntimeError(self.message)


class PictureDescriptionFallbackParser:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def parse_file(
        self,
        *,
        file_path: Path,
        artifact_id: str,
        mime_type: str,
        request: ParseRequest,
        source_filename: str | None = None,
    ) -> ParsedDocument:
        del source_filename
        self.calls.append(
            {
                "file_path": str(file_path),
                "artifact_id": artifact_id,
                "mime_type": mime_type,
                "request": request,
            }
        )
        if request.picture_description is not None:
            raise RuntimeError("OpenAI picture description request timed out.")
        return StubParser().parse_file(
            file_path=file_path,
            artifact_id=artifact_id,
            mime_type=mime_type,
            request=request,
        )


class FakeOfficeRenderer:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def render_to_pdf(self, *, source_path: Path, working_root: Path) -> RenderedOfficeDocument:
        self.calls.append({"source_path": str(source_path), "working_root": str(working_root)})
        output_root = working_root / "job_001" / "out"
        output_root.mkdir(parents=True, exist_ok=True)
        rendered_path = output_root / f"{source_path.stem}.pdf"
        rendered_path.write_bytes(b"%PDF-1.7 rendered")
        return RenderedOfficeDocument(rendered_pdf_path=rendered_path, backend="fake-office-renderer")


class FailingOfficeRenderer:
    def __init__(self, message: str) -> None:
        self.message = message
        self.calls: list[dict[str, Any]] = []

    def render_to_pdf(self, *, source_path: Path, working_root: Path) -> RenderedOfficeDocument:
        self.calls.append({"source_path": str(source_path), "working_root": str(working_root)})
        raise RuntimeError(self.message)


class OfficeEscalationParser:
    def __init__(self, *, fail_full_page_vlm: bool = False) -> None:
        self.fail_full_page_vlm = fail_full_page_vlm
        self.calls: list[dict[str, Any]] = []

    def parse_file(
        self,
        *,
        file_path: Path,
        artifact_id: str,
        mime_type: str,
        request: ParseRequest,
        source_filename: str | None = None,
    ) -> ParsedDocument:
        del source_filename
        self.calls.append(
            {
                "file_path": str(file_path),
                "artifact_id": artifact_id,
                "mime_type": mime_type,
                "request": request,
            }
        )
        if file_path.suffix.lower() == ".pdf" and request.full_page_vlm is not None:
            if self.fail_full_page_vlm:
                raise RuntimeError("Hosted full-page VLM timed out.")
            markdown = (
                "[SLIDE 1]\n\n"
                "# Visual Overview\n\n"
                "A full-slide timeline with three phases: ingest, parse, and synthesize.\n\n"
                "[SLIDE 2]\n\n"
                "# Architecture Flow\n\n"
                "A systems diagram showing gateway, docs parser, orchestrator, and memory.\n"
            )
            slide_two_start = markdown.index("[SLIDE 2]")
            return ParsedDocument(
                title="Image Heavy Deck",
                markdown=markdown,
                document_json={"kind": "docling_document", "source": file_path.name},
                chunk_index={
                    "sections": [
                        {
                            "section_id": "sec_visual_1",
                            "index": 1,
                            "title": "Visual Overview",
                            "level": 1,
                            "text": "# Visual Overview\n\nA full-slide timeline with three phases: ingest, parse, and synthesize.",
                            "start_char": markdown.index("# Visual Overview"),
                            "end_char": slide_two_start - 2,
                        },
                        {
                            "section_id": "sec_visual_2",
                            "index": 2,
                            "title": "Architecture Flow",
                            "level": 1,
                            "text": "# Architecture Flow\n\nA systems diagram showing gateway, docs parser, orchestrator, and memory.",
                            "start_char": markdown.index("# Architecture Flow"),
                            "end_char": len(markdown),
                        },
                    ],
                    "chunks": [
                        {
                            "chunk_id": "chk_visual_1",
                            "section_id": "sec_visual_1",
                            "section_title": "Visual Overview",
                            "text": "A full-slide timeline with three phases: ingest, parse, and synthesize.",
                            "doc_start_char": markdown.index("A full-slide timeline"),
                            "doc_end_char": markdown.index("A full-slide timeline") + len("A full-slide timeline with three phases: ingest, parse, and synthesize."),
                        },
                        {
                            "chunk_id": "chk_visual_2",
                            "section_id": "sec_visual_2",
                            "section_title": "Architecture Flow",
                            "text": "A systems diagram showing gateway, docs parser, orchestrator, and memory.",
                            "doc_start_char": markdown.index("A systems diagram"),
                            "doc_end_char": markdown.index("A systems diagram") + len("A systems diagram showing gateway, docs parser, orchestrator, and memory."),
                        },
                    ],
                    "pages": [],
                    "slides": [
                        {"slide_id": "slide_0001", "slide_number": 1, "start_char": 0, "end_char": slide_two_start - 2, "anchor_id": "slide_0001"},
                        {"slide_id": "slide_0002", "slide_number": 2, "start_char": slide_two_start, "end_char": len(markdown), "anchor_id": "slide_0002"},
                    ],
                    "tables": [],
                    "figures": [],
                    "assets": [],
                    "chunk_count": 2,
                    "section_count": 2,
                    "page_count": 0,
                    "slide_count": 2,
                    "table_count": 0,
                    "figure_count": 0,
                    "asset_count": 0,
                },
                page_count=None,
                slide_count=2,
                table_count=0,
                figure_count=0,
                section_count=2,
                asset_files=[],
            )

        markdown = "[SLIDE 1]\n\n[SLIDE 2]"
        slide_two_start = markdown.index("[SLIDE 2]")
        return ParsedDocument(
            title="Image Heavy Deck",
            markdown=markdown,
            document_json={"kind": "docling_document", "source": file_path.name},
            chunk_index={
                "sections": [
                    {
                        "section_id": "sec_weak_1",
                        "index": 1,
                        "title": "Document",
                        "level": 0,
                        "text": "[SLIDE 1]\n\n[SLIDE 2]",
                        "start_char": 0,
                        "end_char": len(markdown),
                    }
                ],
                "chunks": [
                    {
                        "chunk_id": "chk_weak_1",
                        "section_id": "sec_weak_1",
                        "section_title": "Document",
                        "text": "[SLIDE 1] [SLIDE 2]",
                        "doc_start_char": 0,
                        "doc_end_char": len(markdown),
                    }
                ],
                "pages": [],
                "slides": [
                    {"slide_id": "slide_0001", "slide_number": 1, "start_char": 0, "end_char": slide_two_start - 2, "anchor_id": "slide_0001"},
                    {"slide_id": "slide_0002", "slide_number": 2, "start_char": slide_two_start, "end_char": len(markdown), "anchor_id": "slide_0002"},
                ],
                "tables": [],
                "figures": [],
                "assets": [],
                "chunk_count": 1,
                "section_count": 1,
                "page_count": 0,
                "slide_count": 2,
                "table_count": 0,
                "figure_count": 0,
                "asset_count": 0,
            },
            page_count=None,
            slide_count=2,
            table_count=0,
            figure_count=0,
            section_count=1,
            asset_files=[],
        )


class TextHeavyPptxParser:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def parse_file(
        self,
        *,
        file_path: Path,
        artifact_id: str,
        mime_type: str,
        request: ParseRequest,
        source_filename: str | None = None,
    ) -> ParsedDocument:
        del source_filename
        self.calls.append(
            {
                "file_path": str(file_path),
                "artifact_id": artifact_id,
                "mime_type": mime_type,
                "request": request,
            }
        )
        if file_path.suffix.lower() == ".pdf" and request.full_page_vlm is not None:
            return OfficeEscalationParser().parse_file(
                file_path=file_path,
                artifact_id=artifact_id,
                mime_type=mime_type,
                request=request,
            )

        markdown = (
            "[SLIDE 1]\n\n"
            "# Growth Plan\n\n"
            "This slide contains extensive readable text about growth strategy, execution plan, and market sequencing.\n\n"
            "[SLIDE 2]\n\n"
            "# Operating Model\n\n"
            "This slide contains detailed readable text covering operating cadence, owners, and milestones.\n"
        )
        slide_two_start = markdown.index("[SLIDE 2]")
        return ParsedDocument(
            title="Readable Deck",
            markdown=markdown,
            document_json={"kind": "docling_document", "source": file_path.name},
            chunk_index={
                "sections": [
                    {
                        "section_id": "sec_text_1",
                        "index": 1,
                        "title": "Growth Plan",
                        "level": 1,
                        "text": "# Growth Plan\n\nThis slide contains extensive readable text about growth strategy, execution plan, and market sequencing.",
                        "start_char": markdown.index("# Growth Plan"),
                        "end_char": slide_two_start - 2,
                    },
                    {
                        "section_id": "sec_text_2",
                        "index": 2,
                        "title": "Operating Model",
                        "level": 1,
                        "text": "# Operating Model\n\nThis slide contains detailed readable text covering operating cadence, owners, and milestones.",
                        "start_char": markdown.index("# Operating Model"),
                        "end_char": len(markdown),
                    },
                ],
                "chunks": [
                    {
                        "chunk_id": "chk_text_1",
                        "section_id": "sec_text_1",
                        "section_title": "Growth Plan",
                        "text": "This slide contains extensive readable text about growth strategy, execution plan, and market sequencing.",
                        "doc_start_char": markdown.index("This slide contains extensive"),
                        "doc_end_char": markdown.index("This slide contains extensive")
                        + len("This slide contains extensive readable text about growth strategy, execution plan, and market sequencing."),
                    },
                    {
                        "chunk_id": "chk_text_2",
                        "section_id": "sec_text_2",
                        "section_title": "Operating Model",
                        "text": "This slide contains detailed readable text covering operating cadence, owners, and milestones.",
                        "doc_start_char": markdown.index("This slide contains detailed"),
                        "doc_end_char": markdown.index("This slide contains detailed")
                        + len("This slide contains detailed readable text covering operating cadence, owners, and milestones."),
                    },
                ],
                "pages": [],
                "slides": [
                    {"slide_id": "slide_0001", "slide_number": 1, "start_char": 0, "end_char": slide_two_start - 2, "anchor_id": "slide_0001"},
                    {"slide_id": "slide_0002", "slide_number": 2, "start_char": slide_two_start, "end_char": len(markdown), "anchor_id": "slide_0002"},
                ],
                "tables": [],
                "figures": [],
                "assets": [],
                "chunk_count": 2,
                "section_count": 2,
                "page_count": 0,
                "slide_count": 2,
                "table_count": 0,
                "figure_count": 0,
                "asset_count": 0,
            },
            page_count=None,
            slide_count=2,
            table_count=0,
            figure_count=0,
            section_count=2,
            asset_files=[],
        )


def _make_task(*, payload: dict[str, object], input_artifacts: list[dict[str, object]], session_id: str = "sess_docs") -> TaskEnvelope:
    task = TaskEnvelope(
        task_id="tsk_docs_parse_bundle",
        task_list_id=session_id,
        parent_task_id="tsk_parent",
        session_id=session_id,
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/docs-parser-agent:1.0.0",
        intent="docs.parse_bundle",
        input=payload,
        input_artifacts=input_artifacts,
        idempotency_key="idem_docs_parse_bundle",
        priority="normal",
        signature="",
        created_at=utcnow(),
        source="user",
        source_id="desktop",
        channel="desktop:test",
    )
    return task.model_copy(update={"signature": sign_task_envelope(task, "agent-secret")})


def _make_search_task(*, bundle_id: str, query: str, search_kind: str | None = None, doc_ids: list[str] | None = None) -> TaskEnvelope:
    payload: dict[str, object] = {"bundle_id": bundle_id, "query": query}
    if search_kind:
        payload["search_kind"] = search_kind
    if doc_ids:
        payload["doc_ids"] = doc_ids
    task = TaskEnvelope(
        task_id="tsk_docs_search_bundle",
        task_list_id="sess_docs",
        parent_task_id="tsk_parent",
        session_id="sess_docs",
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/docs-parser-agent:1.0.0",
        intent="docs.search_bundle",
        input=payload,
        input_artifacts=[],
        idempotency_key="idem_docs_search_bundle",
        priority="normal",
        signature="",
        created_at=utcnow(),
        source="user",
        source_id="desktop",
        channel="desktop:test",
    )
    return task.model_copy(update={"signature": sign_task_envelope(task, "agent-secret")})


def _make_browse_task(*, bundle_id: str, index_kind: str, doc_id: str | None = None, limit: int | None = None) -> TaskEnvelope:
    payload: dict[str, object] = {"bundle_id": bundle_id, "index_kind": index_kind}
    if doc_id:
        payload["doc_id"] = doc_id
    if limit is not None:
        payload["limit"] = limit
    task = TaskEnvelope(
        task_id="tsk_docs_browse_bundle",
        task_list_id="sess_docs",
        parent_task_id="tsk_parent",
        session_id="sess_docs",
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/docs-parser-agent:1.0.0",
        intent="docs.browse_bundle",
        input=payload,
        input_artifacts=[],
        idempotency_key="idem_docs_browse_bundle",
        priority="normal",
        signature="",
        created_at=utcnow(),
        source="user",
        source_id="desktop",
        channel="desktop:test",
    )
    return task.model_copy(update={"signature": sign_task_envelope(task, "agent-secret")})


def _make_read_task(
    *,
    bundle_id: str,
    doc_id: str,
    section_id: str | None = None,
    chunk_ids: list[str] | None = None,
    extra: dict[str, object] | None = None,
) -> TaskEnvelope:
    payload: dict[str, object] = {"bundle_id": bundle_id, "doc_id": doc_id}
    if section_id:
        payload["section_id"] = section_id
    if chunk_ids:
        payload["chunk_ids"] = chunk_ids
    if extra:
        payload.update(extra)
    task = TaskEnvelope(
        task_id="tsk_docs_read_bundle",
        task_list_id="sess_docs",
        parent_task_id="tsk_parent",
        session_id="sess_docs",
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/docs-parser-agent:1.0.0",
        intent="docs.read_bundle",
        input=payload,
        input_artifacts=[],
        idempotency_key="idem_docs_read_bundle",
        priority="normal",
        signature="",
        created_at=utcnow(),
        source="user",
        source_id="desktop",
        channel="desktop:test",
    )
    return task.model_copy(update={"signature": sign_task_envelope(task, "agent-secret")})


def _make_fetch_asset_task(*, bundle_id: str, asset_id: str, doc_id: str | None = None) -> TaskEnvelope:
    payload: dict[str, object] = {"bundle_id": bundle_id, "asset_id": asset_id}
    if doc_id:
        payload["doc_id"] = doc_id
    task = TaskEnvelope(
        task_id="tsk_docs_fetch_asset",
        task_list_id="sess_docs",
        parent_task_id="tsk_parent",
        session_id="sess_docs",
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/docs-parser-agent:1.0.0",
        intent="docs.fetch_asset",
        input=payload,
        input_artifacts=[],
        idempotency_key="idem_docs_fetch_asset",
        priority="normal",
        signature="",
        created_at=utcnow(),
        source="user",
        source_id="desktop",
        channel="desktop:test",
    )
    return task.model_copy(update={"signature": sign_task_envelope(task, "agent-secret")})


def _make_reinspect_asset_task(
    *,
    bundle_id: str,
    asset_id: str,
    doc_id: str | None = None,
    question: str | None = None,
) -> TaskEnvelope:
    payload: dict[str, object] = {"bundle_id": bundle_id, "asset_id": asset_id}
    if doc_id:
        payload["doc_id"] = doc_id
    if question:
        payload["question"] = question
    task = TaskEnvelope(
        task_id="tsk_docs_reinspect_asset",
        task_list_id="sess_docs",
        parent_task_id="tsk_parent",
        session_id="sess_docs",
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/docs-parser-agent:1.0.0",
        intent="docs.reinspect_asset",
        input=payload,
        input_artifacts=[],
        idempotency_key="idem_docs_reinspect_asset",
        priority="normal",
        signature="",
        created_at=utcnow(),
        source="user",
        source_id="desktop",
        channel="desktop:test",
    )
    return task.model_copy(update={"signature": sign_task_envelope(task, "agent-secret")})


@pytest.mark.asyncio
async def test_docs_parser_agent_parse_bundle_persists_canonical_outputs(tmp_path: Path) -> None:
    source_root = tmp_path / "runs" / "artifacts" / "req_ingest_001" / "inputs" / "art_pdf_001"
    source_root.mkdir(parents=True, exist_ok=True)
    source_file = source_root / "strategy.pdf"
    source_file.write_bytes(b"%PDF-1.7 fake strategy pdf")
    sha256 = hashlib.sha256(source_file.read_bytes()).hexdigest()

    parser = StubParser()
    agent = DocsParserAgent(
        redis_client=FakeRedis(),
        config=DocsParserConfig(
            redis_url="redis://unused",
            gateway_url="http://gateway",
            gateway_internal_token="internal-token",
            picture_description_api_key="test-openai-key",
        ),
        parser=parser,
        store_root=tmp_path / "store",
        runtime_root=tmp_path / "runtime",
        artifacts_root=tmp_path / "runs" / "artifacts",
        agent_secret="agent-secret",
    )
    await agent.on_startup()
    try:
        result = await agent.execute(
            _make_task(
                payload={"bundle_label": "Board prep", "ocr_mode": "auto"},
                input_artifacts=[
                    {
                        "artifact_id": "art_pdf_001",
                        "path": str(source_file),
                        "mime": "application/pdf",
                        "filename": "strategy.pdf",
                        "sha256": sha256,
                    }
                ],
            )
        )
    finally:
        await agent.stop()

    assert result.status == "completed"
    assert result.output["document_count"] == 1
    document = result.output["documents"][0]
    assert document["filename"] == "strategy.pdf"
    assert document["section_count"] == 2
    assert document["chunk_count"] == 2
    assert document["asset_count"] == 2
    assert len(result.artifacts) == 6
    assert parser.calls
    assert parser.calls[0]["request"].max_file_size_bytes == agent.config.max_input_file_bytes
    assert parser.calls[0]["request"].max_num_pages == agent.config.max_num_pages
    assert parser.calls[0]["request"].picture_description is not None
    assert parser.calls[0]["request"].generate_picture_images is True

    output_root = tmp_path / "runs" / "artifacts" / "tsk_docs_parse_bundle" / "docs_parser" / "art_pdf_001"
    assert (output_root / "document.json").exists()
    assert (output_root / "document.md").exists()
    assert (output_root / "chunk_index.json").exists()
    assert (output_root / "manifest.json").exists()
    assert (output_root / "assets" / "tables" / "tbl_001.md").exists()
    assert (output_root / "assets" / "figures" / "fig_001.png").exists()

    manifest = json.loads((output_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["source_artifact_id"] == "art_pdf_001"
    assert manifest["counts"]["chunk_count"] == 2
    assert manifest["counts"]["asset_count"] == 2

    bundle_id = result.output["bundle_id"]
    doc_id = document["doc_id"]

    search_result = await agent.execute(_make_search_task(bundle_id=bundle_id, query="enterprise architecture"))
    assert search_result.status == "completed"
    assert search_result.output["count"] >= 1
    assert search_result.output["matches"][0]["doc_id"] == doc_id
    assert search_result.output["matches"][0]["recommended_section_id"] is not None
    assert search_result.output["matches"][0]["recommended_chunk_ids"]

    visual_search_result = await agent.execute(_make_search_task(bundle_id=bundle_id, query="systems architecture diagram"))
    assert visual_search_result.status == "completed"
    assert visual_search_result.output["count"] >= 1
    assert visual_search_result.output["matches"][0]["chunk_id"] == "chk_2"
    assert visual_search_result.output["matches"][0]["recommended_read_kind"] in {"section", "chunk_ids"}

    browse_result = await agent.execute(_make_browse_task(bundle_id=bundle_id, index_kind="sections", doc_id=doc_id))
    assert browse_result.status == "completed"
    assert browse_result.output["index_kind"] == "sections"
    assert len(browse_result.output["sections"]) == 2

    page_browse_result = await agent.execute(_make_browse_task(bundle_id=bundle_id, index_kind="pages", doc_id=doc_id))
    assert page_browse_result.status == "completed"
    assert page_browse_result.output["index_kind"] == "pages"
    assert len(page_browse_result.output["pages"]) == 2

    chunk_browse_result = await agent.execute(_make_browse_task(bundle_id=bundle_id, index_kind="chunks", doc_id=doc_id, limit=1))
    assert chunk_browse_result.status == "completed"
    assert chunk_browse_result.output["index_kind"] == "chunks"
    assert chunk_browse_result.output["chunk_count"] == 2
    assert len(chunk_browse_result.output["chunks"]) == 1
    assert chunk_browse_result.output["chunks"][0]["excerpt"] == "Overview paragraph."

    read_result = await agent.execute(_make_read_task(bundle_id=bundle_id, doc_id=doc_id, section_id="sec_2"))
    assert read_result.status == "completed"
    assert read_result.output["mode"] == "section"
    assert "Shift focus to enterprise" in read_result.output["content"]

    document_read_result = await agent.execute(
        _make_read_task(
            bundle_id=bundle_id,
            doc_id=doc_id,
            extra={"read_kind": "document", "offset_chars": 0, "max_chars": 1200},
        )
    )
    assert document_read_result.status == "completed"
    assert document_read_result.output["mode"] == "document"
    assert "[PAGE 1]" in document_read_result.output["content"]
    assert document_read_result.output["has_more"] is False

    page_read_result = await agent.execute(
        _make_read_task(
            bundle_id=bundle_id,
            doc_id=doc_id,
            extra={"read_kind": "page_range", "start_page": 2, "end_page": 2},
        )
    )
    assert page_read_result.status == "completed"
    assert page_read_result.output["mode"] == "page_range"
    assert "[PAGE 2]" in page_read_result.output["content"]
    assert "Shift focus to enterprise" in page_read_result.output["content"]

    asset_result = await agent.execute(_make_fetch_asset_task(bundle_id=bundle_id, doc_id=doc_id, asset_id="asset_tbl_001"))
    assert asset_result.status == "completed"
    assert asset_result.output["asset"]["kind"] == "table_markdown"
    assert "| Alice |" in asset_result.output["content"]

    figure_asset_result = await agent.execute(_make_fetch_asset_task(bundle_id=bundle_id, doc_id=doc_id, asset_id="asset_fig_001"))
    assert figure_asset_result.status == "completed"
    assert figure_asset_result.output["figure"]["classification"]["label"] == "flow_chart"
    assert "systems architecture diagram" in figure_asset_result.output["figure"]["description"]


def test_docling_adapter_builds_structure_aware_chunks() -> None:
    adapter = DoclingAdapter()
    markdown = (
        "# Strategy Review\n\n"
        "First paragraph stays whole and ends with a full sentence. Another sentence closes this block.\n\n"
        "Second paragraph carries the next complete idea and should stay intact for retrieval quality.\n\n"
        "[FIGURE id=fig_001 asset_id=asset_fig_001 page=1 caption=\"Architecture\"]\n\n"
        "Figure description follows as its own paragraph so the chunker should preserve it cleanly."
    )
    section = {
        "section_id": "sec_strategy",
        "title": "Strategy Review",
        "start_char": 0,
        "end_char": len(markdown),
        "text": markdown.strip(),
    }
    chunks = adapter._chunk_section(
        artifact_id="art_test_001",
        markdown=markdown,
        section=section,
        max_chunk_chars=140,
        chunk_overlap_chars=40,
    )
    assert len(chunks) >= 2
    assert chunks[0]["text"].endswith(".")
    assert any("Figure description follows" in chunk["text"] for chunk in chunks)
    assert all("block_kinds" in chunk for chunk in chunks)


def test_docling_adapter_enriches_chunk_search_text_with_figures_and_tables() -> None:
    adapter = DoclingAdapter()
    markdown = (
        "[PAGE 1]\n\n"
        "# Strategy Review\n\n"
        "[TABLE id=tbl_001 asset_id=asset_tbl_001 page=1 title=\"Ownership Table\"]\n\n"
        "[FIGURE id=fig_001 asset_id=asset_fig_001 page=1 caption=\"Architecture\"]\n\n"
        "Gateway and memory coordination summary."
    )
    page_entries = [{"page_id": "page_0001", "page_number": 1, "start_char": 0, "end_char": len(markdown), "anchor_id": "page_0001"}]
    figure_start = markdown.index("[FIGURE id=fig_001")
    table_start = markdown.index("[TABLE id=tbl_001")
    chunk_index = adapter._build_chunk_index(
        artifact_id="art_test_002",
        markdown=markdown,
        max_chunk_chars=400,
        chunk_overlap_chars=40,
        page_entries=page_entries,
        slide_entries=[],
        figure_entries=[
            {
                "figure_id": "fig_001",
                "asset_id": "asset_fig_001",
                "caption": "Architecture",
                "description": "A systems architecture diagram connecting gateway, orchestrator, and memory.",
                "classification": {"label": "flow_chart", "confidence": 0.98},
                "page_number": 1,
                "start_char": figure_start,
                "end_char": figure_start + len("[FIGURE id=fig_001 asset_id=asset_fig_001 page=1 caption=\"Architecture\"]"),
            }
        ],
        table_entries=[
            {
                "table_id": "tbl_001",
                "asset_id": "asset_tbl_001",
                "title": "Ownership Table",
                "text_excerpt": "| Owner | Alice |",
                "page_number": 1,
                "start_char": table_start,
                "end_char": table_start + len("[TABLE id=tbl_001 asset_id=asset_tbl_001 page=1 title=\"Ownership Table\"]"),
            }
        ],
        asset_entries=[],
    )
    search_text = " ".join(item.get("search_text", "") for item in chunk_index["chunks"])
    assert "systems architecture diagram" in search_text.lower()
    assert "ownership table" in search_text.lower()


@pytest.mark.asyncio
async def test_docs_parser_agent_accepts_gateway_logical_runs_artifacts_path(tmp_path: Path) -> None:
    source_root = tmp_path / "runs" / "artifacts" / "req_ingest_002" / "inputs" / "art_pdf_002"
    source_root.mkdir(parents=True, exist_ok=True)
    source_file = source_root / "partners.pdf"
    source_file.write_bytes(b"%PDF-1.7 fake partners pdf")
    sha256 = hashlib.sha256(source_file.read_bytes()).hexdigest()

    parser = StubParser()
    agent = DocsParserAgent(
        redis_client=FakeRedis(),
        config=DocsParserConfig(redis_url="redis://unused", gateway_url="http://gateway", gateway_internal_token="internal-token"),
        parser=parser,
        store_root=tmp_path / "store",
        runtime_root=tmp_path / "runtime",
        artifacts_root=tmp_path / "runs" / "artifacts",
        agent_secret="agent-secret",
    )
    await agent.on_startup()
    try:
        result = await agent.execute(
            _make_task(
                payload={"bundle_label": "Partners", "ocr_mode": "auto"},
                input_artifacts=[
                    {
                        "artifact_id": "art_pdf_002",
                        "path": "runs/artifacts/req_ingest_002/inputs/art_pdf_002/partners.pdf",
                        "mime": "application/pdf",
                        "filename": "partners.pdf",
                        "sha256": sha256,
                    }
                ],
            )
        )
    finally:
        await agent.stop()

    assert result.status == "completed"
    assert parser.calls
    assert parser.calls[0]["file_path"] == str(source_file.resolve())


@pytest.mark.asyncio
async def test_docs_parser_agent_rejects_missing_or_unsupported_artifacts(tmp_path: Path) -> None:
    agent = DocsParserAgent(
        redis_client=FakeRedis(),
        config=DocsParserConfig(redis_url="redis://unused", gateway_url="http://gateway", gateway_internal_token="internal-token"),
        parser=StubParser(),
        store_root=tmp_path / "store",
        runtime_root=tmp_path / "runtime",
        artifacts_root=tmp_path / "runs" / "artifacts",
        agent_secret="agent-secret",
    )
    await agent.on_startup()
    try:
        missing_result = await agent.execute(
            _make_task(
                payload={},
                input_artifacts=[
                    {
                        "artifact_id": "art_missing_001",
                        "path": str(tmp_path / "runs" / "artifacts" / "missing.pdf"),
                        "mime": "application/pdf",
                        "filename": "missing.pdf",
                    }
                ],
            )
        )
        unsupported_result = await agent.execute(
            _make_task(
                payload={},
                input_artifacts=[
                    {
                        "artifact_id": "art_txt_001",
                        "path": "note.txt",
                        "mime": "text/plain",
                        "filename": "note.txt",
                    }
                ],
            )
        )
    finally:
        await agent.stop()

    assert missing_result.status == "failed"
    assert missing_result.error is not None
    assert missing_result.error.code == "MISSING_ARTIFACT"

    assert unsupported_result.status == "failed"
    assert unsupported_result.error is not None
    assert unsupported_result.error.code == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_docs_parser_agent_rejects_oversized_artifacts_before_parse(tmp_path: Path) -> None:
    source_root = tmp_path / "runs" / "artifacts" / "req_ingest_003" / "inputs" / "art_pdf_003"
    source_root.mkdir(parents=True, exist_ok=True)
    source_file = source_root / "oversized.pdf"
    source_file.write_bytes(b"x" * 9)

    parser = StubParser()
    agent = DocsParserAgent(
        redis_client=FakeRedis(),
        config=DocsParserConfig(
            redis_url="redis://unused",
            gateway_url="http://gateway",
            gateway_internal_token="internal-token",
            max_input_file_bytes=8,
        ),
        parser=parser,
        store_root=tmp_path / "store",
        runtime_root=tmp_path / "runtime",
        artifacts_root=tmp_path / "runs" / "artifacts",
        agent_secret="agent-secret",
    )
    await agent.on_startup()
    try:
        result = await agent.execute(
            _make_task(
                payload={},
                input_artifacts=[
                    {
                        "artifact_id": "art_pdf_003",
                        "path": str(source_file),
                        "mime": "application/pdf",
                        "filename": "oversized.pdf",
                    }
                ],
            )
        )
    finally:
        await agent.stop()

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "INVALID_INPUT"
    assert "8 B docs parsing limit" in result.error.message
    assert parser.calls == []


@pytest.mark.asyncio
async def test_docs_parser_agent_classifies_docling_limit_failures_as_invalid_input(tmp_path: Path) -> None:
    source_root = tmp_path / "runs" / "artifacts" / "req_ingest_004" / "inputs" / "art_pdf_004"
    source_root.mkdir(parents=True, exist_ok=True)
    source_file = source_root / "long.pdf"
    source_file.write_bytes(b"%PDF-1.7 fake long pdf")

    agent = DocsParserAgent(
        redis_client=FakeRedis(),
        config=DocsParserConfig(redis_url="redis://unused", gateway_url="http://gateway", gateway_internal_token="internal-token"),
        parser=FailingParser("Conversion aborted because max_num_pages was exceeded."),
        store_root=tmp_path / "store",
        runtime_root=tmp_path / "runtime",
        artifacts_root=tmp_path / "runs" / "artifacts",
        agent_secret="agent-secret",
    )
    await agent.on_startup()
    try:
        result = await agent.execute(
            _make_task(
                payload={},
                input_artifacts=[
                    {
                        "artifact_id": "art_pdf_004",
                        "path": str(source_file),
                        "mime": "application/pdf",
                        "filename": "long.pdf",
                    }
                ],
            )
        )
    finally:
        await agent.stop()

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "INVALID_INPUT"
    assert result.error.retryable is False


@pytest.mark.asyncio
async def test_docs_parser_agent_falls_back_when_picture_description_fails(tmp_path: Path) -> None:
    source_root = tmp_path / "runs" / "artifacts" / "req_ingest_005" / "inputs" / "art_pdf_005"
    source_root.mkdir(parents=True, exist_ok=True)
    source_file = source_root / "visual.pdf"
    source_file.write_bytes(b"%PDF-1.7 fake visual pdf")

    parser = PictureDescriptionFallbackParser()
    agent = DocsParserAgent(
        redis_client=FakeRedis(),
        config=DocsParserConfig(
            redis_url="redis://unused",
            gateway_url="http://gateway",
            gateway_internal_token="internal-token",
            picture_description_api_key="test-openai-key",
        ),
        parser=parser,
        store_root=tmp_path / "store",
        runtime_root=tmp_path / "runtime",
        artifacts_root=tmp_path / "runs" / "artifacts",
        agent_secret="agent-secret",
    )
    await agent.on_startup()
    try:
        result = await agent.execute(
            _make_task(
                payload={},
                input_artifacts=[
                    {
                        "artifact_id": "art_pdf_005",
                        "path": str(source_file),
                        "mime": "application/pdf",
                        "filename": "visual.pdf",
                    }
                ],
            )
        )
    finally:
        await agent.stop()

    assert result.status == "completed"
    assert len(parser.calls) == 2
    assert parser.calls[0]["request"].picture_description is not None
    assert parser.calls[1]["request"].picture_description is None
    assert result.output["documents"][0]["visual_enrichment"]["picture_description_requested"] is True
    assert result.output["documents"][0]["visual_enrichment"]["picture_description_applied"] is False
    assert "timed out" in result.output["documents"][0]["visual_enrichment"]["picture_description_fallback_reason"]


@pytest.mark.asyncio
async def test_docs_parser_agent_escalates_image_heavy_pptx_through_office_render_and_full_page_vlm(tmp_path: Path) -> None:
    source_root = tmp_path / "runs" / "artifacts" / "req_ingest_006" / "inputs" / "art_pptx_001"
    source_root.mkdir(parents=True, exist_ok=True)
    source_file = source_root / "visual_deck.pptx"
    source_file.write_bytes(b"fake pptx bytes")

    parser = OfficeEscalationParser()
    renderer = FakeOfficeRenderer()
    agent = DocsParserAgent(
        redis_client=FakeRedis(),
        config=DocsParserConfig(
            redis_url="redis://unused",
            gateway_url="http://gateway",
            gateway_internal_token="internal-token",
            full_page_vlm_api_key="test-openai-key",
            picture_description_api_key="test-openai-key",
        ),
        parser=parser,
        office_renderer=renderer,
        store_root=tmp_path / "store",
        runtime_root=tmp_path / "runtime",
        artifacts_root=tmp_path / "runs" / "artifacts",
        agent_secret="agent-secret",
    )
    await agent.on_startup()
    try:
        result = await agent.execute(
            _make_task(
                payload={"bundle_label": "Visual deck"},
                input_artifacts=[
                    {
                        "artifact_id": "art_pptx_001",
                        "path": str(source_file),
                        "mime": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                        "filename": "visual_deck.pptx",
                    }
                ],
            )
        )
    finally:
        await agent.stop()

    assert result.status == "completed"
    assert len(parser.calls) == 2
    assert renderer.calls
    assert parser.calls[0]["file_path"].endswith("visual_deck.pptx")
    assert parser.calls[1]["file_path"].endswith("visual_deck.pdf")
    assert parser.calls[1]["request"].full_page_vlm is not None
    assert parser.calls[1]["request"].picture_description is None
    assert parser.calls[1]["request"].generate_page_images is True

    document = result.output["documents"][0]
    enrichment = document["visual_enrichment"]
    assert enrichment["full_page_vlm_requested"] is True
    assert enrichment["full_page_vlm_applied"] is True
    assert enrichment["office_render_requested"] is True
    assert enrichment["office_render_applied"] is True
    assert enrichment["office_render_backend"] == "fake-office-renderer"
    assert enrichment["image_heavy_analysis"]["should_escalate"] is True
    assert document["slide_count"] == 2
    assert document["paths"]["rendered_source_pdf"] is not None

    rendered_output = (
        tmp_path
        / "runs"
        / "artifacts"
        / "tsk_docs_parse_bundle"
        / "docs_parser"
        / "art_pptx_001"
        / "intermediate"
        / "rendered_source.pdf"
    )
    assert rendered_output.exists()


@pytest.mark.asyncio
async def test_docs_parser_agent_treats_pptx_as_visual_first_in_auto_mode(tmp_path: Path) -> None:
    source_root = tmp_path / "runs" / "artifacts" / "req_ingest_006b" / "inputs" / "art_pptx_auto"
    source_root.mkdir(parents=True, exist_ok=True)
    source_file = source_root / "readable_deck.pptx"
    source_file.write_bytes(b"fake pptx bytes")

    parser = TextHeavyPptxParser()
    renderer = FakeOfficeRenderer()
    agent = DocsParserAgent(
        redis_client=FakeRedis(),
        config=DocsParserConfig(
            redis_url="redis://unused",
            gateway_url="http://gateway",
            gateway_internal_token="internal-token",
            full_page_vlm_api_key="test-openai-key",
        ),
        parser=parser,
        office_renderer=renderer,
        store_root=tmp_path / "store",
        runtime_root=tmp_path / "runtime",
        artifacts_root=tmp_path / "runs" / "artifacts",
        agent_secret="agent-secret",
    )
    await agent.on_startup()
    try:
        result = await agent.execute(
            _make_task(
                payload={},
                input_artifacts=[
                    {
                        "artifact_id": "art_pptx_auto",
                        "path": str(source_file),
                        "mime": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                        "filename": "readable_deck.pptx",
                    }
                ],
            )
        )
    finally:
        await agent.stop()

    assert result.status == "completed"
    assert len(parser.calls) == 2
    assert renderer.calls
    assert parser.calls[0]["file_path"].endswith("readable_deck.pptx")
    assert parser.calls[1]["file_path"].endswith("readable_deck.pdf")
    assert parser.calls[1]["request"].full_page_vlm is not None
    enrichment = result.output["documents"][0]["visual_enrichment"]
    assert enrichment["full_page_vlm_requested"] is True
    assert enrichment["full_page_vlm_applied"] is True
    assert enrichment["image_heavy_analysis"]["should_escalate"] is True
    assert "pptx_visual_first_default" in (enrichment["image_heavy_analysis"]["reasons"] or [])


@pytest.mark.asyncio
async def test_docs_parser_agent_keeps_standard_parse_when_full_page_vlm_fails(tmp_path: Path) -> None:
    source_root = tmp_path / "runs" / "artifacts" / "req_ingest_007" / "inputs" / "art_pptx_002"
    source_root.mkdir(parents=True, exist_ok=True)
    source_file = source_root / "visual_deck_fail.pptx"
    source_file.write_bytes(b"fake pptx bytes")

    parser = OfficeEscalationParser(fail_full_page_vlm=True)
    renderer = FakeOfficeRenderer()
    agent = DocsParserAgent(
        redis_client=FakeRedis(),
        config=DocsParserConfig(
            redis_url="redis://unused",
            gateway_url="http://gateway",
            gateway_internal_token="internal-token",
            full_page_vlm_api_key="test-openai-key",
        ),
        parser=parser,
        office_renderer=renderer,
        store_root=tmp_path / "store",
        runtime_root=tmp_path / "runtime",
        artifacts_root=tmp_path / "runs" / "artifacts",
        agent_secret="agent-secret",
    )
    await agent.on_startup()
    try:
        result = await agent.execute(
            _make_task(
                payload={},
                input_artifacts=[
                    {
                        "artifact_id": "art_pptx_002",
                        "path": str(source_file),
                        "mime": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                        "filename": "visual_deck_fail.pptx",
                    }
                ],
            )
        )
    finally:
        await agent.stop()

    assert result.status == "completed"
    document = result.output["documents"][0]
    enrichment = document["visual_enrichment"]
    assert enrichment["full_page_vlm_requested"] is True
    assert enrichment["full_page_vlm_applied"] is False
    assert enrichment["office_render_applied"] is True
    assert "timed out" in (enrichment["full_page_vlm_fallback_reason"] or "")
    assert document["chunk_count"] == 1
    assert document["paths"]["rendered_source_pdf"] is not None


@pytest.mark.asyncio
async def test_docs_parser_agent_keeps_standard_parse_when_office_renderer_is_unavailable(tmp_path: Path) -> None:
    source_root = tmp_path / "runs" / "artifacts" / "req_ingest_008" / "inputs" / "art_docx_001"
    source_root.mkdir(parents=True, exist_ok=True)
    source_file = source_root / "brochure.docx"
    source_file.write_bytes(b"fake docx bytes")

    parser = OfficeEscalationParser()
    renderer = FailingOfficeRenderer("Office renderer executable was not found: soffice")
    agent = DocsParserAgent(
        redis_client=FakeRedis(),
        config=DocsParserConfig(
            redis_url="redis://unused",
            gateway_url="http://gateway",
            gateway_internal_token="internal-token",
            full_page_vlm_api_key="test-openai-key",
        ),
        parser=parser,
        office_renderer=renderer,
        store_root=tmp_path / "store",
        runtime_root=tmp_path / "runtime",
        artifacts_root=tmp_path / "runs" / "artifacts",
        agent_secret="agent-secret",
    )
    await agent.on_startup()
    try:
        result = await agent.execute(
            _make_task(
                payload={},
                input_artifacts=[
                    {
                        "artifact_id": "art_docx_001",
                        "path": str(source_file),
                        "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        "filename": "brochure.docx",
                    }
                ],
            )
        )
    finally:
        await agent.stop()

    assert result.status == "completed"
    assert len(parser.calls) == 1
    document = result.output["documents"][0]
    enrichment = document["visual_enrichment"]
    assert enrichment["full_page_vlm_requested"] is True
    assert enrichment["full_page_vlm_applied"] is False
    assert enrichment["office_render_requested"] is True
    assert enrichment["office_render_applied"] is False
    assert "soffice" in (enrichment["office_render_fallback_reason"] or "")
    assert document["paths"]["rendered_source_pdf"] is None


@pytest.mark.asyncio
async def test_docs_parser_agent_reinspects_image_asset_and_uses_cache(tmp_path: Path) -> None:
    source_root = tmp_path / "runs" / "artifacts" / "req_ingest_009" / "inputs" / "art_pdf_009"
    source_root.mkdir(parents=True, exist_ok=True)
    source_file = source_root / "strategy.pdf"
    source_file.write_bytes(b"%PDF-1.7 fake strategy pdf")
    request_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        assert request.url == httpx.URL("https://api.openai.com/v1/chat/completions")
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["model"] == "gpt-4.1-mini"
        assert payload["response_format"] == {"type": "json_object"}
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "summary": "A systems architecture diagram linking gateway, orchestrator, docs parser, and memory.",
                                    "visual_type": "diagram",
                                    "visible_text": ["gateway", "orchestrator", "memory"],
                                    "chart_observations": [],
                                    "diagram_relationships": ["Gateway routes into orchestrator and docs parser."],
                                    "design_observations": ["The gateway node is visually emphasized at the left."],
                                    "key_entities": ["gateway", "orchestrator", "docs parser", "memory"],
                                    "uncertainties": [],
                                    "confidence": "high",
                                }
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    parser = StubParser()
    agent = DocsParserAgent(
        redis_client=FakeRedis(),
        config=DocsParserConfig(
            redis_url="redis://unused",
            gateway_url="http://gateway",
            gateway_internal_token="internal-token",
            asset_reinspection_api_key="test-openai-key",
        ),
        parser=parser,
        http_client=client,
        store_root=tmp_path / "store",
        runtime_root=tmp_path / "runtime",
        artifacts_root=tmp_path / "runs" / "artifacts",
        agent_secret="agent-secret",
    )
    await agent.on_startup()
    try:
        parse_result = await agent.execute(
            _make_task(
                payload={},
                input_artifacts=[
                    {
                        "artifact_id": "art_pdf_009",
                        "path": str(source_file),
                        "mime": "application/pdf",
                        "filename": "strategy.pdf",
                    }
                ],
            )
        )
        document = parse_result.output["documents"][0]
        bundle_id = parse_result.output["bundle_id"]
        doc_id = document["doc_id"]

        first = await agent.execute(
            _make_reinspect_asset_task(
                bundle_id=bundle_id,
                doc_id=doc_id,
                asset_id="asset_fig_001",
            )
        )
        second = await agent.execute(
            _make_reinspect_asset_task(
                bundle_id=bundle_id,
                doc_id=doc_id,
                asset_id="asset_fig_001",
            )
        )
        fetch = await agent.execute(
            _make_fetch_asset_task(
                bundle_id=bundle_id,
                doc_id=doc_id,
                asset_id="asset_fig_001",
            )
        )
    finally:
        await agent.stop()
        await client.aclose()

    assert request_count == 1
    assert first.status == "completed"
    assert first.output["cached"] is False
    assert first.output["analysis"]["visual_type"] == "diagram"
    assert second.status == "completed"
    assert second.output["cached"] is True
    assert fetch.status == "completed"
    assert fetch.output["reinspection"]["visual_type"] == "diagram"
    assert fetch.output["reinspection_path"] is not None


@pytest.mark.asyncio
async def test_docs_parser_agent_rejects_reinspection_for_non_image_asset(tmp_path: Path) -> None:
    source_root = tmp_path / "runs" / "artifacts" / "req_ingest_010" / "inputs" / "art_pdf_010"
    source_root.mkdir(parents=True, exist_ok=True)
    source_file = source_root / "strategy.pdf"
    source_file.write_bytes(b"%PDF-1.7 fake strategy pdf")

    agent = DocsParserAgent(
        redis_client=FakeRedis(),
        config=DocsParserConfig(
            redis_url="redis://unused",
            gateway_url="http://gateway",
            gateway_internal_token="internal-token",
            asset_reinspection_api_key="test-openai-key",
        ),
        parser=StubParser(),
        store_root=tmp_path / "store",
        runtime_root=tmp_path / "runtime",
        artifacts_root=tmp_path / "runs" / "artifacts",
        agent_secret="agent-secret",
    )
    await agent.on_startup()
    try:
        parse_result = await agent.execute(
            _make_task(
                payload={},
                input_artifacts=[
                    {
                        "artifact_id": "art_pdf_010",
                        "path": str(source_file),
                        "mime": "application/pdf",
                        "filename": "strategy.pdf",
                    }
                ],
            )
        )
        document = parse_result.output["documents"][0]
        result = await agent.execute(
            _make_reinspect_asset_task(
                bundle_id=parse_result.output["bundle_id"],
                doc_id=document["doc_id"],
                asset_id="asset_tbl_001",
            )
        )
    finally:
        await agent.stop()

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "INVALID_INPUT"
