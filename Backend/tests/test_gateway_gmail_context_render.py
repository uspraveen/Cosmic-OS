from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from gateway.runtime import GatewayRuntime


class _FakeGmailContextStore:
    """Serves surfaced items partitioned by status, like the real store."""

    def __init__(self, by_status: dict[str, list[dict[str, object]]] | None = None) -> None:
        self._by_status = by_status or {}

    def list_recent(
        self,
        *,
        limit: int,
        lookback_hours: int,
        statuses: list[str] | None = None,
        status: str = "active",
    ) -> list[dict[str, object]]:
        assert lookback_hours == 96
        wanted = statuses if statuses is not None else [status]
        items: list[dict[str, object]] = []
        for name in wanted:
            items.extend(self._by_status.get(name, []))
        items.sort(key=lambda entry: str(entry.get("updated_at") or ""), reverse=True)
        return items[:limit]


def _runtime(store: _FakeGmailContextStore) -> GatewayRuntime:
    runtime = GatewayRuntime.__new__(GatewayRuntime)
    runtime.gmail_context_store = store
    return runtime


def test_recent_gmail_context_renders_stable_account_reference() -> None:
    store = _FakeGmailContextStore(
        {
            "active": [
                {
                    "surfaced_id": "gmail_bbfccbf97a454295",
                    "account_id": "acc_a83a2c5b1199",
                    "account_email": "uspraveenraj@gmail.com",
                    "message_id": "19eace2b59e6b3b8",
                    "thread_id": "19eace2b59e6b3b8",
                    "sender": "Supabase <noreply@supabase.com>",
                    "subject": "Action required: security vulnerabilities detected in your projects",
                    "category": "urgent",
                    "priority": 1,
                    "status": "active",
                    "suggested_action": "Pull exact vulnerable tables.",
                    "updated_at": "2026-06-09T14:56:41Z",
                }
            ]
        }
    )

    rendered = _runtime(store)._render_recent_gmail_context()

    assert rendered is not None
    assert "account_id=acc_a83a2c5b1199" in rendered
    assert "account_email=uspraveenraj@gmail.com" in rendered
    assert "thread_id=19eace2b59e6b3b8" in rendered
    assert "account=uspraveenraj@gmail.com" not in rendered
    assert "use account_email as account_hint" in rendered


def test_recent_gmail_context_includes_just_notified_item_over_stale_backlog() -> None:
    """Reproduces a real production incident: COSMIC emailed the user about a
    $120 Chase payment, the user replied "It was me" seconds later, and COSMIC
    answered about unrelated GitHub 2FA/OAuth alerts from two days earlier.

    Notifying an item flips its status from active to notified, and this
    context block only ever listed status=active items - so the very item
    COSMIC had just told the user about was the one thing excluded from the
    context used to interpret their reply, while a large backlog of never-sent
    active items (338 of them in production) remained. The just-notified item
    must be present, and must not be crowded out by older active entries."""
    stale_backlog = [
        {
            "surfaced_id": f"gmail_stale_{index}",
            "subject": f"[GitHub] Stale backlog alert {index}",
            "status": "active",
            "priority": 95,
            "updated_at": f"2026-08-03T02:4{index}:00Z",
        }
        for index in range(5)
    ]
    store = _FakeGmailContextStore(
        {
            "active": stale_backlog,
            "notified": [
                {
                    "surfaced_id": "gmail_chase_120",
                    "subject": "Your credit card payment is scheduled",
                    "sender": "Chase <no-reply@chase.com>",
                    "status": "notified",
                    "priority": 2,
                    "updated_at": "2026-08-05T02:39:47Z",
                }
            ],
        }
    )

    rendered = _runtime(store)._render_recent_gmail_context()

    assert rendered is not None
    assert "Your credit card payment is scheduled" in rendered
    assert "status=notified" in rendered
    # The just-notified item is the newest, so it must lead the list.
    chase_position = rendered.index("gmail_chase_120")
    first_stale_position = rendered.index("gmail_stale_")
    assert chase_position < first_stale_position
    # And the model must be told how to resolve a bare "it was me" reply.
    assert "it was me" in rendered


def test_merge_surfaced_gmail_items_reserves_slots_and_dedupes() -> None:
    reserved = [{"surfaced_id": "a", "updated_at": "2026-08-01T00:00:00Z"}]
    filler = [
        {"surfaced_id": "a", "updated_at": "2026-08-01T00:00:00Z"},
        {"surfaced_id": "b", "updated_at": "2026-08-09T00:00:00Z"},
        {"surfaced_id": "c", "updated_at": "2026-08-08T00:00:00Z"},
    ]

    merged = GatewayRuntime._merge_surfaced_gmail_items(reserved, filler, limit=2)

    ids = [item["surfaced_id"] for item in merged]
    # "a" is reserved so it survives even though b/c are newer, and it is not
    # duplicated despite appearing in both buckets.
    assert "a" in ids
    assert len(ids) == 2
    assert len(set(ids)) == 2
    # Presentation order is newest-first.
    assert ids == sorted(
        ids,
        key=lambda item_id: next(
            str(entry.get("updated_at")) for entry in merged if entry["surfaced_id"] == item_id
        ),
        reverse=True,
    )
