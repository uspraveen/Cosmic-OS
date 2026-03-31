"""Shared test fixtures for calendar agent tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BACKEND_ROOT))


@pytest.fixture
def mock_task_envelope():
    """Create a minimal TaskEnvelope for testing."""
    from shared.contracts import TaskEnvelope

    return TaskEnvelope(
        task_id="tsk_test_001",
        task_list_id="tl_test",
        parent_task_id=None,
        session_id="sess_test",
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/calendar-agent:1.0.0",
        intent="calendar.list_events",
        input={"calendar_id": "primary"},
        idempotency_key="idem_test_001",
        deadline_ts=None,
        priority="normal",
        signature="test_sig",
    )
