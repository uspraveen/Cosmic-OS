from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agents.tabular_agent.agent import TabularAgent
from agents.tabular_agent.config import TabularAgentConfig
from shared.contracts import TaskEnvelope

BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _make_agent(tmp_path: Path) -> TabularAgent:
    cfg = TabularAgentConfig(
        gateway_internal_token="test-token",
        enable_internal_llm=False,
    )
    return TabularAgent(
        redis_client=AsyncMock(),
        config=cfg,
        registry_db_path=tmp_path / "registry.db",
        agent_root=BACKEND_ROOT / "agents" / "tabular_agent",
        artifacts_root=tmp_path / "runs" / "artifacts",
        store_root=tmp_path / "store",
        runtime_root=tmp_path / "runtime",
    )


def _make_task(agent: TabularAgent, *, input_payload: dict[str, object]) -> TaskEnvelope:
    return TaskEnvelope(
        task_id="tsk_create_workbook_test",
        task_list_id="req_test_create_workbook",
        session_id="sess_tabular_create",
        sender="cosmic/orchestrator:1.0.0",
        recipient=agent.agent_id,
        intent=agent.CREATE_WORKBOOK,
        input=input_payload,
        input_artifacts=[],
        idempotency_key="idem_tabular_create_workbook",
        signature="test-signature",
        source="user",
        source_id="src_test_create_workbook",
        channel="desktop:test",
    )


@pytest.mark.asyncio
async def test_create_workbook_creates_bundle_and_xlsx_artifact(tmp_path: Path) -> None:
    agent = _make_agent(tmp_path)
    await agent.on_startup()
    agent._emit_stage = AsyncMock()  # type: ignore[method-assign]

    task = _make_task(
        agent,
        input_payload={
            "filename": "YC_Spring_2026_Companies.xlsx",
            "sheets": [
                {
                    "sheet_id": "yc_companies",
                    "display_name": "YC Companies",
                    "columns": ["#", "Company", "Batch"],
                    "rows": [
                        {"#": 1, "Company": "Alpha", "Batch": "X26"},
                        {"#": 2, "Company": "Beta", "Batch": "X26"},
                    ],
                }
            ],
        },
    )

    result = await agent._handle_create_workbook(task)

    assert result.status == "completed"
    assert result.error is None
    assert result.output["bundle_id"].startswith("bundle_")
    assert result.output["workbook_count"] == 1
    assert result.output["created_workbook"]["filename"] == "YC_Spring_2026_Companies.xlsx"

    workbook = result.output["workbooks"][0]
    bundle_root = tmp_path / "runs" / "artifacts" / task.task_id / "parsed" / workbook["artifact_id"]
    assert (bundle_root / "manifest.json").is_file()
    assert (bundle_root / "workbook_manifest.json").is_file()
    assert (bundle_root / "sheet_catalog.json").is_file()
    assert (bundle_root / "preview.md").is_file()
    assert (bundle_root / "bundle.duckdb").is_file()
    assert (bundle_root / "sheets" / "yc_companies.parquet").is_file()
    assert (bundle_root / "generated" / "YC_Spring_2026_Companies.xlsx").is_file()

    workbook_manifest = json.loads((bundle_root / "workbook_manifest.json").read_text(encoding="utf-8"))
    assert workbook_manifest["origin"] == "user_created"
    assert workbook_manifest["sheet_count"] == 1

    sheet_catalog = json.loads((bundle_root / "sheet_catalog.json").read_text(encoding="utf-8"))
    assert sheet_catalog["sheets"][0]["sheet_id"] == "yc_companies"
    assert sheet_catalog["sheets"][0]["display_name"] == "YC Companies"

    assert len(result.artifacts) == 1
    assert result.artifacts[0].mime == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert result.artifacts[0].path.endswith("/generated/YC_Spring_2026_Companies.xlsx")

    stored = agent._load_bundle(result.output["bundle_id"])
    assert stored["bundle_id"] == result.output["bundle_id"]
    assert stored["workbooks"][0]["artifact_id"] == workbook["artifact_id"]


@pytest.mark.asyncio
async def test_create_workbook_supports_array_rows_and_filename_auto_suffix(tmp_path: Path) -> None:
    agent = _make_agent(tmp_path)
    await agent.on_startup()
    agent._emit_stage = AsyncMock()  # type: ignore[method-assign]

    task = _make_task(
        agent,
        input_payload={
            "filename": "Simple_Output",
            "sheets": [
                {
                    "display_name": "Forecast",
                    "columns": ["Month", "Revenue"],
                    "rows": [
                        ["Jan", 1000],
                        ["Feb", 1200],
                    ],
                }
            ],
        },
    )

    result = await agent._handle_create_workbook(task)

    assert result.status == "completed"
    assert result.output["created_workbook"]["filename"] == "Simple_Output.xlsx"
    workbook = result.output["workbooks"][0]
    assert workbook["sheet_count"] == 1
    assert workbook["parsed_sheet_count"] == 1
    assert workbook["notable_tabs"] == ["Forecast"]
    assert result.artifacts[0].path.endswith("/generated/Simple_Output.xlsx")
