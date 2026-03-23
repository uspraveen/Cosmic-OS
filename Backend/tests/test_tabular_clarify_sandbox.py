"""Tests for tabular clarify semantics and upgraded sandbox (venv/pip/network)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agents.tabular_agent import internal_workflow as iw
from agents.tabular_agent import tabular_reason_graph as trg
from agents.tabular_agent.sandbox import (
    _deny_patterns,
    bundle_script_with_prelude,
    persist_bundle_python_script,
    provision_venv,
    run_python_script,
    validate_pip_packages,
    validate_tabular_python_code,
    write_execution_receipt,
)


# ════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════

def _make_agent(tmp_path: Path, *, with_emit: bool = False):
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

        def sync_run_select(self, bundle_id: str, artifact_id: str, sql: str) -> dict:
            return {"row_count": 0, "rows": [], "columns": [], "truncated": False}

    return _Agent(), events


def _make_task(*, parent_task_id=None, channel="desktop:test", **extra):
    attrs = {
        "task_id": "child_tsk",
        "session_id": "sess_x",
        "input": {"bundle_id": "b1", "artifact_id": "a1", "goal": "test", "request_id": "req_1"},
        "source": "orchestrator",
        "source_id": "cosmic/orchestrator:1.0.0",
        "channel": channel,
        **extra,
    }
    if parent_task_id is not None:
        attrs["parent_task_id"] = parent_task_id
    return type("T", (), attrs)()


def _make_cfg(**overrides):
    defaults = {
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
        "sandbox_allow_network": False,
        "sandbox_allow_pip": False,
        "sandbox_pip_timeout_sec": 60.0,
        "sandbox_venv_cache_root": "",
    }
    defaults.update(overrides)
    return type("C", (), defaults)()


# ════════════════════════════════════════════════════════════
#  1. Clarify: relay_error is surfaced correctly
# ════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_clarify_relay_error_surface(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "preview.md").write_text("# t\n", encoding="utf-8")
    (tmp_path / "sheet_catalog.json").write_text('{"sheets":[]}', encoding="utf-8")
    agent, events = _make_agent(tmp_path)
    task = _make_task(parent_task_id="parent_orch")
    cfg = _make_cfg()

    step = {"n": 0}

    async def fake_invoke(*, operation: str, **kwargs) -> str:
        if operation == "tabular.internal_llm.reason_step":
            step["n"] += 1
            if step["n"] == 1:
                return '{"action":"clarify","question":"Which?","options":["X"],"rationale":"ask"}'
            return '{"action":"done","answer":"fallback","rationale":"d"}'
        return "Summary."

    async def fake_orch_error(**kwargs) -> dict:
        raise RuntimeError("orchestrator unreachable")

    monkeypatch.setattr(trg, "invoke_tabular_mimo", fake_invoke)
    monkeypatch.setattr(trg, "request_orchestrator_task_input", fake_orch_error)

    out = await iw.run_tabular_reason_workbook(agent=agent, task=task, http_client=AsyncMock(), cfg=cfg)  # type: ignore[arg-type]
    assert out.get("clarify_used") is True
    et_types = [e[1] for e in events]
    assert "task.suspended" not in et_types
    assert "task.resumed" not in et_types
    assert out.get("last_tool_result", {}).get("clarify_status") == "relay_error"
    assert "orchestrator unreachable" in json.dumps(out.get("last_tool_result") or {})


# ════════════════════════════════════════════════════════════
#  2. Clarify: successful publication suspends the run
# ════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_clarify_suspended_status(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "preview.md").write_text("# t\n", encoding="utf-8")
    (tmp_path / "sheet_catalog.json").write_text('{"sheets":[]}', encoding="utf-8")
    agent, events = _make_agent(tmp_path)
    task = _make_task(parent_task_id="parent_orch")
    cfg = _make_cfg()

    step = {"n": 0}

    async def fake_invoke(*, operation: str, **kwargs) -> str:
        if operation == "tabular.internal_llm.reason_step":
            step["n"] += 1
            return '{"action":"clarify","question":"Which date?","options":[],"rationale":"ask"}'
        return "Summary."

    async def fake_orch_suspend(**kwargs) -> dict:
        return {"input_request_id": "uir_t", "ok": True}

    monkeypatch.setattr(trg, "invoke_tabular_mimo", fake_invoke)
    monkeypatch.setattr(trg, "request_orchestrator_task_input", fake_orch_suspend)

    out = await iw.run_tabular_reason_workbook(agent=agent, task=task, http_client=AsyncMock(), cfg=cfg)  # type: ignore[arg-type]
    assert out.get("clarify_used") is True
    assert out.get("suspended") is True
    assert out.get("input_request_id") == "uir_t"
    suspended_payload = next(e[2] for e in events if e[1] == "task.suspended")
    assert suspended_payload["input_request_id"] == "uir_t"


# ════════════════════════════════════════════════════════════
#  3. Clarify: resumed invocation carries provenance
# ════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_clarify_resumed_provenance(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "preview.md").write_text("# t\n", encoding="utf-8")
    (tmp_path / "sheet_catalog.json").write_text('{"sheets":[]}', encoding="utf-8")
    agent, events = _make_agent(tmp_path)
    task = _make_task(parent_task_id="parent_orch")
    cfg = _make_cfg()

    async def fake_invoke(*, operation: str, **kwargs) -> str:
        if operation == "tabular.internal_llm.reason_step":
            return '{"action":"done","answer":"Used A.","rationale":"done"}'
        return "Final."

    monkeypatch.setattr(trg, "invoke_tabular_mimo", fake_invoke)
    resume_task = _make_task(
        parent_task_id="child_tsk",
        task_id="resume_child",
        input={
            "bundle_id": "b1",
            "artifact_id": "a1",
            "goal": "test",
            "request_id": "req_1",
            "_resume": {
                "resume_of_task_id": "child_tsk",
                "input_request_id": "uir_p",
                "resume_state": {"transcript": "", "tool_round": 1, "clarify_used": True, "steps_log": []},
                "reply": {"content": "A"},
            },
        },
    )

    out = await iw.run_tabular_reason_workbook(agent=agent, task=resume_task, http_client=AsyncMock(), cfg=cfg)  # type: ignore[arg-type]
    resumed = next(e[2] for e in events if e[1] == "task.resumed")
    assert resumed["session_id"] == "sess_x"
    assert resumed["channel"] == "desktop:test"
    assert resumed["source"] == "orchestrator"
    assert resumed["source_id"] == "cosmic/orchestrator:1.0.0"
    assert resumed["child_task_id"] == "child_tsk"
    assert resumed["resumed_task_id"] == "resume_child"
    assert resumed["input_request_id"] == "uir_p"
    assert out.get("response")


# ════════════════════════════════════════════════════════════
#  4. No conversational sticky routing used
# ════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_clarify_no_awaiting_reply_used(monkeypatch, tmp_path: Path) -> None:
    """Ensure no event contains <awaiting_reply/> or chat-style hack."""
    (tmp_path / "preview.md").write_text("# t\n", encoding="utf-8")
    (tmp_path / "sheet_catalog.json").write_text('{"sheets":[]}', encoding="utf-8")
    agent, events = _make_agent(tmp_path)
    task = _make_task(parent_task_id="parent_orch")
    cfg = _make_cfg()

    async def fake_invoke(*, operation: str, **kwargs) -> str:
        if operation == "tabular.internal_llm.reason_step":
            return '{"action":"clarify","question":"Q?","options":[],"rationale":"ask"}'
        return "Summary."

    async def fake_orch(**kwargs) -> dict:
        return {"input_request_id": "uir_r"}

    monkeypatch.setattr(trg, "invoke_tabular_mimo", fake_invoke)
    monkeypatch.setattr(trg, "request_orchestrator_task_input", fake_orch)

    await iw.run_tabular_reason_workbook(agent=agent, task=task, http_client=AsyncMock(), cfg=cfg)  # type: ignore[arg-type]
    all_payloads = json.dumps([e[2] for e in events])
    assert "awaiting_reply" not in all_payloads.lower()
    event_types = {e[1] for e in events}
    assert event_types <= {"task.suspended"}


# ════════════════════════════════════════════════════════════
#  5. Sandbox: network denylist relaxation
# ════════════════════════════════════════════════════════════

def test_sandbox_network_denied_by_default() -> None:
    with pytest.raises(ValueError, match="forbidden"):
        validate_tabular_python_code("import requests\nrequests.get('http://x')")


def test_sandbox_network_allowed_when_enabled() -> None:
    validate_tabular_python_code("import requests\nrequests.get('http://x')", allow_network=True)


def test_sandbox_deny_core_still_active_with_network() -> None:
    with pytest.raises(ValueError, match="forbidden"):
        validate_tabular_python_code("import subprocess\nsubprocess.run(['ls'])", allow_network=True)


def test_deny_patterns_differ_by_network_flag() -> None:
    core = _deny_patterns(allow_network=True)
    full = _deny_patterns(allow_network=False)
    assert len(full) > len(core)


# ════════════════════════════════════════════════════════════
#  6. Sandbox: pip package validation
# ════════════════════════════════════════════════════════════

def test_pip_packages_valid() -> None:
    out = validate_pip_packages(["pandas", "openpyxl>=3.0", "matplotlib"])
    assert out == ["pandas", "openpyxl>=3.0", "matplotlib"]


def test_pip_packages_rejects_forbidden() -> None:
    with pytest.raises(ValueError, match="forbidden pip"):
        validate_pip_packages(["subprocess"])


def test_pip_packages_truncates_to_limit() -> None:
    pkgs = [f"pkg{i}" for i in range(20)]
    out = validate_pip_packages(pkgs)
    assert len(out) == 12


# ════════════════════════════════════════════════════════════
#  7. Sandbox: venv provisioning (functional)
# ════════════════════════════════════════════════════════════

def test_venv_provision_and_cache_reuse(tmp_path: Path) -> None:
    cache = tmp_path / "venv_cache"
    python_exe, pkgs, pip_log = provision_venv(
        packages=["six"],
        cache_root=cache,
        pip_timeout_sec=60.0,
    )
    assert python_exe.is_file()
    assert pip_log.get("cache_hit") is False
    assert "pip_exit_code" in pip_log

    python_exe2, _, pip_log2 = provision_venv(
        packages=["six"],
        cache_root=cache,
        pip_timeout_sec=60.0,
    )
    assert pip_log2.get("cache_hit") is True
    assert python_exe2 == python_exe


# ════════════════════════════════════════════════════════════
#  8. Sandbox: execution receipt includes metadata
# ════════════════════════════════════════════════════════════

def test_execution_receipt_includes_sandbox_metadata(tmp_path: Path) -> None:
    receipt_path = write_execution_receipt(
        bundle_root=tmp_path,
        execution_id="exec_meta",
        task_id="t1",
        session_id="s1",
        artifact_id="a1",
        receipt={
            "kind": "tabular_sandbox",
            "parent_task_id": "parent_t",
            "network_enabled": True,
            "packages_installed": ["pandas", "openpyxl"],
            "pip_log": {"pip_exit_code": 0},
            "exit_code": 0,
            "stdout": "ok",
            "stderr": "",
            "duration_ms": 123,
            "script_relative": "codes/exec_meta.py",
        },
    )
    data = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert data["parent_task_id"] == "parent_t"
    assert data["network_enabled"] is True
    assert data["packages_installed"] == ["pandas", "openpyxl"]
    assert data["pip_log"]["pip_exit_code"] == 0
    assert data["task_id"] == "t1"
    assert data["session_id"] == "s1"


# ════════════════════════════════════════════════════════════
#  9. Sandbox: network-enabled script with venv
# ════════════════════════════════════════════════════════════

def test_persist_script_with_network_allowed(tmp_path: Path) -> None:
    code = "import requests\nprint('hello')"
    path = persist_bundle_python_script(
        bundle_root=tmp_path,
        execution_id="exec_net",
        code=code,
        allow_network=True,
    )
    text = path.read_text(encoding="utf-8")
    assert "COSMIC tabular sandbox prelude" in text
    assert "import requests" in text


def test_run_python_script_uses_bundle_scoped_home(tmp_path: Path) -> None:
    script_path = persist_bundle_python_script(
        bundle_root=tmp_path,
        execution_id="exec_home",
        code="from pathlib import Path\nprint(Path.home())",
    )
    out = run_python_script(
        script_path=script_path,
        cwd=tmp_path,
        timeout_sec=10.0,
        bundle_root=tmp_path,
    )
    assert out["exit_code"] == 0
    assert str((tmp_path / ".sandbox_home").resolve()) in out["stdout"]


# ════════════════════════════════════════════════════════════
# 10. LangGraph: python with pip_install field
# ════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_langgraph_python_with_pip_and_receipt_metadata(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "preview.md").write_text("# t\n", encoding="utf-8")
    (tmp_path / "sheet_catalog.json").write_text('{"sheets":[]}', encoding="utf-8")
    agent, events = _make_agent(tmp_path)
    task = _make_task(parent_task_id="parent_orch")
    cfg = _make_cfg(sandbox_allow_pip=False, sandbox_allow_network=True)

    step = {"n": 0}

    async def fake_invoke(*, operation: str, **kwargs) -> str:
        if operation == "tabular.internal_llm.reason_step":
            step["n"] += 1
            if step["n"] == 1:
                return '{"action":"python","python_code":"print(42)","pip_install":["pandas"],"rationale":"p"}'
            return '{"action":"done","answer":"42","rationale":"done"}'
        return "Done."

    monkeypatch.setattr(trg, "invoke_tabular_mimo", fake_invoke)

    out = await iw.run_tabular_reason_workbook(agent=agent, task=task, http_client=AsyncMock(), cfg=cfg)  # type: ignore[arg-type]
    assert out.get("workflow") == "langgraph"
    receipt_files = list((tmp_path / "executions").glob("*.json"))
    assert len(receipt_files) >= 1
    receipt = json.loads(receipt_files[0].read_text(encoding="utf-8"))
    assert receipt["network_enabled"] is True
    assert receipt["parent_task_id"] == "parent_orch"
    assert receipt.get("pip_log", {}).get("skipped") is True


# ════════════════════════════════════════════════════════════
# 11. Non-regression: SQL happy path still works
# ════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_sql_happy_path_no_regression(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "preview.md").write_text("# t\n", encoding="utf-8")
    (tmp_path / "sheet_catalog.json").write_text('{"sheets":[]}', encoding="utf-8")
    agent, _ = _make_agent(tmp_path)
    task = _make_task(parent_task_id="parent_orch")
    cfg = _make_cfg()

    step = {"n": 0}

    async def fake_invoke(*, operation: str, **kwargs) -> str:
        if operation == "tabular.internal_llm.reason_step":
            step["n"] += 1
            if step["n"] == 1:
                return '{"action":"sql","sql":"select 1","rationale":"q"}'
            return '{"action":"done","answer":"1","rationale":"done"}'
        return "Result."

    monkeypatch.setattr(trg, "invoke_tabular_mimo", fake_invoke)

    out = await iw.run_tabular_reason_workbook(agent=agent, task=task, http_client=AsyncMock(), cfg=cfg)  # type: ignore[arg-type]
    assert out.get("workflow") == "langgraph"
    kinds = [s.get("kind") for s in (out.get("steps") or []) if isinstance(s, dict)]
    assert "sql" in kinds
    assert out.get("response")


# ════════════════════════════════════════════════════════════
# 12. Non-regression: python without pip still works
# ════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_python_no_pip_no_regression(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "preview.md").write_text("# t\n", encoding="utf-8")
    (tmp_path / "sheet_catalog.json").write_text('{"sheets":[]}', encoding="utf-8")
    agent, _ = _make_agent(tmp_path)
    task = _make_task(parent_task_id="parent_orch")
    cfg = _make_cfg()

    step = {"n": 0}

    async def fake_invoke(*, operation: str, **kwargs) -> str:
        if operation == "tabular.internal_llm.reason_step":
            step["n"] += 1
            if step["n"] == 1:
                return '{"action":"python","python_code":"print(\\"hi\\")","rationale":"p"}'
            return '{"action":"done","answer":"hi","rationale":"d"}'
        return "Done."

    monkeypatch.setattr(trg, "invoke_tabular_mimo", fake_invoke)

    out = await iw.run_tabular_reason_workbook(agent=agent, task=task, http_client=AsyncMock(), cfg=cfg)  # type: ignore[arg-type]
    assert out.get("workflow") == "langgraph"
    assert (tmp_path / "executions").is_dir()
    receipt_files = list((tmp_path / "executions").glob("*.json"))
    assert len(receipt_files) >= 1
    receipt = json.loads(receipt_files[0].read_text(encoding="utf-8"))
    assert receipt["network_enabled"] is False
    assert receipt["packages_installed"] == []
