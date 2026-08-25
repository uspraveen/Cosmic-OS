"""An email turn learns what happened on other channels, without merging sessions.

The `email-thread:` isolation is deliberate and stays. What was missing was any
awareness at all: an email arriving eight minutes after "email me the link" on
desktop was answered by a turn that could not see the request, so it guessed.

These tests pin both halves -- the brief appears for email threads, and appears
nowhere else.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from gateway.runtime import GatewayRuntime


class _SessionStore:
    def __init__(self, histories: dict):
        self._histories = histories

    def get_history_tail(self, session_id: str, limit: int = 30):
        return list(self._histories.get(session_id, []))[-limit:]


def _iso(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat().replace(
        "+00:00", "Z"
    )


def _runtime(histories: dict, *, day_session: str = "sess_20260824") -> GatewayRuntime:
    runtime = GatewayRuntime.__new__(GatewayRuntime)
    runtime.session_store = _SessionStore(histories)  # type: ignore[attr-defined]
    runtime._current_session_id = lambda now=None: day_session  # type: ignore[assignment]
    return runtime


DESKTOP_HISTORY = [
    {
        "role": "user",
        "content": "Use alpha and deploy this in your vm and give me an accessible link!",
        "channel": "desktop:desk_a3f639dc299a4160",
        "created_at": _iso(12),
    },
    {
        "role": "assistant",
        "content": "Done - it's live at http://135.148.47.43:8001/",
        "channel": "desktop:desk_a3f639dc299a4160",
        "created_at": _iso(11),
    },
    {
        "role": "user",
        "content": "Email the link to me",
        "channel": "desktop:desk_a3f639dc299a4160",
        "created_at": _iso(10),
    },
]

EMAIL_SESSION = "email-thread:iamcosmic001@mail.thelearnchain.com:d15b3a23"


def test_email_thread_turn_sees_the_recent_desktop_request() -> None:
    runtime = _runtime({"sess_20260824": DESKTOP_HISTORY})
    brief = runtime._recent_cross_channel_brief(EMAIL_SESSION)
    assert brief is not None
    assert "Email the link to me" in brief["content"]
    assert "135.148.47.43:8001" in brief["content"]
    # Labelled as background, not as thread content.
    assert "not part of this email thread" in brief["content"].lower()


def test_brief_is_never_attached_to_non_email_sessions() -> None:
    """Desktop, mobile and day sessions must be completely unaffected."""
    runtime = _runtime({"sess_20260824": DESKTOP_HISTORY})
    for session_id in ("sess_20260824", "desktop:desk_abc", "mobile:mob_abc", ""):
        assert runtime._recent_cross_channel_brief(session_id) is None


def test_stale_activity_is_not_offered_as_context() -> None:
    """An hours-old request must not look like a live one."""
    old = [dict(item, created_at=_iso(600)) for item in DESKTOP_HISTORY]
    runtime = _runtime({"sess_20260824": old})
    assert runtime._recent_cross_channel_brief(EMAIL_SESSION) is None


def test_email_activity_is_excluded_from_the_brief() -> None:
    """The thread's own history is already present; repeating it adds nothing."""
    history = [
        {
            "role": "user",
            "content": "Where is the link",
            "channel": "agent-email:iamcosmic001@mail.thelearnchain.com",
            "created_at": _iso(5),
        }
    ]
    runtime = _runtime({"sess_20260824": history})
    assert runtime._recent_cross_channel_brief(EMAIL_SESSION) is None


def test_brief_is_bounded() -> None:
    history = [
        {
            "role": "user",
            "content": f"message {index} " + ("x" * 500),
            "channel": "desktop:desk_abc",
            "created_at": _iso(5),
        }
        for index in range(40)
    ]
    runtime = _runtime({"sess_20260824": history})
    brief = runtime._recent_cross_channel_brief(EMAIL_SESSION)
    assert brief is not None
    body_lines = [ln for ln in brief["content"].splitlines() if ln.startswith("- [")]
    assert len(body_lines) <= GatewayRuntime.CROSS_CHANNEL_BRIEF_MAX_ITEMS
    for line in body_lines:
        assert len(line) < GatewayRuntime.CROSS_CHANNEL_BRIEF_EXCERPT_CHARS + 120


def test_store_failure_degrades_to_no_brief() -> None:
    class _Boom:
        def get_history_tail(self, *a, **k):
            raise RuntimeError("db gone")

    runtime = GatewayRuntime.__new__(GatewayRuntime)
    runtime.session_store = _Boom()  # type: ignore[attr-defined]
    runtime._current_session_id = lambda now=None: "sess_20260824"  # type: ignore[assignment]
    assert runtime._recent_cross_channel_brief(EMAIL_SESSION) is None


def test_empty_day_session_yields_no_brief() -> None:
    runtime = _runtime({"sess_20260824": []})
    assert runtime._recent_cross_channel_brief(EMAIL_SESSION) is None
