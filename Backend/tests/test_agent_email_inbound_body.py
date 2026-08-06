"""An inbound email's full body must reach the transcript.

`content` is the orchestrator's view of an email: an 800-character excerpt
behind an `Email from: / Email subject:` header. The desktop rendered that
same string, so a long email appeared silently clipped and the header was
repeated on screen under a card that already showed the subject.

The body is now carried separately for display. The model's excerpt is
deliberately left exactly as it was - this must not move the prompt budget.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from gateway.channels.agent_email import (
    MODEL_BODY_EXCERPT_CHARS,
    STORED_BODY_CHARS,
    AgentEmailAdapter,
)


def _adapter() -> AgentEmailAdapter:
    return AgentEmailAdapter(
        cosmic_mail_base_url="http://cosmic-mail.local",
        cosmic_mail_api_token="token",
    )


def _webhook(body: str) -> dict:
    return {
        "mailbox": {"id": "mbx_1", "address": "iamcosmic001@mail.thelearnchain.com"},
        "thread": {"id": "thr_1", "subject": "Re: SPC referral"},
        "message": {
            "id": "msg_1",
            "thread_id": "thr_1",
            "subject": "Re: SPC referral",
            "text_body": body,
            "from_recipients": [{"email": "uspraveenraj@gmail.com", "name": "Praveen Raj U S"}],
        },
    }


def test_full_body_is_stored_for_display_while_content_stays_an_excerpt() -> None:
    body = "A" * (MODEL_BODY_EXCERPT_CHARS * 3)
    normalized = _adapter().normalize_message(_webhook(body))

    assert normalized["metadata"]["body_text"] == body
    assert normalized["metadata"]["body_truncated"] is False
    # The model's view is unchanged: still the bounded excerpt, still behind
    # the envelope header it relies on for sender and subject.
    assert normalized["content"].startswith("Email from: Praveen Raj U S")
    assert "Email subject: Re: SPC referral" in normalized["content"]
    assert normalized["content"].count("A") == MODEL_BODY_EXCERPT_CHARS


def test_a_short_email_is_identical_in_both_views() -> None:
    normalized = _adapter().normalize_message(_webhook("Here's the signed form."))
    assert normalized["metadata"]["body_text"] == "Here's the signed form."
    assert normalized["metadata"]["body_truncated"] is False
    assert normalized["content"].endswith("Here's the signed form.")


def test_a_pathological_email_cannot_bloat_the_session_row() -> None:
    """The display copy is bounded too - it just has a far larger budget than
    the prompt, and says so when it clips."""
    body = "B" * (STORED_BODY_CHARS + 500)
    normalized = _adapter().normalize_message(_webhook(body))

    assert len(normalized["metadata"]["body_text"]) == STORED_BODY_CHARS
    assert normalized["metadata"]["body_truncated"] is True


def test_an_empty_body_reports_no_truncation() -> None:
    normalized = _adapter().normalize_message(_webhook(""))
    assert normalized["metadata"]["body_text"] == ""
    assert normalized["metadata"]["body_truncated"] is False


def test_display_body_carries_no_envelope_header() -> None:
    """The whole point: the transcript shows the message, not the plumbing."""
    normalized = _adapter().normalize_message(_webhook("Who is Gopal Raman?"))
    body_text = normalized["metadata"]["body_text"]
    assert "Email from:" not in body_text
    assert "Email subject:" not in body_text
