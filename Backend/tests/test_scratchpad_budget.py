"""Scratchpad budgeting must never hide the newest entries.

Production failure pinned here: `heartbeat_notes.md` grew to 10,388 characters
against a 4,000-character ambient budget that truncated from the head. Every
beat therefore read notes frozen on Aug 7 - including a move-out watchpoint the
user had cancelled - and every note written to correct it landed in the 6,371
invisible characters at the end. The heartbeat re-derived and re-sent the same
retracted deadline for roughly 30 hours.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from shared.scratchpad import (  # noqa: E402
    derive_reconciliation_query,
    excerpt_head_and_tail,
    truncate_keeping_newest,
)

STANDING = "\n".join(
    [
        "# COSMIC Heartbeat Notes",
        "## CLOSED / RESOLVED",
        "- Waters at Chenal lease: NOT ending Aug 8, extended to Dec 8 2026.",
        "## Active watchpoints",
        "- Google One payment due Sun Aug 9.",
    ]
)
LOG = "\n".join("- 8/8 %02d:00 UTC: Suppressed. No material change." % hour for hour in range(40))
NOTES = STANDING + "\n## Suppression log\n" + LOG + "\n- 8/8 23:00 UTC: NEWEST ENTRY.\n"


def test_the_newest_entry_survives_the_budget() -> None:
    """The whole incident in one assertion."""
    excerpt = excerpt_head_and_tail(NOTES, limit=600)
    assert "NEWEST ENTRY" in excerpt


def test_standing_state_survives_too() -> None:
    """A pure tail would have been just as broken in the other direction."""
    excerpt = excerpt_head_and_tail(NOTES, limit=600)
    assert "NOT ending Aug 8" in excerpt


def test_the_excerpt_says_that_it_dropped_something() -> None:
    """Silent truncation is what made a partial view look complete."""
    excerpt = excerpt_head_and_tail(NOTES, limit=600)
    assert "elided" in excerpt


def test_text_that_fits_is_returned_byte_for_byte() -> None:
    assert excerpt_head_and_tail(NOTES, limit=len(NOTES)) == NOTES
    assert excerpt_head_and_tail(NOTES, limit=len(NOTES) + 5000) == NOTES


def test_the_budget_is_actually_respected() -> None:
    for limit in (200, 400, 600, 1200, 3000):
        assert len(excerpt_head_and_tail(NOTES, limit=limit)) <= limit


def test_lines_are_never_split_mid_bullet() -> None:
    excerpt = excerpt_head_and_tail(NOTES, limit=600)
    original = set(NOTES.split("\n"))
    for line in excerpt.split("\n"):
        if not line.strip() or "elided" in line:
            continue
        assert line in original, "reconstructed a line that was never written: %r" % line


def test_markdown_structure_is_preserved() -> None:
    """The old path collapsed all whitespace, flattening the file to one blob."""
    excerpt = excerpt_head_and_tail(NOTES, limit=900)
    assert "\n" in excerpt
    assert "## CLOSED / RESOLVED" in excerpt


def test_degenerate_budgets_do_not_explode() -> None:
    assert excerpt_head_and_tail(NOTES, limit=0) == ""
    assert excerpt_head_and_tail(NOTES, limit=-10) == ""
    assert len(excerpt_head_and_tail(NOTES, limit=30)) <= 30
    assert excerpt_head_and_tail("", limit=100) == ""


def test_a_single_line_longer_than_the_budget_still_yields_something() -> None:
    monster = "x" * 5000
    out = excerpt_head_and_tail(monster, limit=100)
    assert 0 < len(out) <= 100


class TestWriteCap:
    """`text[:limit]` on an append-only file silently discards new appends."""

    def test_an_append_survives_the_cap(self) -> None:
        document = STANDING + "\n" + ("- filler line\n" * 400) + "- THE APPEND JUST MADE\n"
        assert len(document) > 2000
        capped = truncate_keeping_newest(document, limit=2000)
        assert "THE APPEND JUST MADE" in capped
        assert len(capped) <= 2000

    def test_standing_state_survives_the_cap(self) -> None:
        document = STANDING + "\n" + ("- filler line\n" * 400) + "- newest\n"
        capped = truncate_keeping_newest(document, limit=2000)
        assert "NOT ending Aug 8" in capped

    def test_a_document_under_the_cap_is_untouched(self) -> None:
        assert truncate_keeping_newest(STANDING, limit=32000) == STANDING


class TestReconciliationQuery:
    def test_it_carries_the_claims_the_notes_assert(self) -> None:
        query = derive_reconciliation_query(NOTES)
        assert "Waters at Chenal lease" in query
        assert "Google One" in query

    def test_it_drops_beat_log_noise(self) -> None:
        """Self-observation is never wrong and would drown the real claims."""
        query = derive_reconciliation_query(NOTES)
        assert "Suppressed" not in query
        assert "No material change" not in query

    def test_it_is_bounded_and_cuts_on_a_word_boundary(self) -> None:
        long_notes = "\n".join("- watchpoint number %d about something" % i for i in range(500))
        query = derive_reconciliation_query(long_notes, limit=300)
        assert len(query) <= 300
        assert not query.endswith(" ")

    def test_empty_notes_produce_no_query(self) -> None:
        """An empty query must not be sent to memory as a wildcard."""
        assert derive_reconciliation_query("") == ""
        assert derive_reconciliation_query("\n\n#\n- \n") == ""

    def test_the_elision_marker_is_not_treated_as_a_claim(self) -> None:
        excerpt = excerpt_head_and_tail(NOTES, limit=600)
        assert "elided" not in derive_reconciliation_query(excerpt)
