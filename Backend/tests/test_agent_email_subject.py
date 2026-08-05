from __future__ import annotations

from gateway.channels.agent_email import AgentEmailAdapter


def _channel() -> AgentEmailAdapter:
    return AgentEmailAdapter(
        cosmic_mail_base_url="http://cosmic-mail.local",
        cosmic_mail_api_token="token",
    )


def test_build_subject_derives_distinct_subjects_for_unrelated_new_thread_emails() -> None:
    """Reproduces a real production incident: every brand-new proactive
    email (not a reply within an existing thread) that lacked an explicit
    subject fell back to the same fixed string "COSMIC update", regardless
    of actual topic. A financial fraud alert about a Chase payment and an
    unrelated Google Doc search thread both ended up with the identical
    subject line and sender/recipient pair, so Gmail's own conversation-view
    grouping merged them into one thread even though Cosmic Mail's internal
    thread bookkeeping correctly treated them as separate. The fix derives a
    real subject from the message content instead of a generic placeholder."""
    channel = _channel()

    chase_subject = channel._build_subject(
        {
            "type": "response.complete",
            "content": (
                "Heads-up: Chase just confirmed a scheduled $120.00 payment on your "
                "Sapphire Preferred card (ending 2785), authorized today (Aug 4)."
            ),
        }
    )
    spc_subject = channel._build_subject(
        {
            "type": "response.complete",
            "content": (
                "Found it — I'm connected to your Google Docs and pulled the full doc. "
                "There are actually two copies in your Drive."
            ),
        }
    )

    assert chase_subject != spc_subject
    assert chase_subject != "COSMIC update"
    assert spc_subject != "COSMIC update"
    assert "Chase" in chase_subject
    assert "Google Docs" in spc_subject


def test_build_subject_prefers_explicit_subject_when_present() -> None:
    channel = _channel()
    subject = channel._build_subject(
        {"type": "response.complete", "subject": "Re: Something specific", "content": "Body text."}
    )
    assert subject == "Re: Something specific"


def test_build_subject_falls_back_to_generic_string_for_empty_content() -> None:
    channel = _channel()
    assert channel._build_subject({"type": "response.complete", "content": ""}) == "COSMIC update"
    assert channel._build_subject({"type": "task.failed"}) == "COSMIC task failed"
    assert channel._build_subject({"type": "task.cancelled"}) == "COSMIC notification"


def test_derive_subject_from_content_strips_markdown_and_truncates() -> None:
    channel = _channel()
    long_line = "This is a very long first line of a response that goes well past the typical email subject length limit"
    subject = channel._derive_subject_from_content(f"**{long_line}**\n\nMore body text below.")
    assert subject.endswith("…")
    assert len(subject) <= 79
    assert "*" not in subject
