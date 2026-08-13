"""Durable memory must be resolved against the scratchpad at render time.

The scratchpad restates world facts it does not own, so a correction to the
original cannot reach the copy. Rather than validating the copy on a cadence,
the heartbeat now retrieves memory using the notes' own claims as the query and
renders the result beside them, with memory declared the winner.

That turns "the model might notice the contradiction" into "the contradiction
is in front of it, every beat, next to the line that caused it".
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from gateway.runtime import GatewayRuntime  # noqa: E402

NOTES = "\n".join(
    [
        "# COSMIC Heartbeat Notes",
        "## Active watchpoints",
        "- Waters at Chenal lease ends TODAY Sat Aug 8: surfaced 8/7, no user reply.",
        "## Suppression log",
        "- 8/8 11:43 UTC: Suppressed. No material change since 11:13 beat.",
    ]
)

CORRECTION = {
    "memory_id": "mem_239c20702b2d46aea466a5c20fabb875",
    "title": "Waters at Chenal lease - extended to Dec 8, 2026",
    "content": "There is NO Aug 8, 2026 move-out deadline. Do not surface move-out reminders for Aug 8.",
    "updated_at": "2026-08-07T13:24:11Z",
}


class _MemoryClient:
    def __init__(self, *, enabled=True, response=None, raises=None):
        self.enabled = enabled
        self._response = response
        self._raises = raises
        self.queries: list[str] = []

    async def passive_search(self, payload):
        self.queries.append(payload.get("query", ""))
        if self._raises:
            raise self._raises
        return self._response


class _Config:
    cosmic_memory_passive_kinds = ("user_data", "agent_note")


def _runtime(memory_client):
    runtime = object.__new__(GatewayRuntime)
    runtime.memory_client = memory_client
    runtime.config = _Config()
    return runtime


def _reconcile(runtime, notes=NOTES):
    return asyncio.run(runtime._build_heartbeat_notes_reconciliation(notes))


def test_the_correcting_memory_is_pulled_in_using_the_notes_as_the_query() -> None:
    client = _MemoryClient(response={"items": [CORRECTION]})
    items = _reconcile(_runtime(client))

    assert "Waters at Chenal" in client.queries[0]
    assert items[0]["memory_id"] == CORRECTION["memory_id"]
    assert "NO Aug 8" in items[0]["content"]


def test_beat_log_lines_do_not_pollute_the_query() -> None:
    client = _MemoryClient(response={"items": []})
    _reconcile(_runtime(client))
    assert "No material change" not in client.queries[0]


def test_a_memory_outage_never_blocks_a_beat() -> None:
    client = _MemoryClient(raises=RuntimeError("memory service down"))
    assert _reconcile(_runtime(client)) == []


def test_disabled_memory_skips_the_call_entirely() -> None:
    client = _MemoryClient(enabled=False, response={"items": [CORRECTION]})
    assert _reconcile(_runtime(client)) == []
    assert client.queries == []


def test_empty_notes_never_send_a_blank_query() -> None:
    client = _MemoryClient(response={"items": [CORRECTION]})
    assert _reconcile(_runtime(client), notes="") == []
    assert client.queries == []


def test_malformed_responses_are_tolerated() -> None:
    for response in ({}, {"items": None}, {"items": ["not-a-dict", 7]}, None):
        client = _MemoryClient(response=response)
        assert _reconcile(_runtime(client)) == []


def test_the_result_is_bounded() -> None:
    many = [dict(CORRECTION, memory_id=f"mem_{i}") for i in range(40)]
    items = _reconcile(_runtime(_MemoryClient(response={"items": many})))
    assert len(items) <= 5
    assert all(len(item["content"]) <= 320 for item in items)


class TestRender:
    def _render(self, packet):
        runtime = object.__new__(GatewayRuntime)
        return runtime._render_heartbeat_context_block(packet) or ""

    def test_memory_is_rendered_next_to_the_notes_that_contradict_it(self) -> None:
        block = self._render(
            {"heartbeat_notes": NOTES, "heartbeat_notes_reconciliation": [CORRECTION]}
        )
        notes_at = block.index("Waters at Chenal lease ends TODAY")
        memory_at = block.index("NO Aug 8")
        assert notes_at < memory_at, "the correction must follow the claim it corrects"

    def test_precedence_is_stated_explicitly(self) -> None:
        """Without this the model has two conflicting sources and no rule."""
        block = self._render(
            {"heartbeat_notes": NOTES, "heartbeat_notes_reconciliation": [CORRECTION]}
        )
        assert "the memory wins" in block
        assert "scratchpad, not a source of truth" in block
        assert "stop surfacing it" in block

    def test_the_model_is_told_to_repair_the_note_in_the_same_beat(self) -> None:
        """Otherwise the stale line survives to contaminate the next beat."""
        block = self._render(
            {"heartbeat_notes": NOTES, "heartbeat_notes_reconciliation": [CORRECTION]}
        )
        assert "fix the note in this same beat" in block

    def test_notes_still_render_when_reconciliation_is_absent(self) -> None:
        """Memory being down must not remove the notes themselves."""
        block = self._render({"heartbeat_notes": NOTES})
        assert "Waters at Chenal lease ends TODAY" in block
        assert "Durable Memory On These Same Subjects" not in block

    def test_an_empty_reconciliation_list_renders_no_empty_section(self) -> None:
        block = self._render(
            {"heartbeat_notes": NOTES, "heartbeat_notes_reconciliation": []}
        )
        assert "Durable Memory On These Same Subjects" not in block

    def test_enforced_retractions_are_rendered_before_the_notes(self) -> None:
        block = self._render(
            {
                "heartbeat_notes": NOTES,
                "heartbeat_note_retractions": [
                    {"invalidates": "coprlab.com", "canonical": "copprlab.com"}
                ],
            }
        )
        assert "Gateway-Enforced Watchpoint Corrections" in block
        assert block.index("coprlab.com → copprlab.com") < block.index(
            "Waters at Chenal lease ends TODAY"
        )
        assert "Do not probe, scrape, or alert" in block

    def test_unstructured_reconciliation_does_not_claim_gateway_enforcement(self) -> None:
        """Related memory beside the notes is still advisory. Only structured retractions strike."""
        block = self._render(
            {"heartbeat_notes": NOTES, "heartbeat_notes_reconciliation": [CORRECTION]}
        )
        assert "Gateway-Enforced Watchpoint Corrections" not in block
        assert "Waters at Chenal lease ends TODAY" in block
        assert "the memory wins" in block

    def test_a_malformed_reconciliation_entry_cannot_break_the_render(self) -> None:
        block = self._render(
            {
                "heartbeat_notes": NOTES,
                "heartbeat_notes_reconciliation": ["junk", {}, CORRECTION],
            }
        )
        assert "NO Aug 8" in block
