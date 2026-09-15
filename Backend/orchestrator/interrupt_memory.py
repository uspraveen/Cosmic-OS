"""Deterministic question memory for specialist (browser) interrupts.

The orchestrator is the part of COSMIC that remembers the user. When a browser
run is blocked on a generic question — "what's your current address?", "which
option?" — the answer usually already exists: the user typed it into an earlier
input request in this session. This module decides, without any model call,
whether that prior answer applies here.

Reuse is deliberately conservative:
- only questions with the same *topic* (address, phone, date of birth, ...) or
  an exact normalized match are auto-answered;
- secret-shaped questions (password/OTP/sign-in) are never reused, even when
  they somehow arrived as "generic";
- long replies are never auto-answered (an answer that reads like an essay is
  context, not a field value), and neither are URLs;
- near-miss prior answers become *options* on the card, so the user can pick
  with one click instead of typing the same thing again.
"""

from __future__ import annotations

import re
from typing import Any

# Ordered: the first matching topic wins. Keep the phrases specific enough
# that a generic question ("which plan should I pick?") never lands here.
TOPIC_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("address", ("address", "street address", "mailing address", "city", "zip")),
    ("phone", ("phone number", "phone", "mobile number", "mobile", "telephone", "cell")),
    ("date_of_birth", ("date of birth", "birth date", "birthday", "dob")),
    ("full_name", ("full name", "legal name", "first name", "last name", "middle name")),
    ("unit", ("unit number", "apartment number", "apartment", "apt", "suite")),
    ("moving_date", ("move in", "move-in", "moving date", "are you moving", "when are you moving")),
    ("pets", ("pets", "pet", "assistance animal", "service animal", "no pets")),
    ("employment", ("employer", "employment", "occupation", "job title", "annual income")),
    ("ssn", ("social security", "ssn")),
    ("email", ("email address", "your email")),
)

# A question touching any of these is never answered from memory — the
# generic kind classifier exists, but this is the second, independent guard.
_SECRET_QUESTION_WORDS = (
    "password",
    "verification",
    "verify",
    "one-time",
    "otp",
    "mfa",
    "2fa",
    "security code",
    "sign in",
    "sign-in",
    "log in",
    "login",
    "captcha",
    "credential",
)

_URL_RE = re.compile(r"https?://", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

_MAX_REUSABLE_ANSWER_CHARS = 400
_MAX_OPTIONS = 4
_FUZZY_TOKEN_OVERLAP = 0.5


def normalize_question(question: str) -> str:
    text = str(question or "").lower()
    text = _URL_RE.sub(" ", text)
    text = re.sub(r"[^a-z0-9@._%+-]+", " ", text)
    return " ".join(text.split()).strip()


def _tokens(text: str) -> set[str]:
    return {token for token in re.split(r"[^a-z0-9]+", normalize_question(text)) if len(token) >= 3}


def _looks_secret(question: str, answer: str = "") -> bool:
    lowered = str(question or "").lower()
    if any(word in lowered for word in _SECRET_QUESTION_WORDS):
        return True
    answer_lowered = str(answer or "").lower()
    if _URL_RE.search(answer_lowered):
        return True
    # An answer that itself names a secret is a secret, whatever the question.
    return any(word in answer_lowered for word in ("password", "otp", "verification code", "token"))


def input_topic(question: str) -> str | None:
    """Stable topic key for a question, or None when it has no reusable topic."""
    lowered = " ".join(str(question or "").lower().split())
    if not lowered or _looks_secret(question):
        return None
    for topic, phrases in TOPIC_PATTERNS:
        if any(phrase in lowered for phrase in phrases):
            return topic
    return None


def _reusable(row: dict[str, Any]) -> bool:
    question = str(row.get("question") or "")
    answer = str(row.get("reply_content") or row.get("answer") or "").strip()
    if not answer:
        return False
    if len(answer) > _MAX_REUSABLE_ANSWER_CHARS:
        return False
    return not _looks_secret(question, answer)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = value.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(value.strip())
    return out


def select_reused_answer(
    rows: list[dict[str, Any]],
    question: str,
) -> dict[str, Any]:
    """Pick a prior answer to reuse, or candidate options, or neither.

    `rows` are newest-first answered input requests (see
    TaskLedger.find_answered_input_requests). Returns:
      {"answer": str | None, "topic": str | None, "options": [str], "reason": str}
    """
    if _looks_secret(question):
        return {"answer": None, "topic": None, "options": [], "reason": "secret_shaped"}

    eligible = [row for row in rows if isinstance(row, dict) and _reusable(row)]
    if not eligible:
        return {"answer": None, "topic": None, "options": [], "reason": "no_prior_answers"}

    topic = input_topic(question)
    if topic:
        matches = [
            row for row in eligible if input_topic(str(row.get("question") or "")) == topic
        ]
        if matches:
            answers = _dedupe([str(row.get("reply_content") or "").strip() for row in matches])
            return {
                "answer": answers[0],
                "topic": topic,
                "options": answers[1:_MAX_OPTIONS + 1],
                "reason": "same_topic",
            }

    normalized = normalize_question(question)
    if normalized:
        exact = [
            row
            for row in eligible
            if normalize_question(str(row.get("question") or "")) == normalized
        ]
        if exact:
            answers = _dedupe([str(row.get("reply_content") or "").strip() for row in exact])
            return {
                "answer": answers[0],
                "topic": None,
                "options": answers[1:_MAX_OPTIONS + 1],
                "reason": "exact_match",
            }

    # No confident match: offer near-miss answers as one-click options rather
    # than typing them again. The user still confirms; nothing is auto-filled.
    question_tokens = _tokens(question)
    if not question_tokens:
        return {"answer": None, "topic": None, "options": [], "reason": "not_reusable"}
    scored: list[tuple[float, str]] = []
    for row in eligible:
        row_tokens = _tokens(str(row.get("question") or ""))
        if not row_tokens:
            continue
        overlap = len(question_tokens & row_tokens) / len(question_tokens | row_tokens)
        if overlap >= _FUZZY_TOKEN_OVERLAP:
            scored.append((overlap, str(row.get("reply_content") or "").strip()))
    if not scored:
        return {"answer": None, "topic": None, "options": [], "reason": "not_reusable"}
    scored.sort(key=lambda item: item[0], reverse=True)
    options = _dedupe([answer for _score, answer in scored])[:_MAX_OPTIONS]
    return {"answer": None, "topic": topic, "options": options, "reason": "fuzzy_candidates"}
