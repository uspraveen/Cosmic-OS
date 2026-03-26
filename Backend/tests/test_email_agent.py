from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agents.email_agent.agent import EmailAgent
from agents.email_agent.config import EmailAgentConfig
from shared.contracts import TaskEnvelope


class _FakeRedis:
    pass


def _make_task(
    *,
    intent: str,
    input_payload: dict[str, Any],
    task_id: str,
    session_id: str = "sess_email_1",
    input_artifacts: list[dict[str, Any]] | None = None,
) -> TaskEnvelope:
    return TaskEnvelope(
        task_id=task_id,
        task_list_id=session_id,
        session_id=session_id,
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/email-agent:1.0.0",
        intent=intent,
        input=input_payload,
        input_artifacts=input_artifacts or [],
        idempotency_key=f"idem_{task_id}",
        signature="test-signature",
        source="user",
        source_id="desktop",
        channel="desktop:local",
    )


def _build_agent(tmp_path: Path, *, config: EmailAgentConfig) -> EmailAgent:
    agent = EmailAgent(
        redis_client=_FakeRedis(),
        config=config,
        registry_db_path=tmp_path / "registry.db",
        artifacts_root=tmp_path / "runs" / "artifacts",
        store_root=tmp_path / "store",
        runtime_root=tmp_path / "runtime",
    )
    agent.data_root.mkdir(parents=True, exist_ok=True)
    agent.runtime_root.mkdir(parents=True, exist_ok=True)
    agent.artifacts_root.mkdir(parents=True, exist_ok=True)
    agent._init_db()
    return agent


@pytest.mark.asyncio
async def test_email_agent_manage_instruction_roundtrip_and_recall(tmp_path: Path) -> None:
    agent = _build_agent(
        tmp_path,
        config=EmailAgentConfig(
            gateway_internal_token="",
            enable_internal_llm=False,
        ),
    )

    set_task = _make_task(
        intent="email.manage_instruction",
        task_id="tsk_set_1",
        input_payload={
            "action": "set",
            "mailbox_address": "support@example.com",
            "label": "Auto reply invoices",
            "match": {"from_address": "billing@example.com"},
            "behavior": {"mode": "auto_reply", "reply_template": "Thanks, we received it."},
        },
    )
    set_result = await agent.execute(set_task)
    assert set_result.status == "completed"
    instruction = set_result.output["instruction"]
    assert instruction["label"] == "Auto reply invoices"
    instruction_id = instruction["instruction_id"]

    disable_task = _make_task(
        intent="email.manage_instruction",
        task_id="tsk_disable_1",
        input_payload={
            "action": "disable",
            "instruction_id": instruction_id,
            "mailbox_address": "support@example.com",
        },
    )
    disable_result = await agent.execute(disable_task)
    assert disable_result.status == "completed"
    assert disable_result.output["instruction"]["enabled"] is False

    list_task = _make_task(
        intent="email.manage_instruction",
        task_id="tsk_list_1",
        input_payload={
            "action": "list",
            "mailbox_address": "support@example.com",
        },
    )
    list_result = await agent.execute(list_task)
    assert list_result.status == "completed"
    assert len(list_result.output["instructions"]) == 1

    recall_task = _make_task(
        intent="email.recall_session",
        task_id="tsk_recall_1",
        input_payload={"limit": 5},
    )
    recall_result = await agent.execute(recall_task)
    assert recall_result.status == "completed"
    assert any(run["task_id"] == "tsk_disable_1" for run in recall_result.output["runs"])
    assert any(run["task_id"] == "tsk_set_1" for run in recall_result.output["runs"])


@pytest.mark.asyncio
async def test_email_agent_reason_compose_draft_uses_compact_brief_and_uploads_input_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _build_agent(
        tmp_path,
        config=EmailAgentConfig(
            cosmic_mail_base_url="http://cosmic-mail.local",
            cosmic_mail_api_token="mail-token",
            gateway_internal_token="",
            enable_internal_llm=True,
        ),
    )

    import agents.email_agent.agent as email_agent_module

    async def fake_invoke_email_mimo_json(**kwargs):
        return {
            "subject": "YC company sheet",
            "body": "Attached is the latest YC company sheet.",
            "summary": "Prepared an outbound email draft.",
        }

    monkeypatch.setattr(email_agent_module, "invoke_email_mimo_json", fake_invoke_email_mimo_json)

    uploaded: list[tuple[str, str, bytes, str | None]] = []

    class FakeMailClient:
        async def resolve_mailbox(self, *, mailbox_id=None, mailbox_address=None):
            assert mailbox_id is None
            assert mailbox_address == "assistant@example.com"
            return {"id": "mbx_primary"}

        async def create_draft(self, payload):
            assert payload["mailbox_id"] == "mbx_primary"
            assert payload["subject"] == "YC company sheet"
            return {"id": "draft_123"}

        async def upload_draft_attachment(self, draft_id, *, filename, content, mime_type=None):
            uploaded.append((draft_id, filename, content, mime_type))
            return {"id": "upl_1"}

    agent.mail_client = FakeMailClient()  # type: ignore[assignment]

    attachment_path = tmp_path / "yc.csv"
    attachment_path.write_text("company\nAcme\n", encoding="utf-8")
    task = _make_task(
        intent="email.reason",
        task_id="tsk_reason_1",
        input_payload={
            "goal": "Email the latest YC sheet to Arun.",
            "mailbox_address": "assistant@example.com",
            "to_recipients": [{"email": "arun@example.com", "name": "Arun"}],
            "context_brief": "User wants to send the latest YC company list.",
        },
        input_artifacts=[
            {
                "artifact_id": "art_local",
                "task_id": "tsk_prev",
                "mime": "text/csv",
                "sha256": "dummy",
                "path": str(attachment_path),
                "created_by_agent": "cosmic/tabular-agent:1.0.0",
            }
        ],
    )

    result = await agent.execute(task)
    assert result.status == "completed"
    assert result.output["action"] == "compose_email"
    assert result.output["draft_id"] == "draft_123"
    assert uploaded
    assert uploaded[0][0] == "draft_123"
    assert uploaded[0][1] == "yc.csv"
    assert result.artifacts
    assert result.artifacts[0].path.startswith("runs/artifacts/")
