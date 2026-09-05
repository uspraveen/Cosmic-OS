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


def _sample_table_doc() -> dict:
    return {
        "documentId": "doc_123",
        "title": "Tracker",
        "revisionId": "rev_table",
        "body": {
            "content": [
                {
                    "startIndex": 5,
                    "endIndex": 40,
                    "table": {
                        "tableRows": [
                            {
                                "tableCells": [
                                    {
                                        "content": [
                                            {
                                                "startIndex": 10,
                                                "endIndex": 11,
                                                "paragraph": {
                                                    "elements": [
                                                        {
                                                            "startIndex": 10,
                                                            "endIndex": 11,
                                                            "textRun": {"content": "\n"},
                                                        }
                                                    ]
                                                },
                                            }
                                        ]
                                    },
                                    {
                                        "content": [
                                            {
                                                "startIndex": 20,
                                                "endIndex": 21,
                                                "paragraph": {
                                                    "elements": [
                                                        {
                                                            "startIndex": 20,
                                                            "endIndex": 21,
                                                            "textRun": {"content": "\n"},
                                                        }
                                                    ]
                                                },
                                            }
                                        ]
                                    },
                                ]
                            }
                        ]
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


def test_markdown_pipe_tables_are_split_for_native_docs_tables() -> None:
    from agents.google_docs_agent.doc_structure import split_markdown_native_blocks

    blocks = split_markdown_native_blocks(
        "# Status Key\n\n"
        "| Status | Meaning |\n"
        "|---|---|\n"
        "| To Contact | Not yet reached out |\n"
        "| Meeting Scheduled | Call booked |\n\n"
        "After the table."
    )

    assert [block["type"] for block in blocks] == ["markdown", "table", "markdown"]
    assert blocks[1]["rows"] == [
        ["Status", "Meaning"],
        ["To Contact", "Not yet reached out"],
        ["Meeting Scheduled", "Call booked"],
    ]


def test_block_map_extracts_stable_block_ids() -> None:
    from agents.google_docs_agent.doc_structure import build_block_map

    block_map = build_block_map(_sample_doc())

    assert len(block_map.blocks) == 1
    block = block_map.blocks[0]
    assert block["id"].startswith("blk_")
    assert block["style"] == "HEADING_1"
    assert block_map.get_block(block["id"]) == block
    assert block_map.get_block_by_content("Project") == block


def test_table_cell_positions_use_cell_content_start_index() -> None:
    from agents.google_docs_agent.agent import GoogleDocsAgent

    agent = object.__new__(GoogleDocsAgent)

    assert agent._table_cell_positions(_sample_table_doc(), 1, 2, min_start_index=1) == {
        (0, 0): 10,
        (0, 1): 20,
    }


def test_http_status_error_detail_preserves_google_message() -> None:
    import httpx

    from agents.google_docs_agent.agent import GoogleDocsAgent

    request = httpx.Request("POST", "https://docs.googleapis.com/v1/documents/doc_123:batchUpdate")
    response = httpx.Response(
        400,
        json={"error": {"status": "INVALID_ARGUMENT", "message": "Invalid requests[1].insertText."}},
        request=request,
    )
    exc = httpx.HTTPStatusError("bad request", request=request, response=response)

    assert (
        GoogleDocsAgent._http_status_error_detail(exc)
        == "Google Docs/Drive API error: 400: INVALID_ARGUMENT: Invalid requests[1].insertText."
    )


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
    assert "https://www.googleapis.com/auth/drive.file" in card["auth_requirements"]["docs.edit"]["scopes"]
    assert card["model_requirements"]["internal_llm"]["default_model_key"] == "openai:gpt-5.6-luna"


@pytest.mark.asyncio
async def test_google_docs_internal_llm_omits_temperature_for_gpt5_and_logs_usage() -> None:
    from agents.google_docs_agent.config import GoogleDocsAgentConfig
    from agents.google_docs_agent.internal_llm import invoke_google_docs_planner_llm

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self._payload = payload
            self.status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    class FakeHttp:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def post(self, url: str, **kwargs):
            self.calls.append({"url": url, **kwargs})
            if url.endswith("/chat/completions"):
                return FakeResponse(
                    {
                        "id": "chatcmpl_docs",
                        "model": "gpt-5-mini",
                        "usage": {
                            "prompt_tokens": 20,
                            "completion_tokens": 10,
                            "total_tokens": 30,
                        },
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(
                                        {
                                            "intent": "docs.edit",
                                            "operation": "replace_text",
                                            "params": {"old_text": "old", "new_text": "new"},
                                            "confidence": 0.91,
                                            "needs_clarification": False,
                                        }
                                    )
                                }
                            }
                        ],
                    }
                )
            return FakeResponse({"ok": True})

    cfg = GoogleDocsAgentConfig(
        gateway_url="http://gateway.local",
        gateway_internal_token="token",
        internal_llm_api_key="key",
        internal_llm_base_url="https://api.openai.com/v1",
        internal_llm_model="gpt-5-mini",
    )
    http = FakeHttp()

    result = await invoke_google_docs_planner_llm(
        cfg=cfg,
        http_client=http,  # type: ignore[arg-type]
        user_payload={"task": {"intent": "docs.edit", "input": {"query": "replace old with new"}}},
        task_context={"task_id": "tsk_docs", "session_id": "sess_docs", "channel": "desktop"},
    )

    assert result["operation"] == "replace_text"
    chat_call = http.calls[0]
    assert "temperature" not in chat_call["json"]
    usage_call = http.calls[1]
    assert usage_call["url"] == "http://gateway.local/internal/usage/log"
    assert usage_call["json"]["source_id"] == "cosmic/google-docs-agent:1.0.0"
    assert usage_call["json"]["provider"] == "openai"
    assert usage_call["json"]["model"] == "gpt-5-mini"
    assert usage_call["json"]["prompt_tokens"] == 20
    assert usage_call["json"]["completion_tokens"] == 10


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
async def test_execute_logs_google_docs_specialist_usage() -> None:
    from agents.google_docs_agent.agent import GoogleDocsAgent
    from agents.google_docs_agent.config import GoogleDocsAgentConfig

    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

    class FakeHttp:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def post(self, url: str, **kwargs):
            self.calls.append({"url": url, **kwargs})
            return FakeResponse()

    class FakeClient:
        async def list_documents(self, *, query: str, max_results: int) -> list[dict]:
            return [{"document_id": "doc_1", "title": f"{query}:{max_results}"}]

    temp_dir = Path(__file__).resolve().parent / f".tmp_{uuid.uuid4().hex}"
    agent = None
    try:
        temp_dir.mkdir(parents=True, exist_ok=False)
        http = FakeHttp()
        agent = GoogleDocsAgent(
            redis_client=MagicMock(),
            config=GoogleDocsAgentConfig(
                gateway_url="http://gateway.local",
                gateway_internal_token="token",
                enable_internal_llm=False,
            ),
            http_client=http,  # type: ignore[arg-type]
            store_root=temp_dir / "store",
            artifacts_root=temp_dir / "artifacts",
        )
        agent.auth = {"access_token": "token", "account_id": "acct_docs_1"}
        await agent.on_startup()
        agent._client = MagicMock(return_value=FakeClient())

        result = await agent.execute(_task("docs.resolve_resource", {"query": "launch", "max_results": 2}))

        assert result.status == "completed"
        usage_call = http.calls[-1]
        assert usage_call["url"] == "http://gateway.local/internal/usage/log"
        assert usage_call["json"]["source_id"] == "cosmic/google-docs-agent:1.0.0"
        assert usage_call["json"]["provider"] == "google"
        assert usage_call["json"]["model"] == "google-docs-api"
        assert usage_call["json"]["usage_kind"] == "specialist"
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


@pytest.mark.asyncio
async def test_overwrite_doc_routes_pipe_tables_to_native_table_insertion() -> None:
    from agents.google_docs_agent.agent import GoogleDocsAgent

    class FakeClient:
        def __init__(self) -> None:
            self.requests: list[dict] = []
            self.revision = 1

        async def get_document(self, document_id: str, **_: object) -> dict:
            assert document_id == "doc_123"
            return {
                "documentId": "doc_123",
                "title": "Tracker",
                "revisionId": f"rev_{self.revision}",
                "body": {"content": [{"startIndex": 1, "endIndex": 8}]},
            }

        async def batch_update(self, document_id: str, requests: list[dict], *, required_revision_id: str = "") -> dict:
            assert document_id == "doc_123"
            assert required_revision_id.startswith("rev_")
            self.requests.extend(requests)
            self.revision += 1
            return {"replies": []}

        async def get_revision_id(self, document_id: str) -> str:
            assert document_id == "doc_123"
            return f"rev_{self.revision}"

    agent = object.__new__(GoogleDocsAgent)
    inserted_tables: list[list[list[str]]] = []

    async def fake_insert_native_table_at_index(**kwargs: object) -> dict:
        inserted_tables.append(kwargs["rows"])  # type: ignore[arg-type]
        return {
            "revision_after": "rev_table",
            "requests": [
                {
                    "insertTable": {
                        "rows": len(kwargs["rows"]),  # type: ignore[arg-type]
                        "columns": len(kwargs["rows"][0]),  # type: ignore[index]
                    }
                }
            ],
        }

    agent._insert_native_table_at_index = fake_insert_native_table_at_index  # type: ignore[method-assign]
    fake = FakeClient()

    result = await agent._overwrite_document(
        client=fake,  # type: ignore[arg-type]
        task=_task("docs.edit", {}),
        document_id="doc_123",
        full_markdown_text=(
            "# Status Key\n\n"
            "| Status | Meaning |\n"
            "|---|---|\n"
            "| To Contact | Not yet reached out |\n"
            "| Meeting Scheduled | Call booked |\n\n"
            "After the table."
        ),
    )

    assert result["verification"]["native_table_count"] == 1
    assert inserted_tables == [
        [
            ["Status", "Meaning"],
            ["To Contact", "Not yet reached out"],
            ["Meeting Scheduled", "Call booked"],
        ]
    ]
    assert any("deleteContentRange" in req for req in fake.requests)
    assert any("insertText" in req for req in fake.requests)
    assert any("insertTable" in req for req in result["requests"])


@pytest.mark.asyncio
async def test_insert_native_table_uses_valid_cell_indexes_and_tolerates_style_failure() -> None:
    import httpx

    from agents.google_docs_agent.agent import GoogleDocsAgent

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[list[dict]] = []

        async def batch_update(self, document_id: str, requests: list[dict], *, required_revision_id: str = "") -> dict:
            assert document_id == "doc_123"
            assert required_revision_id.startswith("rev_")
            self.calls.append(requests)
            if any("updateTableCellStyle" in req for req in requests):
                request = httpx.Request("POST", "https://docs.googleapis.com/v1/documents/doc_123:batchUpdate")
                response = httpx.Response(
                    400,
                    json={"error": {"status": "INVALID_ARGUMENT", "message": "Invalid table style range."}},
                    request=request,
                )
                raise httpx.HTTPStatusError("bad request", request=request, response=response)
            return {"replies": []}

        async def get_document(self, document_id: str, **_: object) -> dict:
            assert document_id == "doc_123"
            return _sample_table_doc()

        async def get_revision_id(self, document_id: str) -> str:
            assert document_id == "doc_123"
            return "rev_after"

    agent = object.__new__(GoogleDocsAgent)
    fake = FakeClient()

    result = await agent._insert_native_table_at_index(
        client=fake,  # type: ignore[arg-type]
        document_id="doc_123",
        rows=[["Status", "Meaning"]],
        insertion_index=5,
        required_revision_id="rev_1",
        has_header=True,
    )

    fill_requests = [req["insertText"] for batch in fake.calls for req in batch if "insertText" in req]
    assert {req["location"]["index"] for req in fill_requests} == {10, 20}
    assert not any("updateTableCellStyle" in req for req in result["requests"])
    assert result["revision_after"] == "rev_after"


@pytest.mark.asyncio
async def test_high_level_edit_uses_internal_planner(monkeypatch: pytest.MonkeyPatch) -> None:
    from agents.google_docs_agent import agent as docs_agent_module
    from agents.google_docs_agent.agent import GoogleDocsAgent
    from agents.google_docs_agent.config import GoogleDocsAgentConfig

    class FakeClient:
        def __init__(self) -> None:
            self.required_revision_id = ""

        async def get_document(self, document_id: str, **_: object) -> dict:
            assert document_id == "doc_123"
            return _sample_doc("Old sentence\n")

        async def batch_update(self, document_id: str, requests: list[dict], *, required_revision_id: str = "") -> dict:
            assert document_id == "doc_123"
            self.required_revision_id = required_revision_id
            assert requests[0]["replaceAllText"]["containsText"]["text"] == "Old sentence"
            return {"replies": [{"replaceAllText": {"occurrencesChanged": 1}}]}

        async def get_revision_id(self, document_id: str) -> str:
            assert document_id == "doc_123"
            return "rev_2"

    async def fake_planner(**_: object) -> dict:
        return {
            "intent": "docs.edit",
            "operation": "replace_text",
            "params": {
                "old_text": "Old sentence",
                "new_text": "New sentence",
            },
            "confidence": 0.94,
            "needs_clarification": False,
        }

    monkeypatch.setattr(docs_agent_module, "invoke_google_docs_planner_llm", fake_planner)

    temp_dir = Path(__file__).resolve().parent / f".tmp_{uuid.uuid4().hex}"
    agent = None
    try:
        temp_dir.mkdir(parents=True, exist_ok=False)
        fake = FakeClient()
        agent = GoogleDocsAgent(
            redis_client=MagicMock(),
            config=GoogleDocsAgentConfig(
                internal_llm_api_key="key",
                internal_llm_base_url="https://api.openai.com/v1",
            ),
            store_root=temp_dir / "store",
            artifacts_root=temp_dir / "artifacts",
        )
        agent.auth = {"access_token": "token", "account_id": "acct_docs_1"}
        await agent.on_startup()
        agent._client = MagicMock(return_value=fake)

        result = await agent.handle_docs_edit(
            _task(
                "docs.edit",
                {
                    "operation": "auto",
                    "document_id": "doc_123",
                    "query": "Replace the old sentence with a new one.",
                },
            )
        )

        assert result.status == "completed"
        assert result.output["operation"] == "replace_text"
        assert fake.required_revision_id == "rev_1"
    finally:
        if agent is not None:
            await agent.stop()
        shutil.rmtree(temp_dir, ignore_errors=True)
