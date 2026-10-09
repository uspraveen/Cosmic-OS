"""Gateway side of the 2026-10-08 commit-card fixes.

- /internal/browser/commit-authorize answers with no card involved, and any
  failure is "confirm" (never a silent commit);
- ask-user with authorization_checked goes straight to the card (the
  orchestrator is not asked twice), and a screenshot never reaches the card;
- Take control answers a pending card, so the run can actually park.

Route functions are called directly with a stand-in request, the way
test_browser_interrupts exercises the manager rather than HTTP.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from gateway.browser.routes import (
    AskUserRequest,
    CommitAuthorizeRequest,
    internal_ask_user,
    internal_commit_authorize,
    pause_run,
)
from gateway.browser_interrupts import BrowserInterruptManager


class _Orchestrator:
    def __init__(self, decision=None, fail=False):
        self.decision = decision
        self.fail = fail
        self.calls: list[dict] = []

    async def authorize_browser_commit(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("orchestrator down")
        return self.decision


class _Runtime:
    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        self.config = SimpleNamespace(internal_token="", local_api_token="")
        self.browser_interrupts = BrowserInterruptManager()
        self.published: list[dict] = []

    async def publish_browser_takeover_control(self, *, task_id, payload):
        self.published.append({"task_id": task_id, **payload})
        return True


def _request(runtime):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(gateway_runtime=runtime)), headers={})


def test_commit_authorize_passes_the_verdict_and_screenshot_through():
    orchestrator = _Orchestrator({"status": "fix", "reason": "Your Name holds an email", "source": "model"})
    runtime = _Runtime(orchestrator)
    body = CommitAuthorizeRequest(question="Confirm: Continue", task_id="t1", commit={"target": "Continue"},
                                  screenshot_b64="/9j/abc")
    result = asyncio.run(internal_commit_authorize(body, _request(runtime)))
    assert result["status"] == "fix" and "email" in result["reason"]
    assert orchestrator.calls[0]["screenshot_b64"] == "/9j/abc"


def test_commit_authorize_failure_is_confirm():
    runtime = _Runtime(_Orchestrator(fail=True))
    body = CommitAuthorizeRequest(question="Confirm: Continue", task_id="t1")
    assert asyncio.run(internal_commit_authorize(body, _request(runtime)))["status"] == "confirm"


def test_commit_authorize_unknown_status_is_confirm():
    runtime = _Runtime(_Orchestrator({"status": "yolo"}))
    body = CommitAuthorizeRequest(question="Confirm: Continue", task_id="t1")
    assert asyncio.run(internal_commit_authorize(body, _request(runtime)))["status"] == "confirm"


def test_checked_commit_goes_straight_to_the_card_without_its_screenshot():
    orchestrator = _Orchestrator({"status": "authorize"})
    runtime = _Runtime(orchestrator)
    body = AskUserRequest(
        request_id="bwc_1", question="Confirm: Continue", kind="commit", task_id="t1",
        commit={"target": "Continue", "screenshot_b64": "/9j/abc"}, timeout_sec=5,
        authorization_checked=True,
    )

    async def scenario():
        waiter = asyncio.ensure_future(internal_ask_user(body, _request(runtime)))
        await asyncio.sleep(0.05)
        pending = runtime.browser_interrupts.get("bwc_1")
        assert pending is not None and "screenshot_b64" not in pending.commit
        runtime.browser_interrupts.resolve("bwc_1", "approve")
        return await waiter

    result = asyncio.run(scenario())
    assert result["answer"] == "approve"
    assert orchestrator.calls == []  # not authorized twice


def test_unchecked_commit_keeps_the_old_authorize_first_behavior():
    orchestrator = _Orchestrator({"status": "authorize", "source": "model"})
    runtime = _Runtime(orchestrator)
    body = AskUserRequest(request_id="bwc_2", question="Confirm", kind="commit", task_id="t1", commit={})
    result = asyncio.run(internal_ask_user(body, _request(runtime)))
    assert result["answer"] == "approve"
    assert len(orchestrator.calls) == 1


def test_take_control_answers_a_pending_card():
    runtime = _Runtime(_Orchestrator())

    async def scenario():
        commit_wait = asyncio.ensure_future(runtime.browser_interrupts.create_and_wait(
            request_id="bwc_3", question="Confirm: Get on the list", kind="commit", task_id="t1",
            session_id=None, channel=None, timeout_sec=30,
        ))
        password_wait = asyncio.ensure_future(runtime.browser_interrupts.create_and_wait(
            request_id="bwi_4", question="Password?", kind="password", task_id="t1",
            session_id=None, channel=None, timeout_sec=30,
        ))
        other_run = asyncio.ensure_future(runtime.browser_interrupts.create_and_wait(
            request_id="bwi_5", question="Which one?", kind="generic", task_id="t2",
            session_id=None, channel=None, timeout_sec=0.3,
        ))
        await asyncio.sleep(0.05)
        response = await pause_run("t1", _request(runtime))
        return response, await commit_wait, await password_wait, await other_run

    response, commit_result, password_result, other = asyncio.run(scenario())
    assert response["released_interrupts"] == 2
    assert runtime.published == [{"task_id": "t1", "op": "pause"}]
    assert commit_result["status"] == "answered" and commit_result["answer"] == "deny"
    assert "took control" in commit_result["note"]
    # A secret card is skipped, never answered with text it would treat as the secret.
    assert password_result["status"] == "skipped" and password_result["answer"] == ""
    # Another run's question is untouched.
    assert other["status"] == "timeout"
