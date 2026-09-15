"""Browser-interrupt memory: the orchestrator answers what the user already told it.

Pins the conservative reuse rules: same-topic questions get the prior answer
without any model call, secrets are never reused even when they arrive as
"generic", and near-miss answers come back as one-click options instead of
being typed again.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator.config import OrchestratorConfig
from orchestrator.interrupt_memory import (
    input_topic,
    select_reused_answer,
)
from orchestrator.runtime import OrchestratorRuntime


ADDRESS_QUESTION = (
    "I am completing your PetScreening profile. The form now requires your "
    "current address (Address Line 1, City, State, and Zip Code)."
)
ADDRESS_ANSWER = "13500 Chenal Pkwy, Apt 2309, Little Rock, AR 72211"


def _row(question: str, answer: str, **extra) -> dict:
    return {
        "input_request_id": "uir_test",
        "task_id": "tsk_test",
        "question": question,
        "reply_content": answer,
        "options": [],
        **extra,
    }


def test_topic_detection_covers_the_field_questions_that_actually_repeat():
    assert input_topic(ADDRESS_QUESTION) == "address"
    assert input_topic("What is your phone number?") == "phone"
    assert input_topic("Please confirm your date of birth.") == "date_of_birth"
    assert input_topic("Which plan should I pick?") is None


def test_secret_shaped_questions_have_no_topic():
    assert input_topic("Please enter your password to continue.") is None
    assert input_topic("What is the verification code sent to your phone?") is None
    assert input_topic("Click the login link in your inbox.") is None


def test_same_topic_question_reuses_the_prior_answer():
    decision = select_reused_answer([_row(ADDRESS_QUESTION, ADDRESS_ANSWER)], ADDRESS_QUESTION)
    assert decision["answer"] == ADDRESS_ANSWER
    assert decision["topic"] == "address"
    assert decision["reason"] == "same_topic"


def test_newest_answer_wins_and_other_answers_become_options():
    older = _row(ADDRESS_QUESTION, "100 Chenal Woods Dr, Little Rock, AR 72223")
    newer = _row(ADDRESS_QUESTION, ADDRESS_ANSWER)
    decision = select_reused_answer([newer, older], "What is your current address?")
    assert decision["answer"] == ADDRESS_ANSWER
    assert decision["options"] == ["100 Chenal Woods Dr, Little Rock, AR 72223"]


def test_secret_answers_are_never_reused_even_from_a_generic_ask():
    row = _row(
        "I am at the login screen for PetScreening. Please log in using that email.",
        "Just login using uspraveen.... gmail and pass: All@roundthew0rld",
    )
    decision = select_reused_answer([row], "Anything else you need from me?")
    assert decision["answer"] is None
    assert decision["options"] == []


def test_long_and_url_answers_are_not_reused():
    long_answer = "x" * 500
    decision = select_reused_answer([_row(ADDRESS_QUESTION, long_answer)], ADDRESS_QUESTION)
    assert decision["answer"] is None
    url_row = _row("Please open the address page", "https://example.com/form")
    assert select_reused_answer([url_row], "Please open the address page")["answer"] is None


def test_fuzzy_question_returns_options_but_never_auto_answers():
    row = _row(
        "Which of the two profile declarations should I complete for the lease?",
        "No pets declaration",
    )
    decision = select_reused_answer(
        [row],
        "Which profile declaration should I complete for this lease?",
    )
    assert decision["answer"] is None
    assert "No pets declaration" in decision["options"]
    assert decision["reason"] == "fuzzy_candidates"


def test_exact_normalized_match_reuses_without_a_topic():
    question = "Which declaration should I complete for the lease?"
    row = _row(question, "No pets declaration")
    decision = select_reused_answer([row], question)
    assert decision["answer"] == "No pets declaration"
    assert decision["reason"] == "exact_match"


def test_runtime_resolver_answers_from_session_memory(tmp_path) -> None:
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        task_ledger_db_path=tmp_path / "interrupt_resolver.db",
    )
    runtime = OrchestratorRuntime(config)
    runtime.task_ledger.initialize()
    runtime.task_ledger.create_task_input_request(
        input_request_id="uir_1",
        task_id="tsk_1",
        session_id="sess_1",
        channel="desktop:abc",
        agent="cosmic/orchestrator:1.0.0",
        question=ADDRESS_QUESTION,
        options=[],
    )
    runtime.task_ledger.mark_task_input_replied("uir_1", content=ADDRESS_ANSWER)

    answered = asyncio.run(
        runtime.resolve_browser_interrupt(
            session_id="sess_1",
            question="What is your current address for the form?",
        )
    )
    assert answered["status"] == "answered"
    assert answered["answer"] == ADDRESS_ANSWER
    assert answered["source"] == "session_memory"

    escalated = asyncio.run(
        runtime.resolve_browser_interrupt(
            session_id="sess_1",
            question="Which pet profile option should I select?",
        )
    )
    assert escalated["status"] == "escalate"

    secret = asyncio.run(
        runtime.resolve_browser_interrupt(
            session_id="sess_1",
            question="Please enter your password to continue.",
        )
    )
    assert secret["status"] == "escalate"

    wrong_kind = asyncio.run(
        runtime.resolve_browser_interrupt(
            session_id="sess_1",
            question="What is your current address?",
            kind="password",
        )
    )
    assert wrong_kind["status"] == "escalate"
    assert wrong_kind["reason"] == "non_context_kind"
