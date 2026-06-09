from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from gateway.runtime import GatewayRuntime


class _FakeGmailContextStore:
    def list_recent(self, *, limit: int, lookback_hours: int) -> list[dict[str, object]]:
        assert limit == 5
        assert lookback_hours == 96
        return [
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
                "suggested_action": "Pull exact vulnerable tables.",
                "updated_at": "2026-06-09T14:56:41Z",
            }
        ]


def test_recent_gmail_context_renders_stable_account_reference() -> None:
    runtime = GatewayRuntime.__new__(GatewayRuntime)
    runtime.gmail_context_store = _FakeGmailContextStore()

    rendered = runtime._render_recent_gmail_context()

    assert rendered is not None
    assert "account_id=acc_a83a2c5b1199" in rendered
    assert "account_email=uspraveenraj@gmail.com" in rendered
    assert "thread_id=19eace2b59e6b3b8" in rendered
    assert "account=uspraveenraj@gmail.com" not in rendered
    assert "use account_email as account_hint" in rendered

