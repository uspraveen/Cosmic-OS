from __future__ import annotations

import sqlite3

import pytest

from orchestrator.store.ledger import TaskLedger


def test_task_ledger_connect_closes_the_underlying_connection(tmp_path) -> None:
    """Reproduces a real production incident: TaskLedger._connect() returned a
    raw sqlite3.Connection and every call site did `with self._connect() as
    connection:`. sqlite3.Connection.__exit__ only commits/rolls back the
    transaction - it does not close the connection or release its file
    descriptor. Because TaskLedger is hit on every task create/update/complete
    across the orchestrator's lifetime, this leaked one fd per call and
    eventually exhausted the process's open-file limit (observed climbing to
    exactly 1024/1024 after ~40 hours of uptime), which made every in-flight
    user request fail instantly with a silent OPUS_UNAVAILABLE error."""
    ledger = TaskLedger(tmp_path / "ledger.db")
    ledger.initialize()

    captured: list[sqlite3.Connection] = []
    with ledger._connect() as connection:  # noqa: SLF001 - verifying close behavior directly
        captured.append(connection)
        connection.execute("SELECT 1")

    assert len(captured) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        captured[0].execute("SELECT 1")
