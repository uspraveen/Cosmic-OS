"""A turn that ends on a blocking card is awaiting the user, whatever the prose said.

`<awaiting_reply/>` is the model's own judgement for the prose case ("pick one
of these"). The card case has a hard signal instead: vault permission,
sandbox permission, browser credentials and the slide workflow choice each
park the turn on a user action, and the work resumes as a continuation with
no visible user message in between. The desktop groups that resumed work
under the same task on `awaiting_reply`, so the flag must be set for those
turns even when the model forgot the tag.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

from orchestrator.runtime import OrchestratorRuntime, _BLOCKING_CARD_BLOCK_TYPES  # noqa: E402


def _result(block_type: str | None, **extra) -> str:
    payload: dict = {"status": "permission_required", **extra}
    if block_type is not None:
        payload["_cosmic_ui"] = {
            "version": 1,
            "render": "trusted_inline_block",
            "block_type": block_type,
            "response_mode": "brief_acknowledgement",
        }
    return json.dumps(payload)


def test_every_blocking_card_parks_the_turn():
    for block_type in sorted(_BLOCKING_CARD_BLOCK_TYPES):
        assert OrchestratorRuntime._tool_result_raises_blocking_card(_result(block_type)) is True, block_type


def test_the_four_blocking_cards_are_exactly_the_ones_with_a_continuation_path():
    # Each of these resumes the turn through the gateway once the user acts.
    # Adding one here means adding its continuation too; removing one means
    # the desktop stops grouping its resumed work.
    assert _BLOCKING_CARD_BLOCK_TYPES == {
        "vault_permission_request",
        "sandbox_permission_request",
        "browser_credential_request",
        "slide_workflow_choice",
    }


def test_the_gateway_shaped_vault_reply_counts_too():
    # /internal/vault/lookup attaches its own _cosmic_ui (no response_mode);
    # the executor only overwrites it for desktop/mobile channels.
    payload = {
        "status": "permission_required",
        "request_id": "vault_req_1",
        "_cosmic_ui": {"render": "trusted_inline_block", "block_type": "vault_permission_request"},
    }
    assert OrchestratorRuntime._tool_result_raises_blocking_card(json.dumps(payload)) is True


def test_brief_acknowledgement_alone_is_not_a_block():
    # Content cards and email draft approvals ask for a brief acknowledgement
    # but the turn is done; the deliverable is the card.
    for block_type in ("content_card", "gmail_draft_approval", "agent_email_draft_approval", "calendar_event"):
        assert OrchestratorRuntime._tool_result_raises_blocking_card(_result(block_type)) is False, block_type


def test_plain_results_and_junk_are_not_blocks():
    assert OrchestratorRuntime._tool_result_raises_blocking_card(_result(None)) is False
    assert OrchestratorRuntime._tool_result_raises_blocking_card(json.dumps({"ok": True})) is False
    assert OrchestratorRuntime._tool_result_raises_blocking_card("not json at all") is False
    assert OrchestratorRuntime._tool_result_raises_blocking_card(json.dumps([1, 2])) is False
    assert OrchestratorRuntime._tool_result_raises_blocking_card(json.dumps({"_cosmic_ui": "string"})) is False


def test_the_flag_is_not_reset_inside_the_tool_loop():
    """`awaiting_user_card` must outlive the iteration that set it.

    A card is raised by a tool result partway through a turn, and the turn
    keeps looping afterwards. Initialising the flag inside `while iteration <
    max_iterations` would clear it on the next pass and the turn would report
    itself finished — the desktop would then draw the resumed work as a new
    task, which is the whole defect this flag exists to fix. Structural rather
    than textual, so it survives reformatting.

    The orchestrator's streaming tests cannot cover this today: 11 of them
    fail on main for unrelated reasons, so a regression here would not show up.
    """
    import ast

    tree = ast.parse((BACKEND_ROOT / "orchestrator" / "runtime.py").read_text(encoding="utf-8"))

    def assignments_under(node) -> int:
        return sum(
            1
            for child in ast.walk(node)
            for target in getattr(child, "targets", [])
            if isinstance(child, ast.Assign)
            and isinstance(target, ast.Name)
            and target.id == "awaiting_user_card"
        )

    total = assignments_under(tree)
    in_loops = sum(assignments_under(node) for node in ast.walk(tree) if isinstance(node, ast.While))
    # Two `= False` initialisations (one per streaming path), each outside its
    # loop; two `= True` sets, each inside. Anything else means the wiring moved.
    assert total == 4, f"expected 4 assignments, found {total}"
    assert in_loops == 2, f"expected exactly the 2 `= True` sets inside loops, found {in_loops}"
