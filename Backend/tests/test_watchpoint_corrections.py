"""Watchpoint retractions must be lexical and explicit, never inferred."""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from shared.watchpoint_corrections import (  # noqa: E402
    apply_retractions_to_notes,
    extract_hostname_corrections,
    retraction_from_memory_payload,
    sanitize_invalidate_phrases,
    upsert_retraction,
)

NOTES = "\n".join(
    [
        "# COSMIC Heartbeat Notes",
        "## Active watchpoints",
        "- coprlab.com ICANN verification: ~3-4 days left (as of 8/7). Watch.",
        "- Google One payment declined.",
        "## Suppression log",
        "- 8/12 17:05 UTC: DELIVERED. coprlab.com DNS resolution FAILING.",
        "- 8/12 18:08 UTC: Suppressed. No material change since 17:38 UTC beat.",
        "",
    ]
)


def test_extracts_the_coppr_email_correction() -> None:
    text = (
        "It's not coprlab.com but copprlab.com On Sun, Aug 9, 2026 at 2:01 PM "
        "Cosmic 001 wrote: Heads up — coprlab.com just failed DNS"
    )
    pairs = extract_hostname_corrections(text)
    assert pairs == [{"invalidates": "coprlab.com", "canonical": "copprlab.com"}]


def test_does_not_extract_non_host_not_but() -> None:
    assert extract_hostname_corrections("It's not ready but soon") == []
    assert extract_hostname_corrections("not this but that") == []


def test_rejects_short_invalidate_phrases() -> None:
    assert sanitize_invalidate_phrases(["down", "com", "lab.com"]) == []
    assert "coprlab.com" in sanitize_invalidate_phrases(["coprlab.com"])


def test_unstructured_memory_is_not_a_retraction() -> None:
    """A related session summary must not be treated as a strike instruction."""
    assert (
        retraction_from_memory_payload(
            {
                "title": "Session summary sess_20260809",
                "content": "Transient DNS outage on coprlab.com, now resolved.",
            }
        )
        is None
    )


def test_structured_memory_write_is_a_retraction() -> None:
    parsed = retraction_from_memory_payload(
        {
            "kind": "user_data",
            "content": "The site is copprlab.com, not coprlab.com.",
            "invalidates": ["coprlab.com"],
            "canonical": "copprlab.com",
        }
    )
    assert parsed is not None
    assert "coprlab.com" in parsed["invalidates"]
    assert parsed["canonical"] == "copprlab.com"


def test_standing_watchpoint_is_struck_log_is_not() -> None:
    new_text, changed, applied = apply_retractions_to_notes(
        NOTES,
        [{"invalidates": ["coprlab.com"], "canonical": "copprlab.com"}],
    )
    assert changed
    assert applied == [{"invalidates": "coprlab.com", "canonical": "copprlab.com"}]
    assert "coprlab.com ICANN verification" not in new_text
    assert "SUPERSEDED: do not watch coprlab.com; canonical is copprlab.com." in new_text
    assert "Google One payment declined." in new_text
    assert "DELIVERED. coprlab.com DNS resolution FAILING." in new_text


def test_canonical_line_is_not_destroyed() -> None:
    notes = "- Watch copprlab.com (YC venture). Ignore coprlab.com.\n"
    new_text, changed, applied = apply_retractions_to_notes(
        notes,
        [{"invalidates": ["coprlab.com"], "canonical": "copprlab.com"}],
    )
    assert not changed
    assert applied == []
    assert new_text == notes


def test_copprlab_is_not_a_substring_hit_for_coprlab() -> None:
    notes = "- copprlab.com ICANN verification: watch.\n"
    new_text, changed, _applied = apply_retractions_to_notes(
        notes,
        [{"invalidates": ["coprlab.com"], "canonical": "example.com"}],
    )
    assert not changed
    assert new_text == notes


def test_empty_retractions_leave_notes_untouched() -> None:
    new_text, changed, applied = apply_retractions_to_notes(NOTES, [])
    assert new_text == NOTES
    assert not changed
    assert applied == []


def test_upsert_is_idempotent() -> None:
    first = upsert_retraction(
        [],
        {"invalidates": ["coprlab.com"], "canonical": "copprlab.com", "source": "user_text"},
    )
    second = upsert_retraction(
        first,
        {"invalidates": ["coprlab.com"], "canonical": "copprlab.com", "source": "memory_write"},
    )
    assert len(second) == 1
    assert second[0]["source"] == "memory_write"


def test_gateway_enforces_a_harvested_user_correction(tmp_path) -> None:
    from gateway.runtime import GatewayRuntime
    from gateway.session_store import SessionStore

    notes_path = tmp_path / "heartbeat_notes.md"
    notes_path.write_text(NOTES, encoding="utf-8")
    store = SessionStore(tmp_path / "sessions.db")
    store.initialize()
    store.append_message(
        "email-thread:iamcosmic001@mail.thelearnchain.com:thr_coppr",
        role="user",
        content="It's not coprlab.com but copprlab.com",
        channel="agent-email:iamcosmic001@mail.thelearnchain.com",
    )

    runtime = GatewayRuntime.__new__(GatewayRuntime)
    runtime.config = type("Cfg", (), {"heartbeat_notes_path": notes_path})()
    runtime.session_store = store

    enforced, applied = runtime._enforce_heartbeat_notes_retractions(NOTES)
    assert applied == [{"invalidates": "coprlab.com", "canonical": "copprlab.com"}]
    assert "coprlab.com ICANN verification" not in enforced
    assert "SUPERSEDED: do not watch coprlab.com; canonical is copprlab.com." in enforced
    assert "DELIVERED. coprlab.com DNS resolution FAILING." in enforced
    persisted = notes_path.read_text(encoding="utf-8")
    assert "SUPERSEDED: do not watch coprlab.com" in persisted
    assert "coprlab.com ICANN verification" not in persisted
