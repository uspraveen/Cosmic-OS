from __future__ import annotations

import json
from pathlib import Path

import pytest

from gateway.scheduler.store import SchedulerStore
from orchestrator.prompts import build_agentic_system_prompt
from orchestrator.tools.executor import ToolExecutionContext, ToolExecutor
from orchestrator.tools.registry import get_tool_spec


def _store(tmp_path: Path) -> SchedulerStore:
    store = SchedulerStore(tmp_path / "scheduler.db")
    store.initialize(default_timezone="America/Chicago")
    return store


def test_orchestrator_learning_dedupes_and_stale_keeps_history(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.render_orchestrator_learnings() == ""

    created = store.record_orchestrator_learning(
        lesson="On an inbound agent-email turn, the reply is the email. Do not also send.",
        request_id="req_1",
        session_id="email-thread:demo",
    )
    assert created["duplicate"] is False
    assert created["applied_to_specialist"] == "orchestrator"
    assert created["author"] == "orchestrator"

    again = store.record_orchestrator_learning(
        lesson="  on an inbound agent-email turn, the reply is the email. do not also send.  "
    )
    assert again["duplicate"] is True
    assert again["learning_id"] == created["learning_id"]
    assert len(store.list_orchestrator_learnings()) == 1

    specialist = store.record_orchestrator_learning(
        lesson="Pass the deliverable artifact id when this specialist must attach a file.",
        applied_to_specialist="cosmic/email-agent:1.0.0",
    )
    rendered = store.render_orchestrator_learnings()
    assert created["learning_id"] in rendered
    assert "Before delegating to that specialist:" in rendered
    assert "cosmic/email-agent:1.0.0" in rendered

    updated = store.update_orchestrator_learning(
        learning_id=created["learning_id"],
        lesson="On an inbound agent-email turn, answer in the reply and do not send a second email.",
    )
    assert updated["learning_id"] == created["learning_id"]
    assert "second email" in updated["lesson"]

    assert store.stale_orchestrator_learning(learning_id=specialist["learning_id"], reason="no longer true") == 1
    assert "cosmic/email-agent:1.0.0" not in store.render_orchestrator_learnings()
    history = store.list_orchestrator_learnings(include_stale=True)
    assert any(item["learning_id"] == specialist["learning_id"] and item["status"] == "stale" for item in history)


def test_orchestrator_learning_cap_is_per_specialist(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for index in range(store.ORCHESTRATOR_LEARNING_ACTIVE_CAP):
        store.record_orchestrator_learning(lesson=f"Orchestrator lesson {index}")
    with pytest.raises(ValueError, match="Active learning cap"):
        store.record_orchestrator_learning(lesson="One lesson too many")
    extra = store.record_orchestrator_learning(
        lesson="A specialist can still take its own lesson.",
        applied_to_specialist="cosmic/gmail-agent:1.0.0",
    )
    assert extra["duplicate"] is False


def test_learning_expiry_is_optional_and_stated_back(tmp_path: Path) -> None:
    store = _store(tmp_path)
    standing = store.record_orchestrator_learning(lesson="On an inbound agent-email turn, do not send a second email.")
    assert standing["expires_at"] is None
    assert "until you stale it" in standing["lifetime"]
    assert "until " not in store.render_orchestrator_learnings()

    temporary = store.record_orchestrator_learning(
        lesson="Skip the flaky sheet export until the owner fixes it.",
        expires_at="48h",
        expires_at_set=True,
    )
    assert temporary["expires_at"]
    assert "expires at" in temporary["lifetime"]
    assert "until " in store.render_orchestrator_learnings()

    changed = store.record_orchestrator_learning(
        lesson="Skip the flaky sheet export until the owner fixes it.",
        expires_at="7d",
        expires_at_set=True,
    )
    assert changed["duplicate"] is True
    assert changed["expiry_changed"] is True
    assert changed["expires_at"] != temporary["expires_at"]

    cleared = store.update_orchestrator_learning(
        learning_id=changed["learning_id"],
        expires_at="none",
        expires_at_set=True,
    )
    assert cleared["expires_at"] is None
    assert "until you stale it" in cleared["lifetime"]

    soon = store.record_orchestrator_learning(
        lesson="This one should already be over.",
        expires_at="1h",
        expires_at_set=True,
    )
    with store._lock, store._connect() as connection:
        connection.execute(
            "UPDATE orchestrator_learnings SET expires_at = ? WHERE learning_id = ?",
            ("2000-01-01T00:00:00Z", soon["learning_id"]),
        )
        connection.commit()
    rendered = store.render_orchestrator_learnings()
    assert soon["learning_id"] not in rendered
    history = store.list_orchestrator_learnings(include_stale=True)
    expired = next(item for item in history if item["learning_id"] == soon["learning_id"])
    assert expired["status"] == "stale"
    assert expired["stale_reason"] == "Expired"

    with pytest.raises(ValueError, match="in the future"):
        store.record_orchestrator_learning(
            lesson="Already over.",
            expires_at="2000-01-01T00:00:00Z",
            expires_at_set=True,
        )


def test_learnings_prompt_is_absent_until_a_row_exists() -> None:
    empty = build_agentic_system_prompt("User prefers short answers.")
    assert "orchestrator_learnings" in empty
    assert "do not enter `memory_write` or the nightly summary" in empty
    assert "Leave `expires_at` unset for a standing lesson" in empty
    assert "## Operational Learnings" not in empty
    assert "User prefers short answers." in empty

    filled = build_agentic_system_prompt(
        "User prefers short answers.",
        orchestrator_learnings="- [oln_abc] On an inbound agent-email turn, do not send a second email.",
    )
    assert "## Operational Learnings" in filled
    assert "oln_abc" in filled
    assert filled.index("## Operational Learnings") < filled.index("User prefers short answers.")
    assert "Not memory, and not part of the nightly summary." in filled


def test_orchestrator_learnings_tool_is_registered() -> None:
    spec = get_tool_spec("orchestrator_learnings")
    assert spec is not None
    assert spec.handler_method == "_orchestrator_learnings"
    assert "outside memory" in spec.prompt_summary


@pytest.mark.asyncio
async def test_orchestrator_learnings_tool_uses_the_gateway() -> None:
    executor = ToolExecutor(gateway_url="http://gateway.local", gateway_internal_token="token")
    seen: dict[str, object] = {}

    async def fake_request(method: str, path: str, **kwargs):
        seen["method"] = method
        seen["path"] = path
        seen["json"] = kwargs.get("json_body")
        return {
            "updated": True,
            "message": "Operational learning recorded.",
            "content": "- [oln_abc] Keep the reply as the email.",
            "learnings": [{"learning_id": "oln_abc"}],
            "learning": {"learning_id": "oln_abc", "duplicate": False},
            "bytes": 10,
        }

    executor._request_gateway_json = fake_request  # type: ignore[method-assign]
    payload = json.loads(
        await executor.execute(
            "orchestrator_learnings",
            {
                "action": "record",
                "lesson": "On an inbound agent-email turn, do not send a second email.",
                "expires_at": "48h",
            },
            context=ToolExecutionContext(request_id="req_9", session_id="sess_9"),
        )
    )
    assert payload["updated"] is True
    assert payload["learning"]["learning_id"] == "oln_abc"
    assert seen["method"] == "POST"
    assert seen["path"] == "/internal/scheduler/orchestrator-learnings"
    body = seen["json"]
    assert isinstance(body, dict)
    assert body["request_id"] == "req_9"
    assert body["source"] == "orchestrator"
    assert body["expires_at"] == "48h"
    assert body["expires_at_set"] is True


@pytest.mark.asyncio
async def test_orchestrator_learnings_tool_does_not_fall_back_without_gateway() -> None:
    executor = ToolExecutor()
    payload = json.loads(
        await executor.execute(
            "orchestrator_learnings",
            {"action": "record", "lesson": "A lesson that must not become memory."},
        )
    )
    assert payload["error"] is True
    assert "gateway" in payload["message"].lower()
