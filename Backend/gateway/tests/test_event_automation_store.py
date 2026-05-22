from __future__ import annotations

from gateway.event_automation_store import EventAutomationStore


def test_event_automation_store_creates_lists_and_deactivates(tmp_path):
    store = EventAutomationStore(tmp_path / "event_automations.db")
    store.initialize()

    created = store.create_or_update_automation(
        {
            "event_type": "gmail.inbound",
            "label": "Arun doc request",
            "raw_instruction": "When Arun emails me, create the requested doc.",
            "condition": {"person_ref": "Arun", "resolution_mode": "resolve_on_event"},
            "action": {"type": "orchestrator_task", "goal": "Read the thread and create the doc."},
            "approval_policy": {"send_email": "requires_approval"},
            "created_by": "orchestrator",
        }
    )

    assert created["automation_id"].startswith("aut_")
    assert created["condition"]["person_ref"] == "Arun"
    assert created["action"]["goal"] == "Read the thread and create the doc."
    assert store.list_automations(event_type="gmail.inbound")[0]["automation_id"] == created["automation_id"]

    inactive = store.set_automation_status(created["automation_id"], "inactive")
    assert inactive["status"] == "inactive"
    assert store.list_automations(event_type="gmail.inbound") == []
    assert store.list_automations(event_type="gmail.inbound", status="all")[0]["status"] == "inactive"


def test_event_automation_match_dedupe_and_dispatch_update(tmp_path):
    store = EventAutomationStore(tmp_path / "event_automations.db")
    store.initialize()
    automation = store.create_or_update_automation(
        {
            "event_type": "gmail.inbound",
            "label": "YC interview email",
            "raw_instruction": "When YC emails about interviews, tell me.",
        }
    )

    first = store.record_match(
        {
            "automation_id": automation["automation_id"],
            "event_type": "gmail.inbound",
            "event_ref": "gmail:acct:msg_1",
            "decision": "matched",
            "confidence": 0.91,
            "evidence": {"subject": "YC S26 interview invite"},
        }
    )
    second = store.record_match(
        {
            "automation_id": automation["automation_id"],
            "event_type": "gmail.inbound",
            "event_ref": "gmail:acct:msg_1",
            "decision": "matched",
            "confidence": 0.91,
            "evidence": {"subject": "YC S26 interview invite"},
        }
    )

    assert first["created"] is True
    assert second["created"] is False
    assert store.get_automation(automation["automation_id"])["match_count"] == 1

    updated = store.update_match_dispatch(
        match_id=first["match_id"],
        orchestrator_request_id="req_evt_1",
        orchestrator_task_id="tsk_evt_1",
    )
    assert updated["orchestrator_request_id"] == "req_evt_1"
    assert updated["orchestrator_task_id"] == "tsk_evt_1"
