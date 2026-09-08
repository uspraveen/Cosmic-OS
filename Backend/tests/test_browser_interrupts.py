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
    assert result == {"request_id": "bwi_1", "status": "answered", "answer": "482913"}
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
    assert result == {"request_id": "bwi_2", "status": "skipped", "answer": ""}


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
    assert result == {"request_id": "bwi_3", "status": "timeout", "answer": ""}


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
