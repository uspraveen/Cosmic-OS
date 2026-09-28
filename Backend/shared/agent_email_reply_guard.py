"""Stop a second email to the person an inbound agent-email turn is already replying to.

The gateway mails the finished answer on an ``agent-email:`` turn, and it attaches
that turn's deliverable files. A specialist send or draft to that same person is
a second copy, and a draft is what convinced the model the files never left.
Sends to anyone else, search and read, and every send that did not arrive on
``agent-email:``, are left alone.
"""

from __future__ import annotations

import re
from typing import Any

_AGENT_EMAIL_SEND_INTENTS = frozenset({"email.send", "email.handle", "email.reason"})
_EMAIL_IN_TEXT = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)
_READ_GOAL_MARKERS = (
    "search the inbox",
    "search inbox",
    "search my inbox",
    "check the inbox",
    "check inbox",
    "read the inbox",
    "read inbox",
    "read the reply",
    "latest email",
    "last email",
    "latest message",
    "last message",
)
_COMPOSE_GOAL_MARKERS = (
    "draft",
    "attach",
    "send the",
    "send them",
    "send it",
    "send this",
    "reply to",
    "reply with",
    "email the file",
    "email the files",
)
_ALPHA_MAIL_MARKERS = (
    "send an email",
    "send the email",
    "send this email",
    "send a reply",
    "attach and send",
    "mail the file",
    "mail the files",
    "email the file",
    "email the files",
)


def channel_is_agent_email(channel: str | None) -> bool:
    platform = str(channel or "").split(":", 1)[0].strip().casefold()
    return platform == "agent-email"


def normalize_email_address(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    match = _EMAIL_IN_TEXT.search(text)
    return (match.group(0) if match else text).casefold()


def recipient_emails(payload: dict[str, Any] | None) -> list[str]:
    if not isinstance(payload, dict):
        return []
    found: list[str] = []
    seen: set[str] = set()
    for key in ("to_recipients", "cc_recipients", "bcc_recipients", "to", "cc", "bcc"):
        raw = payload.get(key)
        items = raw if isinstance(raw, list) else [raw]
        for item in items:
            if isinstance(item, dict):
                candidate = item.get("email") or item.get("address")
            else:
                candidate = item
            email = normalize_email_address(candidate)
            if email and email not in seen:
                seen.add(email)
                found.append(email)
    return found


def explicit_send(intent: str, payload: dict[str, Any] | None) -> bool:
    """True when this delegation itself transmits, before a specialist infers anything."""
    if str(intent or "").strip() == "email.send":
        return True
    if not isinstance(payload, dict):
        return False
    value = payload.get("send")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes"}
    return False


def targets_inbound_sender(emails: list[str], inbound_sender_email: str | None) -> bool:
    """A send with no recipient on this channel goes to the person who wrote in.

    A named recipient who is not that person is someone else, and the send proceeds.
    """
    normalized = [email for email in (normalize_email_address(item) for item in emails) if email]
    sender = normalize_email_address(inbound_sender_email)
    if not normalized:
        return True
    if not sender:
        return False
    return any(email == sender for email in normalized)


def _goal_text(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return ""
    return " ".join(
        str(payload.get(key) or "")
        for key in ("goal", "context_brief", "query", "body", "subject")
    ).casefold()


def _is_read_goal(text: str) -> bool:
    return any(marker in text for marker in _READ_GOAL_MARKERS)


def composes_reply(intent: str, payload: dict[str, Any] | None) -> bool:
    """True when this call would draft or send a reply, not search or read mail."""
    normalized = str(intent or "").strip()
    if normalized == "email.send":
        return True
    if normalized not in _AGENT_EMAIL_SEND_INTENTS or not isinstance(payload, dict):
        return False
    if explicit_send(normalized, payload):
        return True
    if any(str(payload.get(key) or "").strip() for key in ("subject", "body", "draft_seed", "draft_id")):
        return True
    mode = str(payload.get("mode") or payload.get("mode_hint") or "").strip().casefold()
    if mode in {"compose", "reply", "send"}:
        return True
    if _artifact_ids(payload):
        return True
    text = _goal_text(payload)
    if _is_read_goal(text):
        return False
    return any(marker in text for marker in _COMPOSE_GOAL_MARKERS)


def alpha_goal_sends_mail(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    text = str(payload.get("goal") or "").casefold()
    return any(marker in text for marker in _ALPHA_MAIL_MARKERS)


def agent_email_reply_send_message(
    *,
    inbound_sender_email: str | None,
    artifact_ids: list[str] | None = None,
    via_alpha: bool = False,
) -> str:
    sender = normalize_email_address(inbound_sender_email)
    who = sender or "the person who wrote this email"
    ids = [str(item).strip() for item in (artifact_ids or []) if str(item).strip()]
    lines = [
        "Nothing was sent.",
        "Nothing was drafted.",
        "Alpha was not started." if via_alpha else "",
        f"This turn arrived on agent-email, so the finished answer is the one email to {who}.",
        "The gateway attaches this turn's deliverable files, including files the local code sandbox already returned.",
        "Do not draft a reply to check delivery, and do not tell the user the files failed to attach.",
        "A specialist or resolve result that says an artifact could not be resolved is not a delivery failure.",
    ]
    if ids:
        listed = ", ".join(ids)
        lines.append(
            "If a prior file must be on that reply and it is not already a deliverable from this turn, "
            "call artifact_redeliver once for each of these artifact ids: "
            f"{listed}. A successful redelivery means the gateway will attach it."
        )
    lines.append(
        "Do not call email.send, email.handle, or email.reason to compose or send for this person again. "
        "Do not use Alpha to send this reply. "
        "A send to someone else is still allowed when every recipient is someone else."
    )
    return " ".join(line for line in lines if line)


def blocked_agent_email_reply_send(
    *,
    channel: str | None,
    intent: str,
    payload: dict[str, Any] | None,
    inbound_sender_email: str | None,
    sending: bool | None = None,
    composing: bool | None = None,
    recipient_addresses: list[str] | None = None,
) -> str | None:
    """Return the model-facing refusal, or None when the call should proceed.

    ``composing=None`` infers a draft from the payload. Pass False when the caller
    already knows this is search or read, so a draft is not inferred again.
    """
    if not channel_is_agent_email(channel):
        return None
    normalized_intent = str(intent or "").strip()
    if normalized_intent == "alpha.execute":
        if not alpha_goal_sends_mail(payload):
            return None
        return agent_email_reply_send_message(
            inbound_sender_email=inbound_sender_email,
            artifact_ids=_artifact_ids(payload),
            via_alpha=True,
        )
    if normalized_intent not in _AGENT_EMAIL_SEND_INTENTS:
        return None
    will_send = explicit_send(normalized_intent, payload) if sending is None else bool(sending)
    will_compose = composes_reply(normalized_intent, payload) if composing is None else bool(composing)
    if not will_send and not will_compose:
        return None
    emails = recipient_addresses if recipient_addresses is not None else recipient_emails(payload)
    if not targets_inbound_sender(emails, inbound_sender_email):
        return None
    return agent_email_reply_send_message(
        inbound_sender_email=inbound_sender_email,
        artifact_ids=_artifact_ids(payload),
    )


def _artifact_ids(payload: dict[str, Any] | None) -> list[str]:
    if not isinstance(payload, dict):
        return []
    found: list[str] = []
    raw = payload.get("artifact_ids")
    if isinstance(raw, list):
        found.extend(str(item).strip() for item in raw if str(item).strip())
    artifacts = payload.get("input_artifacts")
    if isinstance(artifacts, list):
        for item in artifacts:
            if not isinstance(item, dict):
                continue
            artifact_id = str(item.get("artifact_id") or "").strip()
            if artifact_id:
                found.append(artifact_id)
    deduped: list[str] = []
    seen: set[str] = set()
    for item in found:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped
