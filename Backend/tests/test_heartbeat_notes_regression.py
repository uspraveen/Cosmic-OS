"""The Waters at Chenal lease incident, pinned against the real file.

`fixtures/heartbeat_notes_lease_incident.md` is the exact scratchpad the
gateway was reading on 2026-08-08, copied off the VM before it was repaired.
The user had cancelled the Aug 8 move-out on Aug 7 and COSMIC had written a
correct durable memory saying so - and the heartbeat still emailed the retracted
deadline for another 30 hours.

These tests assert the two properties whose absence made that possible:
the newest notes must be visible, and the claims in the notes must be what
drives memory retrieval.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from shared.scratchpad import (  # noqa: E402
    derive_reconciliation_query,
    excerpt_head_and_tail,
)

INCIDENT_NOTES = (Path(__file__).parent / "fixtures" / "heartbeat_notes_lease_incident.md").read_text(
    encoding="utf-8"
)

# The value the gateway actually used while the incident was running.
OLD_LIMIT = 4000
NEW_LIMIT = 6000


def _old_excerpt(text: str, limit: int) -> str:
    """The previous behaviour: collapse whitespace, keep the head."""
    collapsed = " ".join(str(text or "").strip().split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: limit - 3].rstrip()}..."


def test_the_fixture_really_is_the_incident() -> None:
    assert len(INCIDENT_NOTES) > OLD_LIMIT
    assert "Waters at Chenal lease ends TODAY Sat Aug 8" in INCIDENT_NOTES
    assert "15:16 UTC" in INCIDENT_NOTES  # a late-Saturday beat


def test_the_old_path_hid_every_recent_note() -> None:
    """Characterisation of the bug: this is why the loop could not self-correct."""
    old = _old_excerpt(INCIDENT_NOTES, OLD_LIMIT)
    assert "8/8 15:16 UTC" not in old
    assert "8/8 18:21 UTC" not in old
    # ...while the stale watchpoint stayed permanently in view.
    assert "lease ends TODAY Sat Aug 8" in old


def test_the_newest_beat_is_now_visible() -> None:
    excerpt = excerpt_head_and_tail(INCIDENT_NOTES, limit=NEW_LIMIT)
    assert "8/8 18:51 UTC" in excerpt, "the most recent note must reach the prompt"


def test_standing_state_is_still_visible() -> None:
    excerpt = excerpt_head_and_tail(INCIDENT_NOTES, limit=NEW_LIMIT)
    assert "## CLOSED / RESOLVED" in excerpt
    assert "## Active watchpoints" in excerpt


def test_the_excerpt_admits_what_it_dropped() -> None:
    excerpt = excerpt_head_and_tail(INCIDENT_NOTES, limit=NEW_LIMIT)
    assert "elided" in excerpt


def test_structure_survives_instead_of_becoming_one_blob() -> None:
    excerpt = excerpt_head_and_tail(INCIDENT_NOTES, limit=NEW_LIMIT)
    assert excerpt.count("\n") > 20
    assert "\n\n" in excerpt


def test_even_at_the_old_budget_the_newest_note_survives() -> None:
    """The fix is the excerpt shape, not the larger budget."""
    excerpt = excerpt_head_and_tail(INCIDENT_NOTES, limit=OLD_LIMIT)
    assert "8/8 18:51 UTC" in excerpt


def test_retrieval_query_names_the_subject_that_was_wrong() -> None:
    """The generic production query never mentioned this, so the correcting
    memory was never retrieved. Verified against the live memory service:
    notes-derived ranked it #1, generic did not return it at all."""
    query = derive_reconciliation_query(
        excerpt_head_and_tail(INCIDENT_NOTES, limit=NEW_LIMIT)
    )
    assert "Waters at Chenal" in query


def test_retrieval_query_is_not_drowned_in_beat_log_noise() -> None:
    query = derive_reconciliation_query(
        excerpt_head_and_tail(INCIDENT_NOTES, limit=NEW_LIMIT)
    )
    assert "No material change" not in query
    assert len(query) <= 900


def test_other_live_watchpoints_are_also_reconciled() -> None:
    """Generality: this must work for any claim, not just the lease."""
    query = derive_reconciliation_query(
        excerpt_head_and_tail(INCIDENT_NOTES, limit=NEW_LIMIT)
    )
    assert "Google One" in query
