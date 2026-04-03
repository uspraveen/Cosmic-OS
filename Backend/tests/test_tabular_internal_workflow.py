from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agents.tabular_agent import internal_workflow as iw
from agents.tabular_agent import tabular_reason_graph as trg
from agents.tabular_agent.skills import discover_skills, load_skill_content


class _StepPlanSpy:
    def __init__(self) -> None:
        self.created_steps: list[list[str]] = []
        self.updates: list[tuple[int, str, str | None]] = []

    async def create(self, steps: list[str]) -> dict:
        self.created_steps.append(list(steps))
        return {"plan_active": True, "total_steps": len(steps), "steps": steps}

    async def update(self, step: int, status: str, note: str | None = None) -> dict:
        self.updates.append((step, status, note))
        return {"step": step, "status": status, "note": note}


def test_extract_json_object_fenced() -> None:
    raw = 'Here:\n```json\n{"mode": "sql", "sql": "select 1", "python_code": null, "rationale": "x"}\n```'
    out = iw.extract_json_object(raw)
    assert out is not None
    assert out.get("mode") == "sql"


def test_extract_json_object_plain() -> None:
    raw = '{"mode":"python","sql":null,"python_code":"import duckdb","rationale":"y"}'
    out = iw.extract_json_object(raw)
    assert out is not None
    assert out.get("mode") == "python"


@pytest.mark.asyncio
async def test_run_tabular_reason_workbook_sql_happy_path(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "preview.md").write_text("# t\n", encoding="utf-8")
    (tmp_path / "sheet_catalog.json").write_text('{"sheets":[]}', encoding="utf-8")

    class _Agent:
        def _require(self, x: str) -> str:
            return x

        def _safe(self, x) -> str:
            return str(x or "").strip()

        async def _emit_stage(self, *a, **k) -> None:
            return None

        def _bundle_disk_path(self, bundle_id: str, artifact_id: str) -> Path:
            return tmp_path

        def sync_run_select(self, bundle_id: str, artifact_id: str, sql: str) -> dict:
            return {"row_count": 1, "rows": [{"x": 1}], "columns": ["x"], "truncated": False}

    agent = _Agent()
    task = type(
        "T",
        (),
        {
            "task_id": "task_x",
            "session_id": "sess_x",
            "input": {"bundle_id": "b1", "artifact_id": "a1", "goal": "count rows"},
            "source": "user",
            "source_id": "u1",
            "channel": "desktop:test",
        },
    )()

    cfg = type(
        "C",
        (),
        {
            "enable_internal_llm": True,
            "mimo_api_key": "k",
            "mimo_base_url": "https://x/v1",
            "include_financial_fpna_prompt": False,
            "sandbox_timeout_sec": 30.0,
            "tabular_reason_use_langgraph": False,
            "mimo_model": "m",
            "mimo_timeout_sec": 30.0,
        },
    )()

    calls: list[str] = []

    async def fake_invoke(*, operation: str, **kwargs) -> str:
        calls.append(operation)
        if operation == "tabular.internal_llm.reason_plan":
            return '{"mode":"sql","sql":"select 1","python_code":null,"rationale":"test"}'
        return "Summary line."

    monkeypatch.setattr(iw, "invoke_tabular_mimo", fake_invoke)

    http = AsyncMock()
    out = await iw.run_tabular_reason_workbook(agent=agent, task=task, http_client=http, cfg=cfg)  # type: ignore[arg-type]
    assert "summary" in out
    assert out.get("execution", {}).get("row_count") == 1
    assert "tabular.internal_llm.reason_plan" in calls
    assert "tabular.internal_llm.reason_answer" in calls


@pytest.mark.asyncio
async def test_langgraph_finishes_on_done_action(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "preview.md").write_text("# t\n", encoding="utf-8")
    (tmp_path / "sheet_catalog.json").write_text('{"sheets":[]}', encoding="utf-8")

    class _Agent:
        def __init__(self) -> None:
            self.step_plan = _StepPlanSpy()

        def _require(self, x: str) -> str:
            return x

        def _safe(self, x) -> str:
            return str(x or "").strip()

        async def _emit_stage(self, *a, **k) -> None:
            return None

        def _bundle_disk_path(self, bundle_id: str, artifact_id: str) -> Path:
            return tmp_path

    agent = _Agent()
    task = type(
        "T",
        (),
        {
            "task_id": "task_g",
            "session_id": "sess_g",
            "input": {"bundle_id": "b1", "artifact_id": "a1", "goal": "smoke"},
            "source": "user",
            "source_id": "u1",
            "channel": "desktop:test",
        },
    )()

    cfg = type(
        "C",
        (),
        {
            "enable_internal_llm": True,
            "mimo_api_key": "k",
            "mimo_base_url": "https://x/v1",
            "include_financial_fpna_prompt": False,
            "sandbox_timeout_sec": 30.0,
            "tabular_reason_use_langgraph": True,
            "tabular_reason_max_tool_rounds": 3,
            "mimo_model": "m",
            "mimo_timeout_sec": 30.0,
        },
    )()

    async def fake_invoke(*, operation: str, **kwargs) -> str:
        if operation == "tabular.internal_llm.reason_step":
            return '{"action":"done","answer":"ok","rationale":"done"}'
        if operation == "tabular.internal_llm.reason_answer":
            return "Wrapped up."
        return ""

    monkeypatch.setattr(trg, "invoke_tabular_mimo", fake_invoke)

    out = await iw.run_tabular_reason_workbook(agent=agent, task=task, http_client=AsyncMock(), cfg=cfg)  # type: ignore[arg-type]
    assert out.get("workflow") == "langgraph"
    assert out.get("response")
    # With dynamic planning, a simple "done" action skips plan creation entirely
    # (per §32.4: skip planning for 1-2 obvious steps)
    assert agent.step_plan.created_steps == []
    assert agent.step_plan.updates == []


@pytest.mark.asyncio
async def test_run_tabular_reason_workbook_disabled_llm() -> None:
    class _A:
        def _require(self, x: str) -> str:
            return x

        def _safe(self, x) -> str:
            return str(x or "").strip()

    agent = _A()
    task = type("T", (), {"task_id": "t", "session_id": "s", "input": {"bundle_id": "b", "artifact_id": "a", "goal": "g"}, "source": None, "source_id": None, "channel": None})()
    cfg = type("C", (), {"enable_internal_llm": False, "mimo_api_key": "", "mimo_base_url": "", "include_financial_fpna_prompt": False, "sandbox_timeout_sec": 1.0})()
    out = await iw.run_tabular_reason_workbook(agent=agent, task=task, http_client=AsyncMock(), cfg=cfg)  # type: ignore[arg-type]
    assert out.get("error") == "internal_llm_disabled"
    assert out.get("error_code") == "FEATURE_DISABLED"


@pytest.mark.asyncio
async def test_langgraph_sql_step_then_done(monkeypatch, tmp_path: Path) -> None:
    import duckdb

    (tmp_path / "preview.md").write_text("# t\n", encoding="utf-8")
    (tmp_path / "sheet_catalog.json").write_text('{"sheets":[]}', encoding="utf-8")
    con = duckdb.connect(str(tmp_path / "bundle.duckdb"))
    con.execute("CREATE VIEW s_t AS SELECT 42 AS v")
    con.close()

    class _Agent:
        def _require(self, x: str) -> str:
            return x

        def _safe(self, x) -> str:
            return str(x or "").strip()

        async def _emit_stage(self, *a, **k) -> None:
            return None

        def _bundle_disk_path(self, bundle_id: str, artifact_id: str) -> Path:
            return tmp_path

        def sync_run_select(self, bundle_id: str, artifact_id: str, sql: str) -> dict:
            import duckdb as d

            db = tmp_path / "bundle.duckdb"
            c = d.connect(str(db))
            try:
                df = c.execute(sql).fetchdf()
            finally:
                c.close()
            return {"row_count": len(df), "rows": df.to_dict(orient="records"), "columns": list(df.columns), "truncated": False}

    agent = _Agent()
    task = type(
        "T",
        (),
        {
            "task_id": "task_sql",
            "session_id": "sess_x",
            "input": {"bundle_id": "b1", "artifact_id": "a1", "goal": "get v"},
            "source": "user",
            "source_id": "u1",
            "channel": "desktop:test",
        },
    )()

    cfg = type(
        "C",
        (),
        {
            "enable_internal_llm": True,
            "mimo_api_key": "k",
            "mimo_base_url": "https://x/v1",
            "include_financial_fpna_prompt": False,
            "sandbox_timeout_sec": 30.0,
            "tabular_reason_use_langgraph": True,
            "tabular_reason_max_tool_rounds": 5,
            "mimo_model": "m",
            "mimo_timeout_sec": 30.0,
        },
    )()

    step = {"n": 0}

    async def fake_invoke(*, operation: str, **kwargs) -> str:
        if operation == "tabular.internal_llm.reason_step":
            step["n"] += 1
            if step["n"] == 1:
                return '{"action":"sql","sql":"select * from s_t","rationale":"read view"}'
            return '{"action":"done","answer":"42","rationale":"done"}'
        if operation == "tabular.internal_llm.reason_answer":
            return "Got rows."
        return ""

    monkeypatch.setattr(trg, "invoke_tabular_mimo", fake_invoke)

    out = await iw.run_tabular_reason_workbook(agent=agent, task=task, http_client=AsyncMock(), cfg=cfg)  # type: ignore[arg-type]
    assert out.get("workflow") == "langgraph"
    assert step["n"] >= 2
    steps = out.get("steps") or []
    kinds = [s.get("kind") for s in steps if isinstance(s, dict)]
    assert "sql" in kinds


@pytest.mark.asyncio
async def test_langgraph_max_tool_rounds(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "preview.md").write_text("# t\n", encoding="utf-8")
    (tmp_path / "sheet_catalog.json").write_text('{"sheets":[]}', encoding="utf-8")

    class _Agent:
        def _require(self, x: str) -> str:
            return x

        def _safe(self, x) -> str:
            return str(x or "").strip()

        async def _emit_stage(self, *a, **k) -> None:
            return None

        def _bundle_disk_path(self, bundle_id: str, artifact_id: str) -> Path:
            return tmp_path

        def sync_run_select(self, bundle_id: str, artifact_id: str, sql: str) -> dict:
            return {"row_count": 0, "rows": [], "columns": [], "truncated": False}

    agent = _Agent()
    task = type(
        "T",
        (),
        {
            "task_id": "task_max",
            "session_id": "sess_x",
            "input": {"bundle_id": "b1", "artifact_id": "a1", "goal": "loop"},
            "source": "user",
            "source_id": "u1",
            "channel": "desktop:test",
        },
    )()

    cfg = type(
        "C",
        (),
        {
            "enable_internal_llm": True,
            "mimo_api_key": "k",
            "mimo_base_url": "https://x/v1",
            "include_financial_fpna_prompt": False,
            "sandbox_timeout_sec": 30.0,
            "tabular_reason_use_langgraph": True,
            "tabular_reason_max_tool_rounds": 2,
            "mimo_model": "m",
            "mimo_timeout_sec": 30.0,
        },
    )()

    async def fake_invoke(*, operation: str, **kwargs) -> str:
        if operation == "tabular.internal_llm.reason_step":
            return '{"action":"sql","sql":"select 1","rationale":"more"}'
        if operation == "tabular.internal_llm.reason_answer":
            return "Stopped."
        return ""

    monkeypatch.setattr(trg, "invoke_tabular_mimo", fake_invoke)

    out = await iw.run_tabular_reason_workbook(agent=agent, task=task, http_client=AsyncMock(), cfg=cfg)  # type: ignore[arg-type]
    assert out.get("finish_reason") == "max_tool_rounds" or out.get("workflow") == "langgraph"
    tool_steps = [s for s in (out.get("steps") or []) if isinstance(s, dict) and s.get("step") == "tool"]
    assert len(tool_steps) <= 2


@pytest.mark.asyncio
async def test_langgraph_python_writes_receipt(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "preview.md").write_text("# t\n", encoding="utf-8")
    (tmp_path / "sheet_catalog.json").write_text('{"sheets":[]}', encoding="utf-8")

    class _Agent:
        def _require(self, x: str) -> str:
            return x

        def _safe(self, x) -> str:
            return str(x or "").strip()

        async def _emit_stage(self, *a, **k) -> None:
            return None

        def _bundle_disk_path(self, bundle_id: str, artifact_id: str) -> Path:
            return tmp_path

    agent = _Agent()
    task = type(
        "T",
        (),
        {
            "task_id": "task_py",
            "session_id": "sess_x",
            "input": {"bundle_id": "b1", "artifact_id": "a1", "goal": "run"},
            "source": "user",
            "source_id": "u1",
            "channel": "desktop:test",
        },
    )()

    cfg = type(
        "C",
        (),
        {
            "enable_internal_llm": True,
            "mimo_api_key": "k",
            "mimo_base_url": "https://x/v1",
            "include_financial_fpna_prompt": False,
            "sandbox_timeout_sec": 30.0,
            "tabular_reason_use_langgraph": True,
            "tabular_reason_max_tool_rounds": 3,
            "mimo_model": "m",
            "mimo_timeout_sec": 30.0,
        },
    )()

    n = {"v": 0}

    async def fake_invoke(*, operation: str, **kwargs) -> str:
        if operation == "tabular.internal_llm.reason_step":
            n["v"] += 1
            if n["v"] == 1:
                return '{"action":"python","python_code":"print(\\\"ok\\\")","rationale":"p"}'
            return '{"action":"done","answer":"ok","rationale":"d"}'
        if operation == "tabular.internal_llm.reason_answer":
            return "Done."
        return ""

    monkeypatch.setattr(trg, "invoke_tabular_mimo", fake_invoke)

    out = await iw.run_tabular_reason_workbook(agent=agent, task=task, http_client=AsyncMock(), cfg=cfg)  # type: ignore[arg-type]
    assert (tmp_path / "executions").is_dir()
    json_files = list((tmp_path / "executions").glob("*.json"))
    assert json_files, "execution receipt should be persisted"
    assert out.get("last_tool_result", {}).get("exit_code") == 0


@pytest.mark.asyncio
async def test_langgraph_clarify_uses_orchestrator_task_input(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "preview.md").write_text("# t\n", encoding="utf-8")
    (tmp_path / "sheet_catalog.json").write_text('{"sheets":[]}', encoding="utf-8")

    orch_calls: list[dict] = []
    events: list[tuple[str, str, dict]] = []

    class _Agent:
        agent_id = "cosmic/tabular-agent:1.0.0"

        def _require(self, x: str) -> str:
            return x

        def _safe(self, x) -> str:
            return str(x or "").strip()

        async def _emit_stage(self, *a, **k) -> None:
            return None

        async def emit_event(self, task_id: str, event_type: str, payload: dict) -> str:
            events.append((task_id, event_type, payload))
            return "mid"

        def _bundle_disk_path(self, bundle_id: str, artifact_id: str) -> Path:
            return tmp_path

    agent = _Agent()
    task = type(
        "T",
        (),
        {
            "task_id": "child_tsk",
            "parent_task_id": "parent_orch",
            "session_id": "sess_x",
            "input": {"bundle_id": "b1", "artifact_id": "a1", "goal": "ambiguous", "request_id": "req_1"},
            "source": "orchestrator",
            "source_id": "cosmic/orchestrator:1.0.0",
            "channel": "desktop:test",
        },
    )()

    cfg = type(
        "C",
        (),
        {
            "enable_internal_llm": True,
            "mimo_api_key": "k",
            "mimo_base_url": "https://x/v1",
            "include_financial_fpna_prompt": False,
            "sandbox_timeout_sec": 30.0,
            "tabular_reason_use_langgraph": True,
            "tabular_reason_max_tool_rounds": 5,
            "tabular_reason_clarify_wait_sec": 30.0,
            "orchestrator_url": "http://127.0.0.1:8743",
            "orchestrator_internal_token": "tok",
            "mimo_model": "m",
            "mimo_timeout_sec": 30.0,
        },
    )()

    step = {"n": 0}

    async def fake_invoke(*, operation: str, **kwargs) -> str:
        if operation == "tabular.internal_llm.reason_step":
            step["n"] += 1
            return (
                '{"action":"clarify","question":"Which sheet?","options":["A","B"],'
                '"ambiguity":"multiple_sheets","rationale":"ask"}'
            )
        if operation == "tabular.internal_llm.reason_answer":
            return "Final."
        return ""

    async def fake_orch(**kwargs) -> dict:
        orch_calls.append(dict(kwargs))
        return {"input_request_id": "uir_1", "ok": True}

    monkeypatch.setattr(trg, "invoke_tabular_mimo", fake_invoke)
    monkeypatch.setattr(trg, "request_orchestrator_task_input", fake_orch)

    out = await iw.run_tabular_reason_workbook(agent=agent, task=task, http_client=AsyncMock(), cfg=cfg)  # type: ignore[arg-type]
    assert out.get("workflow") == "langgraph"
    assert out.get("clarify_used") is True
    assert out.get("suspended") is True
    assert out.get("finish_reason") == "awaiting_clarification"
    assert out.get("input_request_id") == "uir_1"
    assert len(orch_calls) == 1
    assert orch_calls[0]["parent_task_id"] == "parent_orch"
    assert orch_calls[0]["question"] == "Which sheet?"
    et = [e[1] for e in events]
    assert "task.suspended" in et
    assert "task.resumed" not in et
    assert events[0][0] == "child_tsk"
    last_tr = orch_calls[0]
    assert last_tr["channel"] == "desktop:test"


@pytest.mark.asyncio
async def test_langgraph_clarify_without_parent_emits_no_orchestrator_call(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "preview.md").write_text("# t\n", encoding="utf-8")
    (tmp_path / "sheet_catalog.json").write_text('{"sheets":[]}', encoding="utf-8")

    orch_calls: list[dict] = []

    class _Agent:
        agent_id = "cosmic/tabular-agent:1.0.0"

        def _require(self, x: str) -> str:
            return x

        def _safe(self, x) -> str:
            return str(x or "").strip()

        async def _emit_stage(self, *a, **k) -> None:
            return None

        async def emit_event(self, *a, **k) -> str:
            return "mid"

        def _bundle_disk_path(self, bundle_id: str, artifact_id: str) -> Path:
            return tmp_path

    agent = _Agent()
    task = type(
        "T",
        (),
        {
            "task_id": "child_only",
            "session_id": "sess_x",
            "input": {"bundle_id": "b1", "artifact_id": "a1", "goal": "x"},
            "source": "user",
            "source_id": "u1",
            "channel": None,
        },
    )()

    cfg = type(
        "C",
        (),
        {
            "enable_internal_llm": True,
            "mimo_api_key": "k",
            "mimo_base_url": "https://x/v1",
            "include_financial_fpna_prompt": False,
            "sandbox_timeout_sec": 30.0,
            "tabular_reason_use_langgraph": True,
            "tabular_reason_max_tool_rounds": 5,
            "mimo_model": "m",
            "mimo_timeout_sec": 30.0,
        },
    )()

    step_n = {"n": 0}

    async def fake_invoke(*, operation: str, **kwargs) -> str:
        if operation == "tabular.internal_llm.reason_step":
            step_n["n"] += 1
            if step_n["n"] == 1:
                return '{"action":"clarify","question":"Q?","options":[],"rationale":"c"}'
            return '{"action":"done","answer":"Cannot reach user without parent task.","rationale":"d"}'
        if operation == "tabular.internal_llm.reason_answer":
            return "Stopped without user."
        return ""

    async def fake_orch(**kwargs) -> dict:
        orch_calls.append(dict(kwargs))
        return {"status": "answered", "reply": {"content": "n/a"}}

    monkeypatch.setattr(trg, "invoke_tabular_mimo", fake_invoke)
    monkeypatch.setattr(trg, "request_orchestrator_task_input", fake_orch)

    out = await iw.run_tabular_reason_workbook(agent=agent, task=task, http_client=AsyncMock(), cfg=cfg)  # type: ignore[arg-type]
    assert len(orch_calls) == 0
    assert out.get("clarify_used") is True
    assert "clarify_requires_parent_task_id" in json.dumps(out.get("last_tool_result") or {})


@pytest.mark.asyncio
async def test_langgraph_second_clarify_finalizes_with_clarify_repeat(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "preview.md").write_text("# t\n", encoding="utf-8")
    (tmp_path / "sheet_catalog.json").write_text('{"sheets":[]}', encoding="utf-8")

    orch_calls: list[dict] = []

    class _Agent:
        agent_id = "cosmic/tabular-agent:1.0.0"

        def _require(self, x: str) -> str:
            return x

        def _safe(self, x) -> str:
            return str(x or "").strip()

        async def _emit_stage(self, *a, **k) -> None:
            return None

        async def emit_event(self, *a, **k) -> str:
            return "mid"

        def _bundle_disk_path(self, bundle_id: str, artifact_id: str) -> Path:
            return tmp_path

    agent = _Agent()
    task = type(
        "T",
        (),
        {
            "task_id": "child_tsk2",
            "parent_task_id": "parent_orch2",
            "session_id": "sess_x",
            "input": {"bundle_id": "b1", "artifact_id": "a1", "goal": "x"},
            "source": "orchestrator",
            "source_id": "cosmic/orchestrator:1.0.0",
            "channel": "desktop:t",
        },
    )()

    cfg = type(
        "C",
        (),
        {
            "enable_internal_llm": True,
            "mimo_api_key": "k",
            "mimo_base_url": "https://x/v1",
            "include_financial_fpna_prompt": False,
            "sandbox_timeout_sec": 30.0,
            "tabular_reason_use_langgraph": True,
            "tabular_reason_max_tool_rounds": 5,
            "tabular_reason_clarify_wait_sec": 30.0,
            "orchestrator_url": "http://127.0.0.1:8743",
            "orchestrator_internal_token": "tok",
            "mimo_model": "m",
            "mimo_timeout_sec": 30.0,
        },
    )()

    step = {"n": 0}

    async def fake_invoke(*, operation: str, **kwargs) -> str:
        if operation == "tabular.internal_llm.reason_step":
            step["n"] += 1
            return '{"action":"clarify","question":"Again?","options":[],"rationale":"x"}'
        if operation == "tabular.internal_llm.reason_answer":
            return "Summary."
        return ""

    async def fake_orch(**kwargs) -> dict:
        orch_calls.append(dict(kwargs))
        return {"input_request_id": "uir_2", "ok": True}

    monkeypatch.setattr(trg, "invoke_tabular_mimo", fake_invoke)
    monkeypatch.setattr(trg, "request_orchestrator_task_input", fake_orch)

    first = await iw.run_tabular_reason_workbook(agent=agent, task=task, http_client=AsyncMock(), cfg=cfg)  # type: ignore[arg-type]
    assert first.get("suspended") is True
    resume_task = type(
        "RT",
        (),
        {
            "task_id": "resume_tsk",
            "parent_task_id": "child_tsk2",
            "session_id": "sess_x",
            "input": {
                "bundle_id": "b1",
                "artifact_id": "a1",
                "goal": "x",
                "_resume": {
                    "resume_of_task_id": "child_tsk2",
                    "input_request_id": "uir_2",
                    "resume_state": {"transcript": "", "tool_round": 1, "clarify_used": True, "steps_log": []},
                    "reply": {"content": "yes"},
                },
            },
            "source": "orchestrator",
            "source_id": "cosmic/orchestrator:1.0.0",
            "channel": "desktop:t",
        },
    )()
    out = await iw.run_tabular_reason_workbook(agent=agent, task=resume_task, http_client=AsyncMock(), cfg=cfg)  # type: ignore[arg-type]
    assert out.get("finish_reason") == "clarify_repeat"
    assert len(orch_calls) == 1


@pytest.mark.asyncio
async def test_langgraph_resume_creates_fresh_step_plan(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "preview.md").write_text("# t\n", encoding="utf-8")
    (tmp_path / "sheet_catalog.json").write_text('{"sheets":[]}', encoding="utf-8")

    class _Agent:
        def __init__(self) -> None:
            self.step_plan = _StepPlanSpy()

        def _require(self, x: str) -> str:
            return x

        def _safe(self, x) -> str:
            return str(x or "").strip()

        async def _emit_stage(self, *a, **k) -> None:
            return None

        async def emit_event(self, *a, **k) -> str:
            return "mid"

        def _bundle_disk_path(self, bundle_id: str, artifact_id: str) -> Path:
            return tmp_path

    agent = _Agent()
    task = type(
        "T",
        (),
        {
            "task_id": "resume_child",
            "parent_task_id": "child_tsk2",
            "session_id": "sess_x",
            "input": {
                "bundle_id": "b1",
                "artifact_id": "a1",
                "goal": "x",
                "_resume": {
                    "resume_of_task_id": "child_tsk2",
                    "input_request_id": "uir_2",
                    "resume_state": {"transcript": "", "tool_round": 1, "clarify_used": True, "steps_log": []},
                    "reply": {"content": "yes"},
                },
            },
            "source": "orchestrator",
            "source_id": "cosmic/orchestrator:1.0.0",
            "channel": "desktop:t",
        },
    )()

    cfg = type(
        "C",
        (),
        {
            "enable_internal_llm": True,
            "mimo_api_key": "k",
            "mimo_base_url": "https://x/v1",
            "include_financial_fpna_prompt": False,
            "sandbox_timeout_sec": 30.0,
            "tabular_reason_use_langgraph": True,
            "tabular_reason_max_tool_rounds": 3,
            "mimo_model": "m",
            "mimo_timeout_sec": 30.0,
        },
    )()

    async def fake_invoke(*, operation: str, **kwargs) -> str:
        if operation == "tabular.internal_llm.reason_step":
            return '{"action":"done","answer":"ok","rationale":"done"}'
        if operation == "tabular.internal_llm.reason_answer":
            return "Wrapped up."
        return ""

    monkeypatch.setattr(trg, "invoke_tabular_mimo", fake_invoke)

    out = await iw.run_tabular_reason_workbook(agent=agent, task=task, http_client=AsyncMock(), cfg=cfg)  # type: ignore[arg-type]
    assert out.get("workflow") == "langgraph"
    # With dynamic planning, resume + immediate "done" skips plan creation
    # (MiMo decides whether to plan; fake_invoke returns "done" directly)
    assert agent.step_plan.created_steps == []


@pytest.mark.asyncio
async def test_langgraph_delegate_suspends_via_orchestrator(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "preview.md").write_text("# t\n", encoding="utf-8")
    (tmp_path / "sheet_catalog.json").write_text('{"sheets":[]}', encoding="utf-8")
    events: list[tuple[str, str, dict]] = []
    delegate_calls: list[dict[str, object]] = []

    class _Agent:
        agent_id = "cosmic/tabular-agent:1.0.0"

        def _require(self, x: str) -> str:
            return x

        def _safe(self, x) -> str:
            return str(x or "").strip()

        async def _emit_stage(self, *a, **k) -> None:
            return None

        async def emit_event(self, task_id: str, event_type: str, payload: dict) -> str:
            events.append((task_id, event_type, payload))
            return "mid"

        async def request_orchestrator_delegate(self, **kwargs):
            delegate_calls.append(dict(kwargs))
            return {"reverse_task_id": "rvt_1", "status": "registered"}

        def _bundle_disk_path(self, bundle_id: str, artifact_id: str) -> Path:
            return tmp_path

    agent = _Agent()
    task = type(
        "T",
        (),
        {
            "task_id": "child_delegate",
            "parent_task_id": "parent_orch",
            "session_id": "sess_x",
            "input": {"bundle_id": "b1", "artifact_id": "a1", "goal": "Need tax context", "request_id": "req_1"},
            "source": "orchestrator",
            "source_id": "cosmic/orchestrator:1.0.0",
            "channel": "desktop:test",
            "priority": "high",
            "task_list_id": "sess_x",
            "deadline_ts": None,
            "idempotency_key": "idem_1",
        },
    )()

    cfg = type(
        "C",
        (),
        {
            "enable_internal_llm": True,
            "mimo_api_key": "k",
            "mimo_base_url": "https://x/v1",
            "include_financial_fpna_prompt": False,
            "sandbox_timeout_sec": 30.0,
            "tabular_reason_use_langgraph": True,
            "tabular_reason_max_tool_rounds": 5,
            "mimo_model": "m",
            "mimo_timeout_sec": 30.0,
        },
    )()

    async def fake_invoke(*, operation: str, **kwargs) -> str:
        if operation == "tabular.internal_llm.reason_step":
            return json.dumps(
                {
                    "action": "delegate",
                    "delegate_intent": "firecrawl.scrape",
                    "delegate_input": {"url": "https://www.irs.gov/"},
                    "rationale": "Need live tax guidance.",
                }
            )
        if operation == "tabular.internal_llm.reason_answer":
            return "Waiting."
        return ""

    monkeypatch.setattr(trg, "invoke_tabular_mimo", fake_invoke)

    out = await iw.run_tabular_reason_workbook(
        agent=agent,
        task=task,
        http_client=AsyncMock(),
        cfg=cfg,
    )  # type: ignore[arg-type]

    assert out.get("workflow") == "langgraph"
    assert out.get("suspended") is True
    assert out.get("finish_reason") == "awaiting_delegate"
    assert out.get("delegate_used") is True
    assert len(delegate_calls) == 1
    assert delegate_calls[0]["target_intent"] == "firecrawl.scrape"
    assert delegate_calls[0]["target_input"] == {"url": "https://www.irs.gov/"}
    suspended_payload = next(payload for _, event_type, payload in events if event_type == "task.suspended")
    assert suspended_payload["reverse_task_id"] == "rvt_1"
    assert suspended_payload["target_intent"] == "firecrawl.scrape"


@pytest.mark.asyncio
async def test_langgraph_resume_includes_delegated_result_context(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "preview.md").write_text("# t\n", encoding="utf-8")
    (tmp_path / "sheet_catalog.json").write_text('{"sheets":[]}', encoding="utf-8")
    events: list[tuple[str, str, dict]] = []

    class _Agent:
        agent_id = "cosmic/tabular-agent:1.0.0"

        def _require(self, x: str) -> str:
            return x

        def _safe(self, x) -> str:
            return str(x or "").strip()

        async def _emit_stage(self, *a, **k) -> None:
            return None

        async def emit_event(self, task_id: str, event_type: str, payload: dict) -> str:
            events.append((task_id, event_type, payload))
            return "mid"

        def _bundle_disk_path(self, bundle_id: str, artifact_id: str) -> Path:
            return tmp_path

    agent = _Agent()
    task = type(
        "T",
        (),
        {
            "task_id": "resume_delegate_child",
            "parent_task_id": "child_delegate",
            "session_id": "sess_x",
            "input": {
                "bundle_id": "b1",
                "artifact_id": "a1",
                "goal": "Need tax context",
                "request_id": "req_1",
                "_resume": {
                    "resume_of_task_id": "child_delegate",
                    "resume_state": {
                        "transcript": "",
                        "tool_round": 1,
                        "clarify_used": False,
                        "delegate_used": True,
                        "steps_log": [],
                    },
                    "reply": {},
                    "reverse_task": {
                        "reverse_task_id": "rvt_1",
                        "target_intent": "firecrawl.scrape",
                        "delegated_task_id": "tsk_firecrawl_1",
                    },
                    "reverse_result": {
                        "status": "completed",
                        "output": {"markdown": "IRS guidance text"},
                        "artifacts": [],
                    },
                },
            },
            "source": "orchestrator",
            "source_id": "cosmic/orchestrator:1.0.0",
            "channel": "desktop:test",
        },
    )()

    cfg = type(
        "C",
        (),
        {
            "enable_internal_llm": True,
            "mimo_api_key": "k",
            "mimo_base_url": "https://x/v1",
            "include_financial_fpna_prompt": False,
            "sandbox_timeout_sec": 30.0,
            "tabular_reason_use_langgraph": True,
            "tabular_reason_max_tool_rounds": 5,
            "mimo_model": "m",
            "mimo_timeout_sec": 30.0,
        },
    )()

    async def fake_invoke(*, operation: str, **kwargs) -> str:
        if operation == "tabular.internal_llm.reason_step":
            assert "delegated_result firecrawl.scrape" in kwargs["user_message"]
            assert "IRS guidance text" in kwargs["user_message"]
            return '{"action":"done","answer":"Use the IRS guidance.","rationale":"done"}'
        if operation == "tabular.internal_llm.reason_answer":
            return "Use the IRS guidance."
        return ""

    monkeypatch.setattr(trg, "invoke_tabular_mimo", fake_invoke)

    out = await iw.run_tabular_reason_workbook(
        agent=agent,
        task=task,
        http_client=AsyncMock(),
        cfg=cfg,
    )  # type: ignore[arg-type]

    assert out.get("workflow") == "langgraph"
    assert out.get("response")
    resumed_payload = next(payload for _, event_type, payload in events if event_type == "task.resumed")
    assert resumed_payload["reverse_task_id"] == "rvt_1"


@pytest.mark.asyncio
async def test_langgraph_active_skill_context_isolated_from_transcript(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "preview.md").write_text("# t\n", encoding="utf-8")
    (tmp_path / "sheet_catalog.json").write_text('{"sheets":[]}', encoding="utf-8")

    skills_dir = Path(trg.AGENT_ROOT) / "skills"
    discovered = discover_skills(skills_dir)
    ratio_skill = next(s for s in discovered if s["name"] == "ratio-analysis")
    ratio_body = load_skill_content(ratio_skill["path"]) or ""
    ratio_fragment = ratio_body[:120]

    class _Agent:
        def _require(self, x: str) -> str:
            return x

        def _safe(self, x) -> str:
            return str(x or "").strip()

        async def _emit_stage(self, *a, **k) -> None:
            return None

        def _bundle_disk_path(self, bundle_id: str, artifact_id: str) -> Path:
            return tmp_path

    agent = _Agent()
    task = type(
        "T",
        (),
        {
            "task_id": "task_skill_ctx",
            "session_id": "sess_skill_ctx",
            "input": {"bundle_id": "b1", "artifact_id": "a1", "goal": "analyze ROE"},
            "source": "user",
            "source_id": "u1",
            "channel": "desktop:test",
        },
    )()

    cfg = type(
        "C",
        (),
        {
            "enable_internal_llm": True,
            "mimo_api_key": "k",
            "mimo_base_url": "https://x/v1",
            "include_financial_fpna_prompt": False,
            "sandbox_timeout_sec": 30.0,
            "tabular_reason_use_langgraph": True,
            "tabular_reason_max_tool_rounds": 5,
            "skills_enabled": True,
            "skills_dir": str(skills_dir),
            "mimo_model": "m",
            "mimo_timeout_sec": 30.0,
        },
    )()

    step = {"n": 0}

    async def fake_invoke(*, operation: str, **kwargs) -> str:
        if operation == "tabular.internal_llm.reason_step":
            step["n"] += 1
            if step["n"] == 1:
                assert "## Available Skills" in kwargs["system_content"]
                return '{"action":"activate_skill","skill_name":"ratio-analysis","rationale":"Need finance formulas"}'
            assert "## Active Skill" in kwargs["system_content"]
            assert "Name: ratio-analysis" in kwargs["system_content"]
            assert ratio_fragment in kwargs["system_content"]
            assert ratio_fragment not in kwargs["user_message"]
            return '{"action":"done","answer":"ROE analysis ready","rationale":"done"}'
        if operation == "tabular.internal_llm.reason_answer":
            return "ROE analysis ready"
        return ""

    monkeypatch.setattr(trg, "invoke_tabular_mimo", fake_invoke)

    out = await iw.run_tabular_reason_workbook(
        agent=agent,
        task=task,
        http_client=AsyncMock(),
        cfg=cfg,
    )  # type: ignore[arg-type]

    assert out.get("workflow") == "langgraph"
    assert out.get("response")
    assert step["n"] >= 2


@pytest.mark.asyncio
async def test_langgraph_resume_preserves_active_skill_context(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "preview.md").write_text("# t\n", encoding="utf-8")
    (tmp_path / "sheet_catalog.json").write_text('{"sheets":[]}', encoding="utf-8")

    skills_dir = Path(trg.AGENT_ROOT) / "skills"
    discovered = discover_skills(skills_dir)
    ratio_skill = next(s for s in discovered if s["name"] == "ratio-analysis")
    ratio_body = load_skill_content(ratio_skill["path"]) or ""
    ratio_fragment = ratio_body[:120]

    class _Agent:
        def _require(self, x: str) -> str:
            return x

        def _safe(self, x) -> str:
            return str(x or "").strip()

        async def _emit_stage(self, *a, **k) -> None:
            return None

        async def emit_event(self, *a, **k) -> str:
            return "mid"

        def _bundle_disk_path(self, bundle_id: str, artifact_id: str) -> Path:
            return tmp_path

    agent = _Agent()
    task = type(
        "T",
        (),
        {
            "task_id": "task_skill_resume",
            "parent_task_id": "parent_skill_resume",
            "session_id": "sess_skill_resume",
            "input": {
                "bundle_id": "b1",
                "artifact_id": "a1",
                "goal": "analyze ROE",
                "_resume": {
                    "resume_of_task_id": "task_prior",
                    "input_request_id": "uir_skill",
                    "resume_state": {
                        "transcript": "--- prior work ---",
                        "tool_round": 1,
                        "clarify_used": False,
                        "delegate_used": False,
                        "steps_log": [],
                        "active_skill_name": "ratio-analysis",
                        "active_skill_content": ratio_body,
                    },
                    "reply": {"content": "continue"},
                },
            },
            "source": "orchestrator",
            "source_id": "cosmic/orchestrator:1.0.0",
            "channel": "desktop:test",
        },
    )()

    cfg = type(
        "C",
        (),
        {
            "enable_internal_llm": True,
            "mimo_api_key": "k",
            "mimo_base_url": "https://x/v1",
            "include_financial_fpna_prompt": False,
            "sandbox_timeout_sec": 30.0,
            "tabular_reason_use_langgraph": True,
            "tabular_reason_max_tool_rounds": 5,
            "skills_enabled": True,
            "skills_dir": str(skills_dir),
            "mimo_model": "m",
            "mimo_timeout_sec": 30.0,
        },
    )()

    async def fake_invoke(*, operation: str, **kwargs) -> str:
        if operation == "tabular.internal_llm.reason_step":
            assert "## Active Skill" in kwargs["system_content"]
            assert "Name: ratio-analysis" in kwargs["system_content"]
            assert ratio_fragment in kwargs["system_content"]
            assert ratio_fragment not in kwargs["user_message"]
            return '{"action":"done","answer":"resume ok","rationale":"done"}'
        if operation == "tabular.internal_llm.reason_answer":
            return "resume ok"
        return ""

    monkeypatch.setattr(trg, "invoke_tabular_mimo", fake_invoke)

    out = await iw.run_tabular_reason_workbook(
        agent=agent,
        task=task,
        http_client=AsyncMock(),
        cfg=cfg,
    )  # type: ignore[arg-type]

    assert out.get("workflow") == "langgraph"
    assert out.get("response")
