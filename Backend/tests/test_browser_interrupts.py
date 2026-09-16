"""Tests for the browser agent's AskUser interrupt bridge (in-memory manager).

Covers `gateway/browser_interrupts.py` directly — the actual wait/resolve/skip
logic that `/internal/browser/ask-user` and `/channels/browser/interrupts/*`
(gateway/browser/routes.py) are thin wrappers around, matching how
test_vault_browser_request.py exercises GatewayRuntime methods rather than
the FastAPI routes themselves.
"""

from __future__ import annotations

import asyncio

from gateway.browser_interrupts import BrowserInterruptManager


def test_resolve_wakes_a_pending_wait():
    manager = BrowserInterruptManager()

    async def scenario():
        wait_task = asyncio.create_task(
            manager.create_and_wait(
                request_id="bwi_1",
                question="What is the OTP code?",
                kind="verification_code",
                task_id="t1",
                session_id="s1",
                channel="desktop:abc",
                timeout_sec=30,
            )
        )
        await asyncio.sleep(0.05)  # let create_and_wait register the pending entry
        interrupt = manager.resolve("bwi_1", "482913")
        assert interrupt is not None
        assert interrupt.status == "answered"
        result = await asyncio.wait_for(wait_task, timeout=2)
        return result

    result = asyncio.run(scenario())
    assert result == {"request_id": "bwi_1", "status": "answered", "answer": "482913", "field_edits": [], "note": ""}
    # The pending entry is cleaned up once resolved.
    assert manager.get("bwi_1") is None


def test_skip_wakes_a_pending_wait():
    manager = BrowserInterruptManager()

    async def scenario():
        wait_task = asyncio.create_task(
            manager.create_and_wait(
                request_id="bwi_2",
                question="Please solve the CAPTCHA and tell me when done.",
                kind="generic",
                task_id="t1",
                session_id="s1",
                channel=None,
                timeout_sec=30,
            )
        )
        await asyncio.sleep(0.05)
        interrupt = manager.skip("bwi_2")
        assert interrupt is not None
        assert interrupt.status == "skipped"
        return await asyncio.wait_for(wait_task, timeout=2)

    result = asyncio.run(scenario())
    assert result == {"request_id": "bwi_2", "status": "skipped", "answer": "", "field_edits": [], "note": ""}


def test_wait_times_out_when_nobody_answers():
    manager = BrowserInterruptManager()
    result = asyncio.run(
        manager.create_and_wait(
            request_id="bwi_3",
            question="Enter your password to continue.",
            kind="password",
            task_id="t1",
            session_id="s1",
            channel=None,
            timeout_sec=1,
        )
    )
    assert result == {"request_id": "bwi_3", "status": "timeout", "answer": "", "field_edits": [], "note": ""}


def test_resolve_unknown_or_already_resolved_returns_none():
    manager = BrowserInterruptManager()
    assert manager.resolve("does-not-exist", "answer") is None
    assert manager.skip("does-not-exist") is None

    async def scenario():
        wait_task = asyncio.create_task(
            manager.create_and_wait(
                request_id="bwi_4",
                question="Confirm this sign-in on your phone, then tell me.",
                kind="generic",
                task_id="t1",
                session_id="s1",
                channel=None,
                timeout_sec=30,
            )
        )
        await asyncio.sleep(0.05)
        first = manager.resolve("bwi_4", "done")
        assert first is not None
        # A second resolve/skip after it's already answered is a no-op.
        assert manager.resolve("bwi_4", "again") is None
        assert manager.skip("bwi_4") is None
        await asyncio.wait_for(wait_task, timeout=2)

    asyncio.run(scenario())


def test_wait_carries_the_page_url_for_vault_routing():
    manager = BrowserInterruptManager()

    async def scenario():
        wait_task = asyncio.create_task(
            manager.create_and_wait(
                request_id="bwi_5",
                question="Enter your password to continue.",
                kind="password",
                task_id="t1",
                session_id="s1",
                channel=None,
                page_url="https://thewatersatchenal.petscreening.com/users/sign_in",
                timeout_sec=30,
            )
        )
        await asyncio.sleep(0.05)
        pending = manager.get("bwi_5")
        assert pending is not None
        assert pending.page_url == "https://thewatersatchenal.petscreening.com/users/sign_in"
        manager.skip("bwi_5")
        await asyncio.wait_for(wait_task, timeout=2)

    asyncio.run(scenario())


def test_wait_carries_the_commit_payload_for_the_card():
    manager = BrowserInterruptManager()

    async def scenario():
        wait_task = asyncio.create_task(
            manager.create_and_wait(
                request_id="bwc_1",
                question="Confirm: Submit application",
                kind="commit",
                task_id="t1",
                session_id="s1",
                channel=None,
                page_url="https://jobs.example.com/apply/42",
                commit={"target": "Submit application", "control": {"matched": ["submit", "apply"]}},
                timeout_sec=30,
            )
        )
        await asyncio.sleep(0.05)
        pending = manager.get("bwc_1")
        assert pending is not None
        assert pending.kind == "commit"
        assert pending.commit["target"] == "Submit application"
        manager.skip("bwc_1")
        await asyncio.wait_for(wait_task, timeout=2)

    asyncio.run(scenario())


def test_card_edits_ride_the_approval_back_to_the_browser_agent():
    manager = BrowserInterruptManager()

    async def scenario():
        wait_task = asyncio.create_task(
            manager.create_and_wait(
                request_id="bwc_2",
                question="Confirm: Submit application",
                kind="commit",
                task_id="t1",
                session_id="s1",
                channel=None,
                page_url="https://jobs.example.com/apply/42",
                commit={"target": "Submit application"},
                timeout_sec=30,
            )
        )
        await asyncio.sleep(0.05)
        interrupt = manager.resolve(
            "bwc_2",
            "approve",
            field_edits=[{"label": "Full name", "value": "Praveen Raj U S"}, "junk", {"label": "", "value": "x"}],
        )
        assert interrupt is not None
        assert interrupt.field_edits == [{"label": "Full name", "value": "Praveen Raj U S"}]
        return await asyncio.wait_for(wait_task, timeout=2)

    result = asyncio.run(scenario())
    assert result["status"] == "answered"
    assert result["answer"] == "approve"
    assert result["field_edits"] == [{"label": "Full name", "value": "Praveen Raj U S"}]


def test_note_rides_whichever_action_the_user_takes():
    manager = BrowserInterruptManager()

    async def scenario():
        wait_task = asyncio.create_task(
            manager.create_and_wait(
                request_id="bwc_3",
                question="Confirm: Submit application",
                kind="commit",
                task_id="t1",
                session_id="s1",
                channel=None,
                commit={"target": "Submit application"},
                timeout_sec=30,
            )
        )
        await asyncio.sleep(0.05)
        interrupt = manager.resolve(
            "bwc_3",
            "deny",
            note="  no — use the other resume and skip the cover letter  ",
        )
        assert interrupt is not None
        assert interrupt.note == "no — use the other resume and skip the cover letter"
        return await asyncio.wait_for(wait_task, timeout=2)

    result = asyncio.run(scenario())
    assert result["answer"] == "deny"
    assert result["note"] == "no — use the other resume and skip the cover letter"


def test_blank_request_id_is_rejected_without_hanging():
    manager = BrowserInterruptManager()
    result = asyncio.run(
        manager.create_and_wait(
            request_id="  ",
            question="Anything?",
            kind="generic",
            task_id=None,
            session_id=None,
            channel=None,
            timeout_sec=30,
        )
    )
    assert result == {"request_id": "", "status": "error", "answer": ""}


def test_respond_route_sanitizes_card_edits():
    from gateway.browser.routes import _sanitize_field_edits

    edits = _sanitize_field_edits(
        [
            {"label": "Full name", "value": "  Praveen Raj U S  "},
            {"label": "Password", "value": "********"},       # masked: never a real value
            {"label": "Accept terms", "value": "checked"},     # checkbox state, not text
            {"label": "", "value": "orphan"},                  # nothing to match against
            {"label": "Ghost", "value": "   "},                # empty after trim
            "junk",
            {"label": "Plan", "value": "Pro"},
        ]
    )
    assert edits == [
        {"label": "Full name", "value": "Praveen Raj U S"},
        {"label": "Plan", "value": "Pro"},
    ]
    assert _sanitize_field_edits(None) == []
