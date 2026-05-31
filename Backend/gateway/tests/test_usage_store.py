from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

from gateway.usage_store import UsageStore
from shared.usage import UsageEvent


def _usage_event(
    llm_call_id: str,
    *,
    operation: str = "orchestrator.process",
    source_component: str = "orchestrator",
    metadata_json: dict[str, object] | None = None,
    placed_at: str = "2026-05-20T12:00:00Z",
) -> UsageEvent:
    return UsageEvent(
        llm_call_id=llm_call_id,
        source_component=source_component,
        source_id="cosmic-orchestrator",
        session_id="sess_test",
        route="opus",
        operation=operation,
        usage_kind="tokens",
        provider="anthropic",
        model="claude-sonnet-4-20250514",
        request_id=f"req_{llm_call_id}",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        estimated_cost_usd=0.001,
        metadata_json=metadata_json,
        llm_call_placed_at=placed_at,
    )


def test_dashboard_summary_groups_heartbeat_usage_from_metadata_and_operation(tmp_path):
    store = UsageStore(tmp_path / "usage.db")
    store.initialize()
    store.append(
        _usage_event(
            "call_heartbeat_metadata",
            metadata_json={"source": "heartbeat", "source_id": "scheduler_heartbeat"},
        )
    )
    store.append(_usage_event("call_heartbeat_operation", operation="orchestrator.heartbeat"))
    store.append(_usage_event("call_chat_1"))
    store.append(_usage_event("call_chat_2"))

    summary = store.dashboard_summary(
        range_start_iso="2026-05-20T00:00:00Z",
        range_end_iso="2026-05-21T00:00:00Z",
    )

    by_label = {row["label"]: row for row in summary["usage_by_feature"]}
    assert by_label["Heartbeats"]["count"] == 2
    assert by_label["Orchestration"]["count"] == 2


def test_dashboard_summary_pins_heartbeat_usage_when_lower_volume(tmp_path):
    store = UsageStore(tmp_path / "usage.db")
    store.initialize()
    high_volume_features = [
        ("agent", "docs.parse"),
        ("orchestrator", "memory_write"),
        ("gateway", "gateway.capability_wishlist"),
        ("orchestrator", "orchestrator.perplexity_research"),
        ("scheduler", "scheduler.cron"),
        ("model_router", "model_router.classify"),
        ("gateway", "gateway.direct_chat"),
    ]
    for index, (source_component, operation) in enumerate(high_volume_features):
        for repeat in range(2):
            store.append(
                _usage_event(
                    f"call_high_{index}_{repeat}",
                    operation=operation,
                    source_component=source_component,
                )
            )
    store.append(_usage_event("call_heartbeat", operation="orchestrator.heartbeat"))

    summary = store.dashboard_summary(
        range_start_iso="2026-05-20T00:00:00Z",
        range_end_iso="2026-05-21T00:00:00Z",
        feature_limit=6,
    )

    labels = [row["label"] for row in summary["usage_by_feature"]]
    assert "Heartbeats" in labels
