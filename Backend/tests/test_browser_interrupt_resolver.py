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


def _model_response(content: str, *, status: int = 200, calls: list | None = None):
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/chat/completions"):
            if calls is not None:
                calls.append(request)
            if status >= 400:
                return httpx.Response(status, text="resolver unavailable")
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
        return httpx.Response(204)

    return httpx.MockTransport(handler)


def _runtime_with_client(tmp_path, handler, **config_overrides) -> OrchestratorRuntime:
    import httpx

    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        fireworks_api_key=config_overrides.pop("fireworks_api_key", "test-key"),
        task_ledger_db_path=tmp_path / "interrupt_resolver.db",
        **config_overrides,
    )
    runtime = OrchestratorRuntime(config, client=httpx.AsyncClient(transport=handler))
    runtime.task_ledger.initialize()
    return runtime


def _seed_address_answer(runtime: OrchestratorRuntime, session_id: str = "sess_1") -> None:
    runtime.task_ledger.create_task_input_request(
        input_request_id="uir_1",
        task_id="tsk_1",
        session_id=session_id,
        channel="desktop:abc",
        agent="cosmic/orchestrator:1.0.0",
        question=ADDRESS_QUESTION,
        options=[],
    )
    runtime.task_ledger.mark_task_input_replied("uir_1", content=ADDRESS_ANSWER)


def test_runtime_resolver_answers_from_session_memory(tmp_path) -> None:
    calls: list = []
    runtime = _runtime_with_client(
        tmp_path,
        _model_response('{"decision":"escalate"}', calls=calls),
    )
    _seed_address_answer(runtime)

    answered = asyncio.run(
        runtime.resolve_browser_interrupt(
            session_id="sess_1",
            question="What is your current address for the form?",
        )
    )
    assert answered["status"] == "answered"
    assert answered["answer"] == ADDRESS_ANSWER
    assert answered["source"] == "session_memory"
    # Deterministic memory wins: the model is never called for a known answer.
    assert calls == []

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


def test_runtime_resolver_model_can_answer_when_memory_cannot_match(tmp_path) -> None:
    calls: list = []
    runtime = _runtime_with_client(
        tmp_path,
        _model_response(
            '{"decision":"answer","answer":"No pets declaration","confidence":0.9,"reason":"resident has no pets"}',
            calls=calls,
        ),
    )
    runtime.task_ledger.create_task_input_request(
        input_request_id="uir_pets",
        task_id="tsk_pets",
        session_id="sess_1",
        channel="desktop:abc",
        agent="cosmic/orchestrator:1.0.0",
        question="Does the resident have pets or assistance animals?",
        options=[],
    )
    runtime.task_ledger.mark_task_input_replied("uir_pets", content="No pets / no assistance animals")

    decision = asyncio.run(
        runtime.resolve_browser_interrupt(
            session_id="sess_1",
            question="Should I select the declaration that applies to this lease?",
            page_url="https://thewatersatchenal.petscreening.com/profile",
        )
    )
    assert decision["status"] == "answered"
    assert decision["source"] == "model"
    assert decision["answer"] == "No pets declaration"
    assert len(calls) == 1
    # The decider runs on the configured Fireworks brain, never a hardcoded provider.
    payload = calls[0].content.decode("utf-8")
    assert "accounts/fireworks/models/glm-5p3" in payload
    assert "No pets / no assistance animals" in payload


def test_runtime_resolver_escalates_on_low_model_confidence(tmp_path) -> None:
    runtime = _runtime_with_client(
        tmp_path,
        _model_response(
            '{"decision":"answer","answer":"Maybe the first option","confidence":0.4,"reason":"unclear"}'
        ),
    )
    runtime.task_ledger.create_task_input_request(
        input_request_id="uir_pets",
        task_id="tsk_pets",
        session_id="sess_1",
        channel="desktop:abc",
        agent="cosmic/orchestrator:1.0.0",
        question="Does the resident have pets or assistance animals?",
        options=[],
    )
    runtime.task_ledger.mark_task_input_replied("uir_pets", content="No pets / no assistance animals")

    decision = asyncio.run(
        runtime.resolve_browser_interrupt(
            session_id="sess_1",
            question="Should I select the declaration that applies to this lease?",
        )
    )
    assert decision["status"] == "escalate"


def test_runtime_resolver_never_calls_a_non_fireworks_provider(tmp_path) -> None:
    calls: list = []
    runtime = _runtime_with_client(
        tmp_path,
        _model_response("{}", calls=calls),
        orchestrator_default_provider="anthropic",
        fireworks_api_key="",
    )
    runtime.task_ledger.create_task_input_request(
        input_request_id="uir_pets",
        task_id="tsk_pets",
        session_id="sess_1",
        channel="desktop:abc",
        agent="cosmic/orchestrator:1.0.0",
        question="Does the resident have pets or assistance animals?",
        options=[],
    )
    runtime.task_ledger.mark_task_input_replied("uir_pets", content="No pets / no assistance animals")

    decision = asyncio.run(
        runtime.resolve_browser_interrupt(
            session_id="sess_1",
            question="Should I select the declaration that applies to this lease?",
        )
    )
    assert decision["status"] == "escalate"
    assert calls == []


def test_runtime_resolver_model_failure_escalates_cleanly(tmp_path) -> None:
    runtime = _runtime_with_client(tmp_path, _model_response("", status=503))
    runtime.task_ledger.create_task_input_request(
        input_request_id="uir_pets",
        task_id="tsk_pets",
        session_id="sess_1",
        channel="desktop:abc",
        agent="cosmic/orchestrator:1.0.0",
        question="Does the resident have pets or assistance animals?",
        options=[],
    )
    runtime.task_ledger.mark_task_input_replied("uir_pets", content="No pets / no assistance animals")

    decision = asyncio.run(
        runtime.resolve_browser_interrupt(
            session_id="sess_1",
            question="Should I select the declaration that applies to this lease?",
        )
    )
    assert decision["status"] == "escalate"
