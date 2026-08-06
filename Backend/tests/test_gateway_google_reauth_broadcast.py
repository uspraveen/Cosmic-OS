"""The reauth banner must announce a problem, not nag about it.

The auth-health probe re-runs continuously, so an unsatisfied condition (e.g.
a newly required scope nobody has consented to yet) was re-broadcast on every
single probe - 582 times in one 24h window in production. The mobile push was
already deduped; the desktop/mobile socket broadcast was not.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from gateway.runtime import GatewayRuntime


class _FakeAdapter:
    platform = "desktop"

    def __init__(self) -> None:
        self.events: list[dict] = []

    async def broadcast_all(self, event):
        self.events.append(event)
        return 1


class _DeadAdapter(_FakeAdapter):
    async def broadcast_all(self, event):
        self.events.append(event)
        return 0  # nobody connected


def _runtime(adapter, monkeypatch):
    runtime = GatewayRuntime.__new__(GatewayRuntime)
    runtime._recent_google_reauth_pushes = {}
    runtime._recent_google_reauth_broadcasts = {}
    runtime.registry = type("R", (), {"adapters": {"desktop": adapter}})()
    # Only the socket broadcast is under test; keep pushes out of the way.
    monkeypatch.setattr(runtime, "_schedule_mobile_push", lambda **kw: None)
    monkeypatch.setattr(
        "gateway.runtime.DesktopAdapter", _FakeAdapter, raising=False
    )
    return runtime


def _accounts(error: str = "Unable to resolve Google credential with required scopes."):
    return [
        {
            "account_id": "acc_1",
            "email": "user@example.com",
            "display_name": "User",
            "account_label": "Google account",
            "is_primary": True,
            "status": "reauth_required",
            "needs_reconnect": True,
            "error": error,
        }
    ]


@pytest.mark.asyncio
async def test_unchanged_reauth_condition_is_announced_once(monkeypatch) -> None:
    adapter = _FakeAdapter()
    runtime = _runtime(adapter, monkeypatch)

    first = await runtime.publish_google_reauth_required(
        tool="docs", agent_id="google_docs_agent", accounts=_accounts(), status="reauth_required"
    )
    for _ in range(20):
        repeat = await runtime.publish_google_reauth_required(
            tool="docs",
            agent_id="google_docs_agent",
            accounts=_accounts(),
            status="reauth_required",
        )

    assert first["status"] == "published"
    assert repeat["status"] == "suppressed"
    assert len(adapter.events) == 1


@pytest.mark.asyncio
async def test_a_different_problem_still_gets_through(monkeypatch) -> None:
    """Debouncing must not swallow a genuinely new failure."""
    adapter = _FakeAdapter()
    runtime = _runtime(adapter, monkeypatch)

    await runtime.publish_google_reauth_required(
        tool="docs", agent_id="a", accounts=_accounts(), status="reauth_required"
    )
    await runtime.publish_google_reauth_required(
        tool="docs", agent_id="a", accounts=_accounts("Token has been revoked."), status="reauth_required"
    )
    # A different tool is a different user-visible problem too.
    await runtime.publish_google_reauth_required(
        tool="gmail", agent_id="a", accounts=_accounts(), status="reauth_required"
    )

    assert len(adapter.events) == 3


@pytest.mark.asyncio
async def test_broadcast_into_an_empty_room_is_not_suppressed(monkeypatch) -> None:
    """If no client was listening the user never saw it, so the next probe
    must be allowed to deliver it rather than assuming it landed."""
    adapter = _DeadAdapter()
    runtime = _runtime(adapter, monkeypatch)
    monkeypatch.setattr("gateway.runtime.DesktopAdapter", _DeadAdapter, raising=False)

    for _ in range(3):
        await runtime.publish_google_reauth_required(
            tool="docs", agent_id="a", accounts=_accounts(), status="reauth_required"
        )

    assert len(adapter.events) == 3


@pytest.mark.asyncio
async def test_healthy_accounts_publish_nothing(monkeypatch) -> None:
    adapter = _FakeAdapter()
    runtime = _runtime(adapter, monkeypatch)
    result = await runtime.publish_google_reauth_required(
        tool="docs",
        agent_id="a",
        accounts=[{"account_id": "acc_1", "status": "healthy", "needs_reconnect": False}],
        status="healthy",
    )
    assert result["status"] == "ignored"
    assert adapter.events == []
