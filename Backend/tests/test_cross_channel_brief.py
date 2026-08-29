"""Cross-channel awareness, in both directions, without merging sessions.

Email threads run in their own `email-thread:` session and the daily session
never contains their turns. The isolation is deliberate and stays. What was
missing was awareness in both directions:

- An email arriving eight minutes after "email me the link" on desktop was
  answered by a turn that could not see the request, so it guessed.
- A desktop message two minutes after Cosmic answered an email ("did you use
  the YT specialist?" right after the Parag video exchange) was answered by a
  turn that had no idea the exchange happened.

These tests pin both halves of the mirrored brief: each side sees the other's
recent activity, labelled as background, and each side excludes its own
history from the note.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from gateway.runtime import GatewayRuntime


class _SessionStore:
    def __init__(self, histories: dict | None = None, email_messages: list | None = None):
        self.histories = histories or {}
        self.email_messages = list(email_messages or [])

    def get_history_tail(self, session_id: str, limit: int = 30) -> list:
        return list(self.histories.get(session_id, []))[-limit:]

    def list_email_thread_messages_in_window(
        self, *, started_at: str, ended_at=None
    ) -> list:
        cutoff = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        recent = []
        for item in self.email_messages:
            created_raw = str(item.get("created_at") or "")
            try:
                created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if created >= cutoff:
                recent.append(item)
        return recent


def _iso(minutes_ago: float) -> str:
    return (
        (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))
        .isoformat()
        .replace("+00:00", "Z")
    )


DAY_SESSION = "sess_20260829"
EMAIL_SESSION = "email-thread:iamcosmic001@mail.thelearnchain.com:ead1af0b"


def _runtime(histories: dict, email_messages: list | None = None) -> GatewayRuntime:
    runtime = GatewayRuntime.__new__(GatewayRuntime)
    runtime.session_store = _SessionStore(histories, email_messages)  # type: ignore[attr-defined]
    runtime._current_session_id = lambda now=None: DAY_SESSION  # type: ignore[assignment]
    return runtime


DESKTOP_HISTORY = [
    {
        "role": "user",
        "content": "Use alpha and deploy this and give me the link!",
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

EMAIL_HISTORY = [
    {
        "role": "user",
        "content": "Email from: Praveen Raj U S <uspraveenraj@gmail.com>\nEmail subject: Parallel\n\nListen to this: https://youtu.be/fUcnE6pjq5w",
        "channel": "agent-email:iamcosmic001@mail.thelearnchain.com",
        "created_at": _iso(8),
    },
    {
        "role": "assistant",
        "content": "Watched it - a Sequoia podcast with Parag Agrawal on Parallel Web Systems.",
        "channel": "agent-email:iamcosmic001@mail.thelearnchain.com",
        "created_at": _iso(2),
    },
]


# ---------------------------------------------------------------------------
# Direction 1: an email turn sees recent desktop/mobile activity.
# ---------------------------------------------------------------------------


def test_email_thread_turn_sees_the_recent_desktop_request() -> None:
    runtime = _runtime({"sess_20260829": DESKTOP_HISTORY})
    brief = runtime._recent_cross_channel_brief(EMAIL_SESSION)
    assert brief is not None
    assert "Email the link to me" in brief["content"]
    assert "135.148.47.43:8001" in brief["content"]
    # Labelled as background, not as thread content.
    assert "not part of this email thread" in brief["content"].lower()


def test_email_thread_turn_does_not_inherit_stale_activity() -> None:
    old = [dict(item, created_at=_iso(600)) for item in DESKTOP_HISTORY]
    runtime = _runtime({"sess_20260829": old})
    assert runtime._recent_cross_channel_brief(EMAIL_SESSION) is None


def test_email_thread_brief_excludes_email_activity() -> None:
    """The thread's own history is already present; repeating it adds nothing."""
    runtime = _runtime(
        {
            DAY_SESSION: [
                {
                    "role": "user",
                    "content": "Where is the link",
                    "channel": "agent-email:iamcosmic001@mail.thelearnchain.com",
                    "created_at": _iso(5),
                }
            ]
        }
    )
    assert runtime._recent_cross_channel_brief(EMAIL_SESSION) is None


def test_empty_day_session_yields_no_brief() -> None:
    runtime = _runtime({DAY_SESSION: []})
    assert runtime._recent_cross_channel_brief(EMAIL_SESSION) is None


def test_email_store_failure_degrades_to_no_brief_for_email_turns() -> None:
    class _Boom:
        def get_history_tail(self, *args, **kwargs):
            raise RuntimeError("db gone")

    runtime = GatewayRuntime.__new__(GatewayRuntime)
    runtime.session_store = _Boom()  # type: ignore[attr-defined]
    runtime._current_session_id = lambda now=None: DAY_SESSION  # type: ignore[assignment]
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
    runtime = _runtime({"sess_20260829": history})
    brief = runtime._recent_cross_channel_brief(EMAIL_SESSION)
    assert brief is not None
    body_lines = [ln for ln in brief["content"].splitlines() if ln.startswith("- [")]
    assert len(body_lines) <= GatewayRuntime.CROSS_CHANNEL_BRIEF_MAX_ITEMS
    for line in body_lines:
        assert len(line) < GatewayRuntime.CROSS_CHANNEL_BRIEF_EXCERPT_CHARS + 120


# ---------------------------------------------------------------------------
# The mirror: desktop/mobile day-session turns learn what email just did.
# ---------------------------------------------------------------------------


def test_day_session_turn_sees_the_recent_email_exchange() -> None:
    runtime = _runtime({"sess_20260829": []}, email_messages=EMAIL_HISTORY)
    brief = runtime._recent_cross_channel_brief(DAY_SESSION)
    assert brief is not None
    assert "Listen to this" in brief["content"]
    assert "Parag Agrawal" in brief["content"]
    # Labelled as background, not as session content.
    assert "not in this session" in brief["content"].lower()


def test_stale_email_activity_is_excluded_from_day_session_brief() -> None:
    old = [dict(item, created_at=_iso(600)) for item in EMAIL_HISTORY]
    runtime = _runtime(
        {DAY_SESSION: DESKTOP_HISTORY}, email_messages=old
    )
    assert runtime._recent_cross_channel_brief(DAY_SESSION) is None


def test_empty_email_window_yields_no_day_session_brief() -> None:
    runtime = _runtime({DAY_SESSION: DESKTOP_HISTORY}, email_messages=[])
    assert runtime._recent_cross_channel_brief(DAY_SESSION) is None


def test_email_store_failure_degrades_to_no_brief() -> None:
    class _Boom:
        def list_email_thread_messages_in_window(self, *args, **kwargs):
            raise RuntimeError("db gone")

    runtime = GatewayRuntime.__new__(GatewayRuntime)
    runtime.session_store = _Boom()  # type: ignore[attr-defined]
    assert runtime._recent_cross_channel_brief(DAY_SESSION) is None


def test_email_thread_session_never_gets_the_day_direction() -> None:
    """The dispatcher must not hand a day-session note to a thread, or vice versa."""
    runtime = _runtime(
        {DAY_SESSION: DESKTOP_HISTORY},
        email_messages=[dict(item) for item in EMAIL_HISTORY],
    )
    day_brief = runtime._recent_cross_channel_brief(DAY_SESSION)
    assert day_brief is not None
    # Desktop activity is already in the day session; only email is news here.
    assert "Email the link to me" not in day_brief["content"]
    assert "Recent email correspondence" in day_brief["content"]

    thread_brief = runtime._recent_cross_channel_brief(EMAIL_SESSION)
    assert thread_brief is not None
    assert "Email the link to me" in thread_brief["content"]
    assert "Recent activity on the user's other channels" in thread_brief["content"]
