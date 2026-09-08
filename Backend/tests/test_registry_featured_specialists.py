"""Tests for RegistryStore.refresh_featured_specialists' new-agent grace period.

A brand-new specialist starts with zero usage history, so pure usage-based
ranking would never surface it until the orchestrator happened to find it via
agent_catalog_search first — the same cold-start gap browser_task had before
it was gated behind the featured-specialist mechanism. This covers the fix:
a recently-registered agent is guaranteed a spot (additional to, not
displacing, the merit-ranked ones), capped, and falls off automatically once
its grace window elapses.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from registry.store import RegistryStore


def _card(agent_id: str, display_name: str) -> dict[str, object]:
    return {
        "agent_id": agent_id,
        "display_name": display_name,
        "description": f"{display_name} specialist.",
        "sla": {"max_concurrency": 1, "heartbeat_ttl_sec": 30, "max_task_duration_sec": 300},
        "intents": [{"name": f"{agent_id.split('/')[1].split(':')[0]}.run", "description": "Run."}],
    }


def _set_registered_at(db_path, agent_id: str, when: datetime) -> None:
    """Test-only: upsert_agent_card always stamps registered_at=now on first
    insert (by design — it's preserved across later re-registrations), so
    precise past/future timestamps for grace-window testing are set directly."""
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "UPDATE agents SET registered_at = ? WHERE agent_id = ?",
            (when.isoformat().replace("+00:00", "Z"), agent_id),
        )
        connection.commit()
    finally:
        connection.close()


@pytest.fixture()
def store(tmp_path):
    registry = RegistryStore(tmp_path / "registry.db")
    registry.initialize()
    return registry


def test_brand_new_agent_is_featured_even_with_established_competition(store):
    now = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)

    # Five established, heavily-used agents — enough to fill a limit=5
    # featured list purely on merit, with real margin above a bare recency
    # bonus (established agents used 5x today easily clear that).
    for index in range(5):
        agent_id = f"cosmic/established-{index}-agent:1.0.0"
        store.upsert_agent_card(_card(agent_id, f"Established {index}"))
        _set_registered_at(store.db_path, agent_id, now - timedelta(days=60))
        for _ in range(5):
            store.record_agent_usage(agent_id, "run", used_at=now - timedelta(hours=2))

    # One brand-new agent, zero usage, registered an hour ago.
    store.upsert_agent_card(_card("cosmic/new-agent:1.0.0", "New Agent"))
    _set_registered_at(store.db_path, "cosmic/new-agent:1.0.0", now - timedelta(hours=1))

    featured = store.refresh_featured_specialists(
        limit=5,
        lookback_days=15,
        new_agent_grace_days=3,
        new_agent_extra_slots=3,
        refreshed_at=now,
    )
    featured_ids = {item["agent_id"] for item in featured}

    # All five established agents keep their merit-earned spots...
    for index in range(5):
        assert f"cosmic/established-{index}-agent:1.0.0" in featured_ids
    # ...and the new agent is ADDITIONALLY present, not displacing any of them.
    assert "cosmic/new-agent:1.0.0" in featured_ids
    assert len(featured) == 6

    new_entry = next(item for item in featured if item["agent_id"] == "cosmic/new-agent:1.0.0")
    assert new_entry["new_agent"] is True
    assert new_entry["usage_count"] == 0
    for index in range(5):
        established_entry = next(
            item for item in featured if item["agent_id"] == f"cosmic/established-{index}-agent:1.0.0"
        )
        assert established_entry["new_agent"] is False


def test_agent_past_its_grace_window_is_not_force_included(store):
    now = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)

    for index in range(5):
        agent_id = f"cosmic/established-{index}-agent:1.0.0"
        store.upsert_agent_card(_card(agent_id, f"Established {index}"))
        _set_registered_at(store.db_path, agent_id, now - timedelta(days=60))
        for _ in range(5):
            store.record_agent_usage(agent_id, "run", used_at=now - timedelta(hours=2))

    # Registered 10 days ago: within the 15-day usage lookback (so it's still
    # scored, not silently dropped), but past the 3-day new-agent grace
    # window — it must compete on merit alone, and with zero usage it loses.
    store.upsert_agent_card(_card("cosmic/stale-new-agent:1.0.0", "Stale New Agent"))
    _set_registered_at(store.db_path, "cosmic/stale-new-agent:1.0.0", now - timedelta(days=10))

    featured = store.refresh_featured_specialists(
        limit=5,
        lookback_days=15,
        new_agent_grace_days=3,
        new_agent_extra_slots=3,
        refreshed_at=now,
    )
    featured_ids = {item["agent_id"] for item in featured}
    assert "cosmic/stale-new-agent:1.0.0" not in featured_ids
    assert len(featured) == 5


def test_new_agent_extra_slots_caps_a_burst_of_launches(store):
    now = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)

    for index in range(5):
        agent_id = f"cosmic/established-{index}-agent:1.0.0"
        store.upsert_agent_card(_card(agent_id, f"Established {index}"))
        _set_registered_at(store.db_path, agent_id, now - timedelta(days=60))
        for _ in range(5):
            store.record_agent_usage(agent_id, "run", used_at=now - timedelta(hours=2))

    # Five simultaneously-new agents, but only 2 extra slots configured.
    for index in range(5):
        agent_id = f"cosmic/burst-new-{index}-agent:1.0.0"
        store.upsert_agent_card(_card(agent_id, f"Burst New {index}"))
        _set_registered_at(store.db_path, agent_id, now - timedelta(hours=1))

    featured = store.refresh_featured_specialists(
        limit=5,
        lookback_days=15,
        new_agent_grace_days=3,
        new_agent_extra_slots=2,
        refreshed_at=now,
    )
    new_agent_entries = [item for item in featured if item["new_agent"]]
    assert len(new_agent_entries) == 2
    assert len(featured) == 7  # 5 established + capped 2 new


def test_grace_period_disabled_when_extra_slots_is_zero(store):
    now = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)

    for index in range(5):
        agent_id = f"cosmic/established-{index}-agent:1.0.0"
        store.upsert_agent_card(_card(agent_id, f"Established {index}"))
        _set_registered_at(store.db_path, agent_id, now - timedelta(days=60))
        for _ in range(5):
            store.record_agent_usage(agent_id, "run", used_at=now - timedelta(hours=2))

    store.upsert_agent_card(_card("cosmic/new-agent:1.0.0", "New Agent"))
    _set_registered_at(store.db_path, "cosmic/new-agent:1.0.0", now - timedelta(hours=1))

    featured = store.refresh_featured_specialists(
        limit=5,
        lookback_days=15,
        new_agent_grace_days=3,
        new_agent_extra_slots=0,
        refreshed_at=now,
    )
    assert len(featured) == 5
    assert "cosmic/new-agent:1.0.0" not in {item["agent_id"] for item in featured}
