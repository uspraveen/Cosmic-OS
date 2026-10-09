"""Commit authorization for browser runs.

Default confirm; authorize on its own only when the user's own instruction
explicitly asked for the action, or a task-scoped "Approve all" grant exists.
Any doubt, missing context, or model failure must resolve to confirm — never to
a silent commit.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared import TaskEnvelope, utcnow
from shared.commit_policy import commit_action_class, commit_summary
from orchestrator.config import OrchestratorConfig
from orchestrator.runtime import OrchestratorRuntime


ADDRESS_REQUEST = {
    "target": "Submit application",
    "url": "https://jobs.example.com/apply/42",
    "irreversible": False,
    "control": {"name": "Submit application", "matched": ["submit", "apply"]},
    "fields": [
        {"label": "Full name", "value": "Praveen Raj"},
        {"label": "Resume", "value": "resume.pdf"},
    ],
}


def _model(content: str, *, status: int = 200, calls: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/chat/completions"):
            if calls is not None:
                calls.append(request)
            if status >= 400:
                return httpx.Response(status, text="model unavailable")
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
        return httpx.Response(204)

    return httpx.MockTransport(handler)


def _runtime(tmp_path, handler, **overrides) -> OrchestratorRuntime:
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        fireworks_api_key=overrides.pop("fireworks_api_key", "test-key"),
        task_ledger_db_path=tmp_path / "commit_authorization.db",
        **overrides,
    )
    runtime = OrchestratorRuntime(config, client=httpx.AsyncClient(transport=handler))
    runtime.task_ledger.initialize()
    return runtime


def _seed_tasks(
    runtime: OrchestratorRuntime,
    *,
    query: str = "Please apply to these 50 jobs for me tonight.",
    goal: str = "Apply to each listing on the board, one by one.",
    task_id: str = "tsk_browser_1",
    parent_id: str = "tsk_parent_1",
    source: str = "user",
) -> None:
    parent = TaskEnvelope(
        task_id=parent_id,
        task_list_id="sess_1",
        session_id="sess_1",
        sender="cosmic/gateway:1.0.0",
        recipient="cosmic/orchestrator:1.0.0",
        intent="orchestrator.process",
        input={"query": query, "request_id": "req_1"},
        idempotency_key="idem_parent",
        priority="high",
        signature="",
        created_at=utcnow(),
        source=source,
        source_id="desktop",
        channel="desktop:abc",
    )
    runtime.task_ledger.create_task(parent)
    browser = TaskEnvelope(
        task_id=task_id,
        task_list_id="sess_1",
        parent_task_id=parent_id,
        session_id="sess_1",
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/browser-agent:1.0.0",
        intent="browser.run",
        input={"goal": goal},
        idempotency_key="idem_browser",
        priority="high",
        signature="",
        created_at=utcnow(),
        source="user",
        source_id="desktop",
        channel="desktop:abc",
    )
    runtime.task_ledger.create_task(browser)


class _Commit(dict):
    pass


def test_action_class_uses_the_matched_verb_and_defaults_to_submit():
    assert commit_action_class(_Commit(ADDRESS_REQUEST)) == "submit"
    assert commit_action_class(_Commit({"control": {"matched": ["apply"]}})) == "apply"
    assert commit_action_class(None) == "submit"


def test_commit_summary_names_target_site_and_irreversibility():
    summary = commit_summary(_Commit({**ADDRESS_REQUEST, "irreversible": True}))
    assert "Submit application" in summary
    assert "https://jobs.example.com/apply/42" in summary
    assert "irreversible" in summary


def test_no_context_means_confirm_without_calling_the_model(tmp_path):
    calls: list = []
    runtime = _runtime(tmp_path, _model("", calls=calls))
    decision = asyncio.run(
        runtime.authorize_browser_commit(
            session_id="sess_1",
            task_id=None,
            question="Confirm: Submit application",
            commit=ADDRESS_REQUEST,
        )
    )
    assert decision["status"] == "confirm"
    assert decision["source"] == "no_context"
    assert calls == []


def test_explicit_user_instruction_can_authorize(tmp_path):
    calls: list = []
    runtime = _runtime(
        tmp_path,
        _model('{"decision":"authorize","reason":"user asked to apply"}', calls=calls),
    )
    _seed_tasks(runtime)
    decision = asyncio.run(
        runtime.authorize_browser_commit(
            session_id="sess_1",
            task_id="tsk_browser_1",
            question="Confirm: Submit application",
            commit=ADDRESS_REQUEST,
        )
    )
    assert decision["status"] == "authorize"
    assert decision["source"] == "model"
    assert decision["action_class"] == "submit"
    assert len(calls) == 1
    prompt = calls[0].content.decode("utf-8")
    assert "apply to these 50 jobs" in prompt
    assert "resume.pdf" in prompt


def test_model_confirm_keeps_the_human_card(tmp_path):
    runtime = _runtime(tmp_path, _model('{"decision":"confirm","reason":"not explicit"}'))
    _seed_tasks(runtime, query="Can you look at my browser profile?")
    decision = asyncio.run(
        runtime.authorize_browser_commit(
            session_id="sess_1",
            task_id="tsk_browser_1",
            question="Confirm: Submit application",
            commit=ADDRESS_REQUEST,
        )
    )
    assert decision["status"] == "confirm"
    assert decision["source"] == "model"


def test_model_failure_never_authorizes(tmp_path):
    runtime = _runtime(tmp_path, _model("", status=503))
    _seed_tasks(runtime)
    decision = asyncio.run(
        runtime.authorize_browser_commit(
            session_id="sess_1",
            task_id="tsk_browser_1",
            question="Confirm: Submit application",
            commit=ADDRESS_REQUEST,
        )
    )
    assert decision["status"] == "confirm"
    assert decision["source"] == "model_unavailable"


def test_grant_authorizes_without_a_model_call(tmp_path):
    calls: list = []
    runtime = _runtime(tmp_path, _model("", calls=calls))
    _seed_tasks(runtime)
    granted = runtime.record_browser_commit_grant(task_id="tsk_browser_1", action_class="submit")
    assert granted["ok"] is True
    decision = asyncio.run(
        runtime.authorize_browser_commit(
            session_id="sess_1",
            task_id="tsk_browser_1",
            question="Confirm: Submit application",
            commit=ADDRESS_REQUEST,
        )
    )
    assert decision["status"] == "authorize"
    assert decision["source"] == "grant"
    assert calls == []


def test_non_fireworks_provider_never_calls_a_model(tmp_path):
    calls: list = []
    runtime = _runtime(
        tmp_path,
        _model("", calls=calls),
        orchestrator_default_provider="anthropic",
        fireworks_api_key="",
    )
    _seed_tasks(runtime)
    decision = asyncio.run(
        runtime.authorize_browser_commit(
            session_id="sess_1",
            task_id="tsk_browser_1",
            question="Confirm: Submit application",
            commit=ADDRESS_REQUEST,
        )
    )
    assert decision["status"] == "confirm"
    assert calls == []


def test_commit_miss_labels_are_appended_for_harvesting(tmp_path):
    from gateway.commit_miss import commit_miss_path, record_commit_miss

    path = commit_miss_path(tmp_path / "gateway" / "sessions.db")
    assert path.name == "browser_commit_misses.jsonl"

    assert record_commit_miss(path, {"label": "File return", "task_id": "t1"}) is True
    assert record_commit_miss(path, {"label": "Transmit"}) is True

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["label"] == "File return"
    assert first["task_id"] == "t1"
    assert "at" in first


# --- correctness and the visual check (2026-10-08 Partiful run) -------------

EMAIL_IN_NAME = {
    "target": "Continue",
    "url": "https://partiful.com/e/x?rsvp=true",
    "irreversible": False,
    "control": {"name": "Continue", "matched": [], "is_submit_control": True},
    "fields": [{"label": "Your Name", "value": "uspraveenraj@gmail.com"}],
    "empty_field_count": 1,
}


def _model_and_decider(content: str, *, ready: float | None, model_calls: list, decider_calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/chat/completions"):
            model_calls.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
        if url.endswith("/v1/decisions"):
            decider_calls.append(json.loads(request.content))
            if ready is None:
                return httpx.Response(503, text="busy")
            return httpx.Response(200, json={"answers": {"ready": {"type": "noul", "noul": ready}}})
        return httpx.Response(204)

    return httpx.MockTransport(handler)


def test_prompt_asks_for_correctness_and_shows_the_empty_count(tmp_path):
    model_calls: list = []
    runtime = _runtime(tmp_path, _model_and_decider('{"decision":"confirm","reason":"x"}', ready=None,
                                                    model_calls=model_calls, decider_calls=[]))
    _seed_tasks(runtime, query="Maybe you apply that for me")
    asyncio.run(runtime.authorize_browser_commit(
        session_id="sess_1", task_id="tsk_browser_1", question="Confirm: Continue", commit=EMAIL_IN_NAME,
    ))
    prompt = model_calls[0].content.decode("utf-8")
    assert "Correctness" in prompt and "EMPTY FIELDS IN THIS FORM: 1" in prompt
    assert "Your Name: uspraveenraj@gmail.com" in prompt


def test_fix_verdict_goes_back_with_its_reason(tmp_path):
    runtime = _runtime(tmp_path, _model_and_decider(
        '{"decision":"fix","reason":"Your Name holds an email address; put the name there."}',
        ready=0.9, model_calls=[], decider_calls=[]))
    _seed_tasks(runtime, query="Maybe you apply that for me")
    decision = asyncio.run(runtime.authorize_browser_commit(
        session_id="sess_1", task_id="tsk_browser_1", question="Confirm: Continue", commit=EMAIL_IN_NAME,
    ))
    assert decision["status"] == "fix"
    assert "email address" in decision["reason"]


def test_a_fix_with_nothing_to_fix_is_a_confirm(tmp_path):
    runtime = _runtime(tmp_path, _model_and_decider('{"decision":"fix","reason":""}', ready=0.9,
                                                    model_calls=[], decider_calls=[]))
    _seed_tasks(runtime)
    decision = asyncio.run(runtime.authorize_browser_commit(
        session_id="sess_1", task_id="tsk_browser_1", question="Confirm", commit=ADDRESS_REQUEST,
    ))
    assert decision["status"] == "confirm"


def test_visual_check_can_hold_a_model_authorization(tmp_path):
    decider_calls: list = []
    runtime = _runtime(
        tmp_path,
        _model_and_decider('{"decision":"authorize","reason":"user asked"}', ready=0.1,
                           model_calls=[], decider_calls=decider_calls),
        perplexity_api_key="pplx-test",
    )
    _seed_tasks(runtime)
    decision = asyncio.run(runtime.authorize_browser_commit(
        session_id="sess_1", task_id="tsk_browser_1", question="Confirm: Submit application",
        commit=ADDRESS_REQUEST, screenshot_b64="/9j/" + "A" * 200,
    ))
    assert decision["status"] == "confirm"
    assert decision["source"] == "visual_check"
    state = decider_calls[0]["state"]
    assert state[-1]["image_url"]["url"].startswith("data:image/jpeg;base64,/9j/")


def test_visual_check_passing_keeps_the_authorization(tmp_path):
    runtime = _runtime(
        tmp_path,
        _model_and_decider('{"decision":"authorize","reason":"user asked"}', ready=0.93,
                           model_calls=[], decider_calls=[]),
        perplexity_api_key="pplx-test",
    )
    _seed_tasks(runtime)
    decision = asyncio.run(runtime.authorize_browser_commit(
        session_id="sess_1", task_id="tsk_browser_1", question="Confirm", commit=ADDRESS_REQUEST,
        screenshot_b64="/9j/" + "A" * 200,
    ))
    assert decision["status"] == "authorize"


def test_visual_check_unavailable_never_blocks_on_its_own(tmp_path):
    decider_calls: list = []
    runtime = _runtime(
        tmp_path,
        _model_and_decider('{"decision":"authorize","reason":"user asked"}', ready=None,
                           model_calls=[], decider_calls=decider_calls),
        perplexity_api_key="pplx-test",
    )
    _seed_tasks(runtime)
    decision = asyncio.run(runtime.authorize_browser_commit(
        session_id="sess_1", task_id="tsk_browser_1", question="Confirm", commit=ADDRESS_REQUEST,
        screenshot_b64="/9j/" + "A" * 200,
    ))
    assert decision["status"] == "authorize"
    # A busy decider (503) is retried, then the text decision stands.
    assert len(decider_calls) == 3


def test_no_screenshot_skips_the_visual_check(tmp_path):
    decider_calls: list = []
    runtime = _runtime(
        tmp_path,
        _model_and_decider('{"decision":"authorize","reason":"user asked"}', ready=0.0,
                           model_calls=[], decider_calls=decider_calls),
        perplexity_api_key="pplx-test",
    )
    _seed_tasks(runtime)
    decision = asyncio.run(runtime.authorize_browser_commit(
        session_id="sess_1", task_id="tsk_browser_1", question="Confirm", commit=ADDRESS_REQUEST,
    ))
    assert decision["status"] == "authorize"
    assert decider_calls == []


# --- busy Perplexity: retries, then OpenAI's Decisions API (2026-10-09) -----

PPLX_URL = "https://api.perplexity.ai/v1/decisions"
OPENAI_URL = "https://api.openai.com/v1/decisions"


def _busy_decider(*, pplx_statuses: list[int], pplx_ready: float = 0.9, openai_ready: float | None = 0.1,
                  verdict: str = '{"decision":"authorize","reason":"user asked"}', calls: list):
    """pplx_statuses: status per successive Perplexity call (the last repeats)."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": verdict}}]})
        if url == PPLX_URL:
            calls.append(("pplx", json.loads(request.content)))
            n = sum(1 for kind, _ in calls if kind == "pplx")
            status = pplx_statuses[min(n - 1, len(pplx_statuses) - 1)]
            if status != 200:
                return httpx.Response(status, headers={"retry-after": "1"}, text="busy")
            return httpx.Response(200, json={"answers": {"ready": {"type": "noul", "noul": pplx_ready}}})
        if url == OPENAI_URL:
            calls.append(("openai", json.loads(request.content)))
            if openai_ready is None:
                return httpx.Response(500, text="down")
            return httpx.Response(200, json={"model": "gpt-6-luna", "answers": [
                {"type": "predicate", "name": "ready", "probability": openai_ready}]})
        return httpx.Response(204)

    return httpx.MockTransport(handler)


def _authorize(runtime):
    _seed_tasks(runtime)
    return asyncio.run(runtime.authorize_browser_commit(
        session_id="sess_1", task_id="tsk_browser_1", question="Confirm: Submit application",
        commit=ADDRESS_REQUEST, screenshot_b64="/9j/" + "A" * 200,
    ))


def _no_sleep(monkeypatch) -> list:
    waits: list = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    import orchestrator.decider as decider_module
    monkeypatch.setattr(decider_module.asyncio, "sleep", fake_sleep)
    return waits


def test_a_rate_limited_visual_check_is_answered_by_openai(tmp_path, monkeypatch):
    waits = _no_sleep(monkeypatch)
    calls: list = []
    runtime = _runtime(tmp_path, _busy_decider(pplx_statuses=[429], openai_ready=0.1, calls=calls),
                       perplexity_api_key="pplx-test", decider_fallback_api_key="sk-test")
    decision = _authorize(runtime)
    assert decision["status"] == "confirm" and decision["source"] == "visual_check"
    assert [kind for kind, _ in calls] == ["pplx", "pplx", "pplx", "openai"]
    assert len(waits) == 2 and all(w <= 1.1 for w in waits)
    request = calls[-1][1]
    assert request["model"] == "gpt-6-luna"
    assert request["questions"] == [{"type": "predicate", "name": "ready", "instructions": request["questions"][0]["instructions"]}]
    parts = request["input"][0]["content"]
    assert parts[-1]["type"] == "input_image" and parts[-1]["image_url"].startswith("data:image/jpeg;base64,/9j/")
    assert parts[0]["text"].startswith("The user asked:")


def test_one_busy_answer_is_retried_on_perplexity(tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    calls: list = []
    runtime = _runtime(tmp_path, _busy_decider(pplx_statuses=[503, 200], pplx_ready=0.9, calls=calls),
                       perplexity_api_key="pplx-test", decider_fallback_api_key="sk-test")
    decision = _authorize(runtime)
    assert decision["status"] == "authorize"
    assert [kind for kind, _ in calls] == ["pplx", "pplx"]


def test_a_perplexity_timeout_skips_its_retries(tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"decision":"authorize","reason":"ok"}'}}]})
        if url not in (PPLX_URL, OPENAI_URL):
            return httpx.Response(204)
        calls.append(url)
        if url == PPLX_URL:
            raise httpx.ReadTimeout("slow")
        return httpx.Response(200, json={"answers": [{"type": "predicate", "name": "ready", "probability": 0.95}]})

    runtime = _runtime(tmp_path, httpx.MockTransport(handler),
                       perplexity_api_key="pplx-test", decider_fallback_api_key="sk-test")
    assert _authorize(runtime)["status"] == "authorize"
    assert calls == [PPLX_URL, OPENAI_URL]


def test_both_deciders_down_leaves_the_text_decision_standing(tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    calls: list = []
    runtime = _runtime(tmp_path, _busy_decider(pplx_statuses=[503], openai_ready=None, calls=calls),
                       perplexity_api_key="pplx-test", decider_fallback_api_key="sk-test")
    assert _authorize(runtime)["status"] == "authorize"
    assert [kind for kind, _ in calls] == ["pplx", "pplx", "pplx", "openai", "openai"]


def test_without_a_fallback_key_a_busy_perplexity_is_just_skipped(tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    calls: list = []
    runtime = _runtime(tmp_path, _busy_decider(pplx_statuses=[429], calls=calls), perplexity_api_key="pplx-test")
    assert _authorize(runtime)["status"] == "authorize"
    assert [kind for kind, _ in calls] == ["pplx", "pplx", "pplx"]


def test_fallback_key_resolution(monkeypatch):
    from orchestrator.config import _decider_fallback_key
    for name in ("COSMIC_DECIDER_FALLBACK", "COSMIC_DECIDER_FALLBACK_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    # The visual-enhancement key doubles as the fallback only when it is an OpenAI key.
    assert _decider_fallback_key("sk-visual", "https://api.openai.com/v1") == "sk-visual"
    assert _decider_fallback_key("fw-key", "https://api.fireworks.ai/inference/v1") == ""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    assert _decider_fallback_key("sk-visual", "https://api.openai.com/v1") == "sk-openai"
    monkeypatch.setenv("COSMIC_DECIDER_FALLBACK", "off")
    assert _decider_fallback_key("sk-visual", "https://api.openai.com/v1") == ""


def test_image_data_url_names_the_real_format():
    from orchestrator.decider import image_data_url
    png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    assert image_data_url(png).startswith("data:image/png;base64,")
    assert image_data_url("data:image/jpeg;base64," + png).startswith("data:image/png;base64,")
    assert image_data_url("/9j/" + "A" * 200).startswith("data:image/jpeg;base64,")
    assert image_data_url("short") is None and image_data_url(None) is None
