from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from gateway.runtime import GatewayRuntime


class _FakeGmailContextStore:
    """Serves surfaced items partitioned by status, like the real store:
    filtered by the requested statuses, ordered newest-first, then limited."""

    def __init__(self, by_status: dict[str, list[dict[str, object]]] | None = None) -> None:
        self._by_status = by_status or {}
        self.requested_statuses: list[str] | None = None
        self.requested_limit: int | None = None

    def list_recent(
        self,
        *,
        limit: int,
        lookback_hours: int,
        statuses: list[str] | None = None,
        status: str = "active",
    ) -> list[dict[str, object]]:
        assert lookback_hours == 96
        wanted = list(statuses) if statuses is not None else [status]
        self.requested_statuses = wanted
        self.requested_limit = limit
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

    Notifying an item flips its status active -> notified, and this block was
    filtered to status=active only - so the very item COSMIC had just told the
    user about was the one thing excluded from the context used to interpret
    their reply, while a backlog of never-sent active items (338 of them in
    production) remained. Delivery state must not gate membership."""
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
    # mark_status() bumps updated_at, so the just-notified item is the newest
    # row by construction and recency ordering alone must put it first.
    assert rendered.index("gmail_chase_120") < rendered.index("gmail_stale_")


def test_recent_gmail_context_excludes_suppressed_and_self_items() -> None:
    """Guards against context bloat: suppressed items were deliberately judged
    not worth showing, and "self" items are COSMIC's own outbound mail. The
    user never saw either, so neither is a plausible referent and neither
    belongs in this block."""
    store = _FakeGmailContextStore(
        {
            "active": [
                {
                    "surfaced_id": "gmail_active",
                    "subject": "A real pending item",
                    "status": "active",
                    "updated_at": "2026-08-01T00:00:00Z",
                }
            ],
            "suppressed": [
                {
                    "surfaced_id": "gmail_suppressed",
                    "subject": "Deliberately not surfaced",
                    "status": "suppressed",
                    "updated_at": "2026-08-09T00:00:00Z",
                }
            ],
            "self": [
                {
                    "surfaced_id": "gmail_self",
                    "subject": "COSMIC update",
                    "status": "self",
                    "updated_at": "2026-08-09T00:00:00Z",
                }
            ],
        }
    )

    rendered = _runtime(store)._render_recent_gmail_context()

    assert rendered is not None
    assert store.requested_statuses == ["active", "notified"]
    assert "gmail_active" in rendered
    assert "gmail_suppressed" not in rendered
    assert "gmail_self" not in rendered


def test_recent_gmail_context_does_not_grow_the_block() -> None:
    """The fix must not enlarge the prompt: one query, same item budget."""
    store = _FakeGmailContextStore(
        {
            "active": [
                {
                    "surfaced_id": f"gmail_active_{index}",
                    "subject": f"Active {index}",
                    "status": "active",
                    "updated_at": f"2026-08-0{index + 1}T00:00:00Z",
                }
                for index in range(4)
            ],
            "notified": [
                {
                    "surfaced_id": f"gmail_notified_{index}",
                    "subject": f"Notified {index}",
                    "status": "notified",
                    "updated_at": f"2026-08-1{index}T00:00:00Z",
                }
                for index in range(4)
            ],
        }
    )

    rendered = _runtime(store)._render_recent_gmail_context(limit=5)

    assert rendered is not None
    assert store.requested_limit == 5
    item_lines = [line for line in rendered.splitlines() if line.startswith("- #")]
    assert len(item_lines) == 5
