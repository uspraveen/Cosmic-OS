from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.runtime import ActiveRequest
from gateway.vault.store import VaultStore
from gateway.vault.routes import LookupRequest, internal_lookup
from test_gateway_desktop_ws import build_runtime


def request(runtime):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(gateway_runtime=runtime)),
                           headers={"X-Internal-Token": "internal-token"})


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approved", "rejected", "timeout"])
async def test_approval_decision_returns_to_original_tool_and_preserves_card(tmp_path, decision):
    runtime = build_runtime(tmp_path)
    runtime.vault_store = VaultStore(tmp_path / "vault.db")
    await runtime.start()
    try:
        entry = runtime.vault_store.add_entry({"title": "Example", "site_url": "https://example.com",
                                             "username": "tester", "password_encrypted": "unused"})
        state = ActiveRequest(request_id="req_original", session_id=runtime._current_session_id(), task_id="tsk_original",
                              channel="desktop:desk_inline", route="orchestrator", partial_content="Before.\n\n")
        runtime.active_requests[state.request_id] = state
        runtime.active_requests_by_task[state.task_id] = state.request_id
        runtime._continue_turn_after_vault = AsyncMock()
        body = LookupRequest(query="example.com", task_id=state.task_id, session_id=state.session_id,
                             channel=state.channel, wait_for_approval_sec=0.3 if decision == "timeout" else 2)
        lookup = asyncio.create_task(internal_lookup(body, request(runtime)))
        for _ in range(200):
            if runtime.vault_store.list_pending(status="pending"):
                break
            if lookup.done():
                await lookup
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("lookup did not publish an approval")
        pending = runtime.vault_store.list_pending(status="pending")[0]
        resume = await runtime.build_resume_payload(channel=state.channel, requested_session_id=state.session_id)
        stream = next(item for item in resume["foreground_streams"] if item["request_id"] == state.request_id)
        assert stream["response_blocks"][0]["stream_offset"] == 9
        if decision == "approved":
            publish = runtime._publish_vault_request_block
            async def delayed_publish(pending):
                await publish(pending)
                await asyncio.sleep(0.3)
            runtime._publish_vault_request_block = delayed_publish
            await runtime.approve_vault_request(pending["request_id"])
        elif decision == "rejected":
            await runtime.reject_vault_request(pending["request_id"])
        result = await lookup
        assert result["status"] == {"approved": "ok", "rejected": "denied", "timeout": "permission_required"}[decision]
        runtime._continue_turn_after_vault.assert_not_called()
        assert not runtime._vault_inline_waiters
        if decision == "approved":
            assert result["credential_ref"] == f"vault:{entry['entry_id']}"
            # The one-use grant survives lookup, but a subsequent use needs a NEW card.
            assert runtime.vault_store.take_approved_use(entry["entry_id"], state.task_id, state.session_id)
            again = await internal_lookup(body.model_copy(update={"wait_for_approval_sec": 0}), request(runtime))
            assert again["status"] == "permission_required"
            assert again["request_id"] != pending["request_id"]
        block = runtime._session_vault_action_blocks[state.session_id][f"vault_request:{pending['request_id']}"]["block"]
        assert block["stream_offset"] == 9
        assert block["owner_request_id"] == state.request_id
        # A different request cannot steal the pending card at persistence time.
        unrelated = runtime._merge_session_vault_action_blocks(state.session_id, {}, "req_other")
        assert not unrelated.get("response_blocks")
        metadata = runtime._merge_session_vault_action_blocks(state.session_id, {}, state.request_id)
        runtime.session_store.append_message(state.session_id, role="assistant", content="Before.\n\nAfter.", metadata=metadata)
        runtime.active_requests.clear()
        stored = runtime.session_store.get_history(state.session_id)[-1]["metadata"]["response_blocks"]
        assert next(b for b in stored if b["id"] == block["id"])["stream_offset"] == 9
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_legacy_approval_waits_for_original_worker_before_resuming(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime.vault_store = VaultStore(tmp_path / "vault.db")
    await runtime.start()
    try:
        finished = asyncio.Event()
        owner = ActiveRequest(request_id="req_original", session_id=runtime._current_session_id(), task_id="tsk_original",
                              channel="desktop:desk_inline", route="orchestrator")
        async def worker():
            await finished.wait()
            owner.completed = True
        owner.worker = asyncio.create_task(worker())
        runtime.active_requests[owner.request_id] = owner
        runtime.active_requests_by_task[owner.task_id] = owner.request_id
        records = []
        runtime.start_request_fulfillment = records.append
        pending = dict(channel=owner.channel, session_id=owner.session_id, task_id=owner.task_id,
                       request_id="vault_legacy", status="approved", entry_id="entry", payload={"title": "Example"})
        continuation = asyncio.create_task(runtime._continue_turn_after_vault(pending))
        await asyncio.sleep(0)
        assert not records
        finished.set()
        await continuation
        assert len(records) == 1
        assert owner.completed
    finally:
        await runtime.stop()
