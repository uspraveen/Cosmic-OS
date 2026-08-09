"""An email exchange has to reach an already-open desktop, not just a refetch.

Inbound email is the one channel that gets its own `email-thread:<mailbox>:<id>`
session, so the orchestrator answers with thread-scoped context. Desktop clients
subscribe to the day session. The realtime broadcast targeted the storage
session by exact match, so it reached nobody: a whole exchange - user email,
Alpha run, reply - existed in the database and on the history endpoint, and
simply never appeared in a desktop that was already open.

The history merge knew about thread sessions. The live path did not. This pins
both halves back together.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from gateway.channels.desktop import DesktopAdapter  # noqa: E402
from gateway.runtime import GatewayRuntime  # noqa: E402

DAY_SESSION = "sess_20260809"
THREAD_SESSION = "email-thread:iamcosmic001@mail.thelearnchain.com:144f5032"
SUBJECT = "Portfolio site"


class _Adapter(DesktopAdapter):
    """Records what session it was asked to reach.

    Really subclasses DesktopAdapter because the broadcast loop filters on
    isinstance - a duck-typed stub is silently skipped, which would have made
    every test here pass against the unfixed code.
    """

    def __init__(self):  # deliberately not calling super(): no sockets needed
        self.calls: list[tuple[str, dict]] = []

    async def broadcast_to_session(self, session_id, event):
        self.calls.append((session_id, event))


class _Registry:
    def __init__(self, adapters):
        self.adapters = adapters


def _runtime(adapter):
    runtime = object.__new__(GatewayRuntime)
    runtime.registry = _Registry({"desktop": adapter})
    runtime._current_session_id = lambda: DAY_SESSION
    runtime._email_thread_subject = lambda _sid: SUBJECT
    runtime._channel_platform = lambda channel: "agent-email"
    runtime._hydrate_artifact_list_for_client = lambda items: []
    runtime._build_client_response_blocks = lambda **kwargs: []
    return runtime


def _broadcast(runtime, session_id, **kwargs):
    payload = {
        "role": "user",
        "content": "Why don't you add a section about my projects?",
        "channel": "agent-email:iamcosmic001@mail.thelearnchain.com",
    }
    payload.update(kwargs)
    asyncio.run(
        runtime._broadcast_cross_channel_to_realtime_clients(session_id, **payload)
    )


def test_an_email_thread_message_reaches_the_day_session_clients() -> None:
    """The bug in one assertion: nobody is subscribed to the thread session."""
    adapter = _Adapter()
    _broadcast(_runtime(adapter), THREAD_SESSION)

    assert adapter.calls, "the message reached no client at all"
    target, _event = adapter.calls[0]
    assert target == DAY_SESSION


def test_the_event_reports_the_day_session_not_the_thread_session() -> None:
    """The desktop treats a changed session_id as a rollover and clears the
    transcript. Sending the thread id would have wiped the whole view."""
    adapter = _Adapter()
    _broadcast(_runtime(adapter), THREAD_SESSION)

    _target, event = adapter.calls[0]
    assert event["session_id"] == DAY_SESSION


def test_the_thread_tags_match_what_the_history_endpoint_sets() -> None:
    """Same two fields, so a live message and a refetched one group together."""
    adapter = _Adapter()
    _broadcast(_runtime(adapter), THREAD_SESSION)

    _target, event = adapter.calls[0]
    assert event["email_thread_id"] == THREAD_SESSION
    assert event["email_thread_subject"] == SUBJECT


def test_ordinary_sessions_are_completely_unaffected() -> None:
    """WhatsApp and Telegram already worked; they must keep working."""
    adapter = _Adapter()
    _broadcast(_runtime(adapter), DAY_SESSION)

    target, event = adapter.calls[0]
    assert target == DAY_SESSION
    assert event["session_id"] == DAY_SESSION
    assert "email_thread_id" not in event
    assert "email_thread_subject" not in event


def test_the_originating_platform_is_still_skipped() -> None:
    adapter = _Adapter()
    runtime = _runtime(adapter)
    runtime._channel_platform = lambda channel: "desktop"
    _broadcast(runtime, THREAD_SESSION)

    assert adapter.calls == []


def test_a_missing_subject_does_not_drop_the_message() -> None:
    adapter = _Adapter()
    runtime = _runtime(adapter)
    runtime._email_thread_subject = lambda _sid: ""
    _broadcast(runtime, THREAD_SESSION)

    _target, event = adapter.calls[0]
    assert event["email_thread_id"] == THREAD_SESSION
    assert event["email_thread_subject"] == ""


def test_the_assistant_reply_is_broadcast_the_same_way() -> None:
    """The user's email and Alpha's answer both have to arrive."""
    adapter = _Adapter()
    _broadcast(
        _runtime(adapter),
        THREAD_SESSION,
        role="assistant",
        content="Done - the section is live and a reply is drafted for you.",
    )

    target, event = adapter.calls[0]
    assert target == DAY_SESSION
    assert event["role"] == "assistant"
    assert event["email_thread_id"] == THREAD_SESSION


def test_exact_match_targeting_is_what_the_adapter_really_does() -> None:
    """Guards the assumption the whole fix rests on: DesktopAdapter compares
    session ids exactly, so a thread-session broadcast reaches zero clients."""
    import inspect

    source = inspect.getsource(DesktopAdapter.broadcast_to_session)
    assert "conn.session_id == session_id" in source
