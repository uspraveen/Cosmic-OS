"""Who a message is from is decided in three files, and they must agree.

For months "email me X" put COSMIC's own words into a draft inside the user's
Gmail rather than sending from COSMIC's mailbox. No single file was wrong; the
three surfaces that decide routing simply never covered the case:

  * orchestrator policies said what to do for *explicit Gmail* requests and
    nothing at all about who sends a message COSMIC is writing to the user;
  * the Gmail card claimed "compose, draft, reply" with no boundary;
  * the Agent Email card carried a blanket "do not use this for cron or
    heartbeat delivery", which read as "not my job" on every automation turn.

So the orchestrator picked Gmail, and because `gmail.draft_reply` stamps reply
headers from whatever thread is in context, a Chase balance alert produced a
note to the user filed as a reply to Chase.

These are prompt files with no runtime behaviour of their own, so this test
cannot prove the model routes correctly. What it does prove is that the three
surfaces still say the same thing -- the failure mode was them disagreeing.
"""

from __future__ import annotations

from pathlib import Path

import yaml

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _usage_hints(card_path: Path, intent_name: str) -> str:
    card = yaml.safe_load(card_path.read_text(encoding="utf-8"))
    intent = next(item for item in card["intents"] if item["name"] == intent_name)
    return "\n".join(intent.get("usage_hints") or []).lower()


def test_orchestrator_policy_names_the_sender_for_mail_cosmic_writes() -> None:
    from orchestrator.prompts import build_agentic_system_prompt

    prompt = build_agentic_system_prompt().lower()

    # The rule has to reach the assembled system prompt, not just sit in the file.
    assert "whose mailbox an email leaves from" in prompt
    assert "email.handle" in prompt
    # The channel a request arrived on must not be allowed to decide the sender:
    # that reasoning is exactly how cron and automation turns fell through.
    assert "does not decide who the sender is" in prompt
    # The one legitimate exception, so the rule cannot be read as "always call it".
    assert "agent-email:" in prompt


def test_orchestrator_policy_forbids_drafting_cosmics_own_mail_into_user_gmail() -> None:
    from orchestrator.prompts import build_agentic_system_prompt

    prompt = build_agentic_system_prompt().lower()
    assert "never create a gmail draft in the user's own account" in prompt
    # The reply-header consequence is the part that made this land in Chase's
    # thread, and it is the reason the prohibition is absolute rather than a
    # preference.
    assert "in-reply-to" in prompt


def test_orchestrator_policy_still_owns_the_users_own_correspondence() -> None:
    """The fix must not push the user's mail to other people out of their mailbox."""
    from orchestrator.prompts import build_agentic_system_prompt

    prompt = build_agentic_system_prompt().lower()
    assert "gmail.draft_reply" in prompt
    assert "when the *user* is the author" in prompt


def test_gmail_card_sends_owner_directed_mail_to_the_email_specialist() -> None:
    hints = _usage_hints(
        BACKEND_ROOT / "agents" / "gmail_agent" / "agent_card.yaml", "gmail.draft_reply"
    )
    assert "email.handle" in hints, "Gmail card must redirect COSMIC-authored mail"
    assert "user's own mailbox" in hints
    # Naming the destination address is what made "send a hi to my email
    # (uspraveen)" look like a Gmail request.
    assert "even when the user names their own gmail address" in hints


def test_email_card_claims_mail_cosmic_sends_the_user() -> None:
    hints = _usage_hints(
        BACKEND_ROOT / "agents" / "email_agent" / "agent_card.yaml", "email.handle"
    )
    assert "cosmic's own mailbox" in hints
    assert "whatever channel the request arrived on" in hints


def test_email_card_no_longer_disclaims_every_cron_and_heartbeat_turn() -> None:
    """The old blanket line is what made automations avoid this specialist."""
    hints = _usage_hints(
        BACKEND_ROOT / "agents" / "email_agent" / "agent_card.yaml", "email.handle"
    )
    assert "do not use this specialist for simple already-final cron" not in hints
    # The genuine exception survives, but scoped to channel delivery only.
    assert "already being delivered to the user over an `agent-email:` channel" in hints
    assert "event-automation turns this specialist is exactly how cosmic mails" in hints


def test_the_two_cards_do_not_both_claim_the_same_job() -> None:
    """Each card points at the other for the case it does not own."""
    gmail = _usage_hints(
        BACKEND_ROOT / "agents" / "gmail_agent" / "agent_card.yaml", "gmail.draft_reply"
    )
    email = _usage_hints(
        BACKEND_ROOT / "agents" / "email_agent" / "agent_card.yaml", "email.handle"
    )
    assert "email.handle" in gmail
    assert "gmail specialist" in email
