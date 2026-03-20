from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from agents.docs_parser.agent import DocsParserAgent
from agents.docs_parser.config import DocsParserConfig
from agents.docs_parser.docling_adapter import ParseRequest, ParsedDocument
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
    ) -> ParsedDocument:
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
                    },
                ],
                "chunks": [
                    {
                        "chunk_id": "chk_1",
                        "section_id": "sec_1",
                        "section_title": "Quarterly Strategy",
                        "text": "Overview paragraph.",
                        "doc_start_char": markdown.index("Overview paragraph."),
                        "doc_end_char": markdown.index("Overview paragraph.") + len("Overview paragraph."),
                    },
                    {
                        "chunk_id": "chk_2",
                        "section_id": "sec_2",
                        "section_title": "Key Changes",
                        "text": "Shift focus to enterprise.",
                        "doc_start_char": markdown.index("Shift focus to enterprise."),
                        "doc_end_char": markdown.index("Shift focus to enterprise.") + len("Shift focus to enterprise."),
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
    ) -> ParsedDocument:
        del file_path, artifact_id, mime_type, request
        raise RuntimeError(self.message)


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


def _make_search_task(*, bundle_id: str, query: str) -> TaskEnvelope:
    task = TaskEnvelope(
        task_id="tsk_docs_search_bundle",
        task_list_id="sess_docs",
        parent_task_id="tsk_parent",
        session_id="sess_docs",
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/docs-parser-agent:1.0.0",
        intent="docs.search_bundle",
        input={"bundle_id": bundle_id, "query": query},
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
