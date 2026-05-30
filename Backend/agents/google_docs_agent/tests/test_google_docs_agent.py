"""Focused tests for the Google Docs specialist agent."""

from __future__ import annotations

import json
import shutil
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BACKEND_ROOT))

from shared.contracts import TaskEnvelope, utcnow


def _task(intent: str, input_data: dict) -> TaskEnvelope:
    return TaskEnvelope(
        task_id=f"tsk_{uuid.uuid4().hex[:12]}",
        task_list_id="sess_docs",
        parent_task_id=None,
        session_id="sess_docs",
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/google-docs-agent:1.0.0",
        intent=intent,
        input=input_data,
        input_artifacts=[],
        idempotency_key=f"idem_{uuid.uuid4().hex[:12]}",
        priority="normal",
        signature="sig",
        created_at=utcnow(),
        source="user",
        source_id=None,
        channel="desktop",
    )


def _sample_doc(text: str = "Project Plan\n") -> dict:
    return {
        "documentId": "doc_123",
        "title": "Project Plan",
        "revisionId": "rev_1",
        "body": {
            "content": [
                {
                    "startIndex": 1,
                    "endIndex": 1 + len(text),
                    "paragraph": {
                        "paragraphStyle": {"namedStyleType": "HEADING_1"},
                        "elements": [
                            {
                                "startIndex": 1,
                                "endIndex": 1 + len(text),
                                "textRun": {"content": text},
                            }
                        ],
                    },
                }
            ]
        },
    }


def test_markdown_parser_preserves_useful_standalone_agent_features() -> None:
    from agents.google_docs_agent.doc_structure import MarkdownParser

    requests = MarkdownParser.parse(
        "# Heading\n"
        "Paragraph with **bold**, *italic*, __underline__, `code`, [link](https://example.com), "
        "<color:red>red</color>, and <highlight:yellow>marked</highlight>."
    )

    assert any("insertText" in req for req in requests)
    assert any(
        req.get("updateParagraphStyle", {}).get("paragraphStyle", {}).get("namedStyleType") == "HEADING_1"
        for req in requests
    )
    style_fields = [
        req.get("updateTextStyle", {}).get("fields", "")
        for req in requests
        if "updateTextStyle" in req
    ]
    assert "bold" in ",".join(style_fields)
    assert "italic" in ",".join(style_fields)
    assert "underline" in ",".join(style_fields)
    assert "link" in ",".join(style_fields)
    assert "foregroundColor" in ",".join(style_fields)
    assert "backgroundColor" in ",".join(style_fields)


def test_block_map_extracts_stable_block_ids() -> None:
    from agents.google_docs_agent.doc_structure import build_block_map

    block_map = build_block_map(_sample_doc())

    assert len(block_map.blocks) == 1
    block = block_map.blocks[0]
    assert block["id"].startswith("blk_")
    assert block["style"] == "HEADING_1"
    assert block_map.get_block(block["id"]) == block
    assert block_map.get_block_by_content("Project") == block


def test_all_google_docs_schemas_are_valid_json() -> None:
    schemas_dir = Path(__file__).resolve().parent.parent / "schemas" / "intents"
    schema_files = sorted(schemas_dir.glob("docs.*.json"))
    assert schema_files
    for path in schema_files:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data.get("type") == "object", f"Invalid root type in {path.name}"


def test_agent_card_references_all_google_docs_schemas() -> None:
    yaml = pytest.importorskip("yaml")

    agent_dir = Path(__file__).resolve().parent.parent
    card = yaml.safe_load((agent_dir / "agent_card.yaml").read_text(encoding="utf-8"))
    assert card["agent_id"] == "cosmic/google-docs-agent:1.0.0"
    intent_names = {intent["name"] for intent in card["intents"]}
    assert {
        "docs.resolve_resource",
        "docs.create",
        "docs.read",
        "docs.edit",
        "docs.recall_session",
    }.issubset(intent_names)
    schemas_dir = agent_dir / "schemas" / "intents"
    for intent in card["intents"]:
        assert (schemas_dir / intent["input_schema"].split("/")[-1]).exists()
        assert (schemas_dir / intent["output_schema"].split("/")[-1]).exists()
    assert "https://www.googleapis.com/auth/documents" in card["auth_requirements"]["docs.edit"]["scopes"]
    assert "https://www.googleapis.com/auth/drive" in card["auth_requirements"]["docs.edit"]["scopes"]


@pytest.mark.asyncio
async def test_resolve_resource_uses_selected_google_account() -> None:
    from agents.google_docs_agent.agent import GoogleDocsAgent

    class FakeClient:
        async def list_documents(self, *, query: str, max_results: int) -> list[dict]:
            assert query == "launch"
            assert max_results == 3
            return [{"document_id": "doc_1", "title": "Launch Plan"}]

    temp_dir = Path(__file__).resolve().parent / f".tmp_{uuid.uuid4().hex}"
    agent = None
    try:
        temp_dir.mkdir(parents=True, exist_ok=False)
        agent = GoogleDocsAgent(
            redis_client=MagicMock(),
            store_root=temp_dir / "store",
            artifacts_root=temp_dir / "artifacts",
        )
        agent.auth = {
            "access_token": "token",
            "account_id": "acct_docs_1",
            "account_email": "user@example.com",
        }
        await agent.on_startup()
        agent._client = MagicMock(return_value=FakeClient())

        result = await agent.handle_docs_resolve_resource(
            _task("docs.resolve_resource", {"query": "launch", "max_results": 3})
        )

        assert result.status == "completed"
        assert result.output["count"] == 1
        assert result.output["matches"][0]["document_id"] == "doc_1"
        assert result.output["matches"][0]["account_email"] == "user@example.com"
    finally:
        if agent is not None:
            await agent.stop()
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_update_block_uses_revision_guard() -> None:
    from agents.google_docs_agent.agent import GoogleDocsAgent

    class FakeClient:
        def __init__(self) -> None:
            self.required_revision_id = ""
            self.requests: list[dict] = []

        async def get_document(self, document_id: str, **_: object) -> dict:
            assert document_id == "doc_123"
            return _sample_doc()

        async def batch_update(self, document_id: str, requests: list[dict], *, required_revision_id: str = "") -> dict:
            assert document_id == "doc_123"
            self.required_revision_id = required_revision_id
            self.requests = requests
            return {"replies": []}

        async def get_revision_id(self, document_id: str) -> str:
            assert document_id == "doc_123"
            return "rev_2"

    temp_dir = Path(__file__).resolve().parent / f".tmp_{uuid.uuid4().hex}"
    agent = None
    try:
        temp_dir.mkdir(parents=True, exist_ok=False)
        fake = FakeClient()
        agent = GoogleDocsAgent(
            redis_client=MagicMock(),
            store_root=temp_dir / "store",
            artifacts_root=temp_dir / "artifacts",
        )
        agent.auth = {"access_token": "token", "account_id": "acct_docs_1"}
        await agent.on_startup()

        result = await agent._handle_update_block(
            _task(
                "docs.edit",
                {
                    "operation": "update_block",
                    "document_id": "doc_123",
                    "expected_snippet": "Project Plan",
                    "new_text": "# New Plan",
                },
            ),
            fake,
        )

        assert result.status == "completed"
        assert fake.required_revision_id == "rev_1"
        assert any("deleteContentRange" in req for req in fake.requests)
        assert any("insertText" in req for req in fake.requests)
    finally:
        if agent is not None:
            await agent.stop()
        shutil.rmtree(temp_dir, ignore_errors=True)

