from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from orchestrator.config import OrchestratorConfig
from orchestrator.runtime import OrchestratorRuntime
from shared import TaskEnvelope, sign_task_envelope, utcnow


def _signed_task(signing_secret: str) -> TaskEnvelope:
    task = TaskEnvelope(
        task_id="tsk_test123",
        task_list_id="sess_20260307",
        session_id="sess_20260307",
        sender="cosmic/gateway:1.0.0",
        recipient="cosmic/orchestrator:1.0.0",
        intent="orchestrator.process",
        input={"query": "Read this Gmail thread."},
        idempotency_key="idem_test123",
        priority="high",
        signature="",
        created_at=utcnow(),
        source="user",
        source_id="desktop",
        channel="desktop:desk_test",
    )
    return task.model_copy(update={"signature": sign_task_envelope(task, signing_secret)})


def _runtime_with_resolver(
    tmp_path: Path,
    seen_payloads: list[dict[str, object]],
) -> tuple[OrchestratorRuntime, httpx.AsyncClient]:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/internal/credentials/resolve"
        payload = json.loads(request.content.decode("utf-8"))
        seen_payloads.append(payload)
        return httpx.Response(
            200,
            json={
                "access_token": "gmail-token",
                "account_id": "acc_real",
                "account_email": "user@example.com",
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://gateway.test",
    )
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        task_ledger_db_path=tmp_path / "task_ledger.db",
        gateway_url="http://gateway.test",
    )
    return OrchestratorRuntime(config, client=client), client


@pytest.mark.asyncio
async def test_resolve_auth_treats_email_account_id_as_hint(tmp_path: Path) -> None:
    seen_payloads: list[dict[str, object]] = []
    runtime, client = _runtime_with_resolver(tmp_path, seen_payloads)
    try:
        resolved = await runtime._resolve_auth_for_child_task(
            parent_task=_signed_task("signing-secret"),
            recipient="cosmic/gmail-agent:1.0.0",
            intent="gmail.read_thread",
            child_input={
                "account_id": "user@example.com",
                "thread_id": "thr_1",
            },
            auth_requirement={
                "provider": "google",
                "scopes": ["https://www.googleapis.com/auth/gmail.modify"],
            },
        )
    finally:
        await client.aclose()

    assert resolved["access_token"] == "gmail-token"
    assert seen_payloads == [
        {
            "provider": "google",
            "required_scopes": ["https://www.googleapis.com/auth/gmail.modify"],
            "session_id": "sess_20260307",
            "operation_mode": "read",
            "allow_primary_fallback": True,
            "account_hint": "user@example.com",
        }
    ]


@pytest.mark.asyncio
async def test_resolve_auth_uses_nested_stable_account_id(tmp_path: Path) -> None:
    seen_payloads: list[dict[str, object]] = []
    runtime, client = _runtime_with_resolver(tmp_path, seen_payloads)
    try:
        await runtime._resolve_auth_for_child_task(
            parent_task=_signed_task("signing-secret"),
            recipient="cosmic/gmail-agent:1.0.0",
            intent="gmail.read_thread",
            child_input={
                "account": {
                    "account_id": "acc_a83a2c5b1199",
                    "account_email": "user@example.com",
                },
                "thread_id": "thr_1",
            },
            auth_requirement={
                "provider": "google",
                "scopes": ["https://www.googleapis.com/auth/gmail.modify"],
            },
        )
    finally:
        await client.aclose()

    assert seen_payloads == [
        {
            "provider": "google",
            "required_scopes": ["https://www.googleapis.com/auth/gmail.modify"],
            "session_id": "sess_20260307",
            "operation_mode": "read",
            "allow_primary_fallback": True,
            "account_id": "acc_a83a2c5b1199",
            "account_hint": "user@example.com",
        }
    ]

