from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from gateway.scheduler.store import SchedulerStore


def test_heartbeat_watchpoint_survives_deactivation(tmp_path: Path) -> None:
    store = SchedulerStore(tmp_path / "scheduler.db")
    store.initialize(default_timezone="America/Chicago")
    created = store.upsert_heartbeat_watchpoint(
        name="portfolio_visitors",
        description="new unique visitors on uspraveen.github.io",
        check_kind="manual",
        baseline_state={"known_ips": ["67.172.14.202"]},
        notify_policy="on_new",
    )
    watchpoint_id = created["watchpoint_id"]
    deactivated = store.set_heartbeat_watchpoint_status(
        watchpoint_id,
        status="inactive",
        reason="user asked to stop watching",
        actor="user",
    )
    assert deactivated is not None
    assert deactivated["status"] == "inactive"
    assert store.list_heartbeat_watchpoints() == []
    history = store.list_heartbeat_watchpoints(include_inactive=True)
    assert history[0]["status_reason"] == "user asked to stop watching"
    events = store.list_heartbeat_watchpoint_history(watchpoint_id)
    assert events[0]["event"] == "status_changed"
    assert events[0]["details"]["previous_status"] == "active"
    assert any(event["event"] == "created" for event in events)


def test_record_check_updates_health_and_writes_events(tmp_path: Path) -> None:
    store = SchedulerStore(tmp_path / "scheduler.db")
    store.initialize(default_timezone="America/Chicago")
    created = store.upsert_heartbeat_watchpoint(name="portfolio_visitors")
    watchpoint_id = created["watchpoint_id"]

    blind = store.record_heartbeat_watchpoint_check(
        watchpoint_id,
        check_status="inconclusive",
        detail="approval pending",
    )
    assert blind is not None
    assert blind["last_check_status"] == "inconclusive"
    assert blind["consecutive_failures"] == 1
    blind = store.record_heartbeat_watchpoint_check(
        watchpoint_id,
        check_status="inconclusive",
    )
    assert blind is not None
    assert blind["consecutive_failures"] == 2

    delivered = store.record_heartbeat_watchpoint_check(
        watchpoint_id,
        check_status="ok",
        detail="1 new visitor",
        baseline_state={"known_ips": ["1.2.3.4"]},
        delivered=True,
    )
    assert delivered is not None
    assert delivered["last_check_status"] == "ok"
    assert delivered["consecutive_failures"] == 0
    assert delivered["delivery_count"] == 1
    assert delivered["last_delivered_at"] is not None
    assert delivered["baseline_state"]["known_ips"] == ["1.2.3.4"]

    events = store.list_heartbeat_watchpoint_history(watchpoint_id)
    check_events = [event for event in events if event["event"] == "check"]
    assert [event["status"] for event in check_events] == ["ok", "inconclusive", "inconclusive"]
    assert check_events[0]["details"]["delivered"] is True
    assert "created" in [event["event"] for event in events]
    assert "updated" not in [event["event"] for event in events]

    assert store.record_heartbeat_watchpoint_check(
        "hbwp_missing", check_status="ok"
    ) is None


def test_heartbeat_beat_notes_soft_stale_and_render(tmp_path: Path) -> None:
    store = SchedulerStore(tmp_path / "scheduler.db")
    store.initialize(default_timezone="America/Chicago")
    store.append_heartbeat_beat_note(content="Visitor check: 2 known IPs", kind="note")
    store.append_heartbeat_beat_note(
        content="Suppressed. No material change.",
        kind="beat",
        outcome="suppressed",
        author="gateway",
    )
    rendered = store.render_heartbeat_notes()
    assert "Visitor check" in rendered
    assert "beat/suppressed" in rendered
    assert store.mark_heartbeat_beat_note_stale(match="Visitor check") == 1
    assert len(store.list_heartbeat_beat_notes()) == 1
    assert len(store.list_heartbeat_beat_notes(include_stale=True)) == 2
    store.replace_heartbeat_beat_notes(content="Fresh standing note")
    active = store.list_heartbeat_beat_notes()
    assert len(active) == 1
    assert active[0]["content"] == "Fresh standing note"


def test_record_heartbeat_result_writes_beat_note(tmp_path: Path) -> None:
    store = SchedulerStore(tmp_path / "scheduler.db")
    store.initialize(default_timezone="America/Chicago")
    store.record_heartbeat_result(
        status="suppressed",
        summary="No material change since last delivery.",
        next_fire_at=None,
    )
    notes = store.list_heartbeat_beat_notes(kind="beat")
    assert len(notes) == 1
    assert notes[0]["outcome"] == "suppressed"
    assert "No material change" in notes[0]["content"]


def test_prune_collapses_old_suppress_beats_and_drops_ancient_rows(tmp_path: Path) -> None:
    store = SchedulerStore(tmp_path / "scheduler.db")
    store.initialize(default_timezone="America/Chicago")
    now = datetime.now(timezone.utc)

    def _iso(delta: timedelta) -> str:
        return (now + delta).isoformat().replace("+00:00", "Z")

    with store._lock, store._connect() as connection:
        rows = [
            ("hbnote_recent_a", "beat", "suppressed", _iso(-timedelta(hours=1))),
            ("hbnote_recent_b", "beat", "suppressed", _iso(-timedelta(hours=2))),
            ("hbnote_day_keep", "beat", "suppressed", _iso(-timedelta(days=3, hours=1))),
            ("hbnote_day_drop", "beat", "suppressed", _iso(-timedelta(days=3, hours=5))),
            ("hbnote_old_sup", "beat", "suppressed", _iso(-timedelta(days=20))),
            ("hbnote_old_del", "beat", "delivered", _iso(-timedelta(days=40))),
            ("hbnote_keep_del", "beat", "delivered", _iso(-timedelta(days=10))),
        ]
        for note_id, kind, outcome, created_at in rows:
            connection.execute(
                """
                INSERT INTO heartbeat_beat_notes (
                    note_id, kind, outcome, content, status, author, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'active', 'gateway', ?, ?)
                """,
                (note_id, kind, outcome, f"{outcome} {note_id}", created_at, created_at),
            )
        store._prune_heartbeat_beat_notes(connection)
        connection.commit()
        remaining = {
            row["note_id"]
            for row in connection.execute("SELECT note_id FROM heartbeat_beat_notes").fetchall()
        }
    assert "hbnote_recent_a" in remaining
    assert "hbnote_recent_b" in remaining
    assert "hbnote_day_keep" in remaining
    assert "hbnote_day_drop" not in remaining
    assert "hbnote_old_sup" not in remaining
    assert "hbnote_old_del" not in remaining
    assert "hbnote_keep_del" in remaining
