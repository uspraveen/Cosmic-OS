from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from shared import (
    AgentEmailIntegrationStore,
    CosmicMailClient,
    CosmicMailClientError,
    agent_email_integration_is_disabled,
    agent_email_integration_is_configured,
    dispatch_task,
    generate_task_id,
    is_supported_document_artifact,
    parse_event_envelope,
    render_markdown_email_bodies,
    sign_task_envelope,
)
from shared.agent_runtime import AgentRuntime
from shared.contracts import AgentError, AgentResult, ArtifactManifest, TaskEnvelope, utcnow
from shared.sqlite_client import connect_sync

from .config import AGENT_ROOT, BACKEND_ROOT, EmailAgentConfig
from .email_usage import log_email_specialist_operation, monotonic_ms_since
from .internal_llm import invoke_email_internal_llm, invoke_email_internal_llm_json

logger = logging.getLogger(__name__)

_RUNS_SQL = """
CREATE TABLE IF NOT EXISTS email_session_runs (
    task_id TEXT PRIMARY KEY,
    session_id TEXT,
    intent TEXT NOT NULL,
    mailbox_address TEXT,
    thread_id TEXT,
    message_id TEXT,
    summary_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_email_runs_session_created
ON email_session_runs (session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_email_runs_thread_created
ON email_session_runs (thread_id, created_at DESC);

CREATE TABLE IF NOT EXISTS email_instructions (
    instruction_id TEXT PRIMARY KEY,
    mailbox_address TEXT,
    label TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    match_from_address TEXT,
    match_subject_contains TEXT,
    match_body_contains TEXT,
    raw_user_instruction TEXT,
    behavior_json TEXT NOT NULL,
    last_triggered_at TEXT,
    completed_at TEXT,
    last_action_thread_id TEXT,
    last_action_message_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_email_instructions_mailbox
ON email_instructions (mailbox_address, enabled);

CREATE TABLE IF NOT EXISTS email_attachment_records (
    record_id TEXT PRIMARY KEY,
    mailbox_address TEXT,
    thread_id TEXT,
    message_id TEXT NOT NULL,
    attachment_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    mime_type TEXT,
    size_bytes INTEGER,
    sha256 TEXT,
    artifact_id TEXT,
    local_path TEXT,
    download_status TEXT NOT NULL DEFAULT 'unknown',
    parse_status TEXT NOT NULL DEFAULT 'not_applicable',
    parse_task_id TEXT,
    parsed_bundle_id TEXT,
    parsed_summary_json TEXT,
    parse_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(message_id, attachment_id)
);
CREATE INDEX IF NOT EXISTS idx_email_attachment_records_thread_message
ON email_attachment_records (thread_id, message_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_email_attachment_records_sha256
ON email_attachment_records (sha256, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_email_attachment_records_parse_status
ON email_attachment_records (parse_status, updated_at DESC);

CREATE TABLE IF NOT EXISTS email_attachment_parse_runs (
    parse_run_id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL,
    parse_task_id TEXT,
    status TEXT NOT NULL,
    parsed_bundle_id TEXT,
    parsed_summary_json TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_email_attachment_parse_runs_record_created
ON email_attachment_parse_runs (record_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_email_attachment_parse_runs_task
ON email_attachment_parse_runs (parse_task_id, updated_at DESC);
"""


class EmailAgentError(RuntimeError):
    def __init__(self, *, code: str, message: str, retryable: bool, next_action: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.next_action = next_action


class EmailAgent(AgentRuntime):
    PROCESS_INBOUND = "email.process_inbound"
    # Sending is the one thing this agent does that cannot be taken back, so a
    # send that fails has to fail loudly and specifically rather than looking
    # like a generic error somebody might reasonably work around.
    HANDLE = "email.handle"
    REASON = "email.reason"
    MANAGE_INSTRUCTION = "email.manage_instruction"
    RECALL_SESSION = "email.recall_session"

    def __init__(
        self,
        *,
        redis_client,
        config: EmailAgentConfig | None = None,
        instance_id: str | None = None,
        agent_secret: str | None = None,
        registry_db_path: str | Path | None = None,
        http_client=None,
        agent_root: str | Path | None = None,
        artifacts_root: str | Path | None = None,
        store_root: str | Path | None = None,
        runtime_root: str | Path | None = None,
    ) -> None:
        self.config = config or EmailAgentConfig.from_env()
        self.agent_root = (Path(agent_root).expanduser() if agent_root else AGENT_ROOT).resolve()
        self.store_root = (Path(store_root).expanduser() if store_root else self.agent_root / "store").resolve()
        self.runtime_root = (Path(runtime_root).expanduser() if runtime_root else self.agent_root / "runtime").resolve()
        self.data_root = self.store_root / "data"
        self.session_db_path = self.data_root / "email_agent.db"
        self.artifacts_root = (
            Path(artifacts_root).expanduser() if artifacts_root else BACKEND_ROOT / "runs" / "artifacts"
        ).resolve()
        self.integration_store = AgentEmailIntegrationStore(self.config.agent_email_integrations_db_path)
        self._env_cosmic_mail_base_url = str(self.config.cosmic_mail_base_url or "").strip()
        self._env_cosmic_mail_api_token = str(self.config.cosmic_mail_api_token or "").strip()
        self._env_primary_mailbox_address = str(self.config.primary_mailbox_address or "").strip()

        super().__init__(
            agent_card_path=self.agent_root / "agent_card.yaml",
            redis_client=redis_client,
            instance_id=instance_id,
            agent_secret=agent_secret,
            registry_db_path=registry_db_path,
            gateway_url=self.config.gateway_url,
            gateway_internal_token=self.config.gateway_internal_token,
            http_client=http_client,
        )
        self._background_jobs: set[asyncio.Task[Any]] = set()
        self.mail_client = CosmicMailClient(
            base_url=self.config.cosmic_mail_base_url,
            api_token=self.config.cosmic_mail_api_token,
            timeout_sec=self.config.cosmic_mail_timeout_sec,
            client=self._http_client,
        )
        self._trusted_sender_set: set[str] = set()

    def _render_outbound_email_body(self, body: str) -> dict[str, str]:
        rendered = render_markdown_email_bodies(body)
        return {
            "text_body": rendered.text_body,
            "html_body": rendered.html_body,
        }

    async def on_startup(self) -> None:
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.artifacts_root.mkdir(parents=True, exist_ok=True)
        self.integration_store.initialize()
        self._init_db()
        await self._refresh_mail_client_from_store()
        if self.config.cosmic_mail_base_url and self.config.cosmic_mail_api_token:
            await self.mail_client.get_auth_context()

    async def stop(self) -> None:
        for job in list(self._background_jobs):
            job.cancel()
        if self._background_jobs:
            await asyncio.gather(*self._background_jobs, return_exceptions=True)
        self._background_jobs.clear()
        await super().stop()

    def _init_db(self) -> None:
        with connect_sync(self.session_db_path) as conn:
            conn.executescript(_RUNS_SQL)
            self._migrate_db(conn)
            conn.commit()

    def _migrate_db(self, conn: Any) -> None:
        self._ensure_table_column(conn, "email_instructions", "raw_user_instruction", "TEXT")
        self._ensure_table_column(conn, "email_instructions", "last_triggered_at", "TEXT")
        self._ensure_table_column(conn, "email_instructions", "completed_at", "TEXT")
        self._ensure_table_column(conn, "email_instructions", "last_action_thread_id", "TEXT")
        self._ensure_table_column(conn, "email_instructions", "last_action_message_id", "TEXT")

    def _ensure_table_column(self, conn: Any, table: str, column: str, definition: str) -> None:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {self._safe_text(row[1]) for row in rows if len(row) > 1}
        if column in existing:
            return
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    async def execute(self, task: TaskEnvelope) -> AgentResult:
        started = time.perf_counter()
        intent = self._canonical_intent(task.intent)
        operation = self._operation_for_intent(intent)
        try:
            if intent == self.PROCESS_INBOUND:
                result = await self._handle_process_inbound(task)
            elif intent == self.HANDLE:
                result = await self._handle_reason(task)
            elif intent == self.MANAGE_INSTRUCTION:
                result = await self._handle_manage_instruction(task)
            elif intent == self.RECALL_SESSION:
                result = await self._handle_recall_session(task)
            else:
                await self._maybe_log_usage(
                    task,
                    operation,
                    started,
                    success=False,
                    error_code="INVALID_INPUT",
                    metadata={"reason": "unsupported_intent"},
                )
                return self._err("INVALID_INPUT", f"Unsupported intent: {task.intent}", False, "escalate")
        except EmailAgentError as exc:
            await self._maybe_log_usage(
                task,
                operation,
                started,
                success=False,
                error_code=exc.code,
                metadata={"next_action": exc.next_action},
            )
            return self._err(exc.code, exc.message, exc.retryable, exc.next_action)
        except Exception as exc:
            logger.exception("email_agent.error task_id=%s", task.task_id)
            await self._maybe_log_usage(
                task,
                operation,
                started,
                success=False,
                error_code="INTERNAL_ERROR",
                metadata={"exception": type(exc).__name__},
            )
            return self._err("INTERNAL_ERROR", str(exc)[:500], False, "escalate")

        await self._maybe_log_usage(
            task,
            operation,
            started,
            success=True,
            error_code=None,
            metadata=self._usage_metadata(intent, result),
        )
        return result

    def _canonical_intent(self, intent: str) -> str:
        normalized = self._safe_text(intent)
        if normalized == self.REASON:
            return self.HANDLE
        return normalized

    def _operation_for_intent(self, intent: str) -> str:
        return {
            self.PROCESS_INBOUND: "email.process_inbound",
            self.HANDLE: "email.handle",
            self.REASON: "email.handle",
            self.MANAGE_INSTRUCTION: "email.manage_instruction",
            self.RECALL_SESSION: "email.recall_session",
        }.get(intent, intent)

    def _usage_metadata(self, intent: str, result: AgentResult) -> dict[str, Any]:
        output = result.output if isinstance(result.output, dict) else {}
        metadata: dict[str, Any] = {"status": result.status}
        if intent in {self.PROCESS_INBOUND, self.HANDLE, self.REASON}:
            metadata["thread_id"] = output.get("thread_id")
            metadata["message_id"] = output.get("message_id")
            metadata["sent"] = output.get("sent")
            metadata["action"] = output.get("action")
        elif intent == self.MANAGE_INSTRUCTION:
            instructions = output.get("instructions")
            if isinstance(instructions, list):
                metadata["instruction_count"] = len(instructions)
        elif intent == self.RECALL_SESSION:
            runs = output.get("runs")
            if isinstance(runs, list):
                metadata["run_count"] = len(runs)
        return metadata

    async def _maybe_log_usage(
        self,
        task: TaskEnvelope,
        operation: str,
        started: float,
        *,
        success: bool,
        error_code: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await log_email_specialist_operation(
            cfg=self.config,
            http_client=self._http_client,
            operation=operation,
            task=task,
            latency_ms=monotonic_ms_since(started),
            success=success,
            error_code=error_code,
            metadata=metadata,
        )

    async def _handle_process_inbound(self, task: TaskEnvelope) -> AgentResult:
        await self._ensure_mail_client_ready()
        thread_id = self._required_text(task.input, "thread_id")
        message_id = self._required_text(task.input, "message_id")
        mailbox_address = self._optional_text(task.input, "mailbox_address")

        context = await self._fetch_thread_context(
            thread_id=thread_id,
            message_id=message_id,
            mailbox_address=mailbox_address,
            mailbox_id=self._optional_text(task.input, "mailbox_id"),
        )
        from_address = self._thread_sender(context)
        trusted_sender = self._is_trusted_sender(from_address)
        sender_role = "owner" if trusted_sender else "external"
        attachments, attachment_artifacts = await self._download_message_attachments(task, message_id=message_id)
        attachments = await self._reconcile_inbound_attachments(
            task,
            mailbox_address=mailbox_address,
            thread_id=thread_id,
            message_id=message_id,
            attachments=attachments,
        )
        matched_instructions, instruction_match_reason = await self._resolve_matched_instructions(
            task=task,
            mailbox_address=mailbox_address,
            from_address=self._thread_sender(context),
            subject=context.get("subject"),
            body=context.get("latest_body"),
            context=context,
        )
        matched_instruction = matched_instructions[0] if matched_instructions else None
        if matched_instructions:
            self._mark_instructions_triggered(
                instruction_ids=[self._safe_text(item.get("instruction_id")) for item in matched_instructions],
            )
        summary = await self._summarize_thread(
            task=task,
            context=context,
            matched_instruction=matched_instruction,
            matched_instructions=matched_instructions,
            operation="email.internal_llm.process_inbound",
        )
        summary = self._augment_thread_summary_with_attachments(summary=summary, attachments=attachments)
        auto_reply = None
        if len(matched_instructions) == 1 and matched_instruction and self._instruction_mode(matched_instruction) == "auto_reply":
            auto_reply = await self._apply_auto_reply(task, context=context, instruction=matched_instruction)
            if auto_reply and auto_reply.get("sent"):
                self._record_instruction_delivery(
                    instruction_ids=[self._safe_text(matched_instruction.get("instruction_id"))],
                    thread_id=thread_id,
                    message_id=message_id,
                )

        artifacts = [
            self._write_json_artifact(
                task_id=task.task_id,
                name="thread_snapshot.json",
                payload=context,
                mime="application/json",
                kind="intermediate",
                audience="supporting",
            )
        ]
        artifacts.extend(attachment_artifacts)
        if attachments:
            artifacts.append(
                self._write_json_artifact(
                    task_id=task.task_id,
                    name="downloaded_attachments.json",
                    payload={"attachments": attachments},
                    mime="application/json",
                    kind="intermediate",
                    audience="supporting",
                )
            )
            artifacts.append(
                self._write_json_artifact(
                    task_id=task.task_id,
                    name="attachment_parse_results.json",
                    payload={"attachments": attachments},
                    mime="application/json",
                    kind="intermediate",
                    audience="supporting",
                )
            )
        if auto_reply:
            artifacts.append(
                self._write_json_artifact(
                    task_id=task.task_id,
                    name="auto_reply_result.json",
                    payload=auto_reply,
                    mime="application/json",
                    kind="output",
                    audience="supporting",
                )
            )

        output = {
            "response": summary,
            "summary": summary,
            "thread_id": thread_id,
            "message_id": message_id,
            "mailbox_address": mailbox_address,
            "from_address": from_address,
            "trusted_sender": trusted_sender,
            "sender_role": sender_role,
            "subject": context.get("subject"),
            "matched_instruction": matched_instruction,
            "matched_instructions": matched_instructions,
            "instruction_match_reason": instruction_match_reason,
            "auto_reply": auto_reply,
            "attachments": attachments,
            "action": "processed_inbound",
            "sent": bool(auto_reply and auto_reply.get("sent")),
            "delivery_status": self._safe_text(auto_reply.get("delivery_status")) if isinstance(auto_reply, dict) else None,
        }
        self._record_session_run(
            task=task,
            intent=self.PROCESS_INBOUND,
            mailbox_address=mailbox_address,
            thread_id=thread_id,
            message_id=message_id,
            summary=output,
        )
        return AgentResult(status="completed", output=output, artifacts=artifacts, error=None)

    async def _handle_reason(self, task: TaskEnvelope) -> AgentResult:
        await self._ensure_mail_client_ready()
        goal = self._required_text(task.input, "goal")
        thread_id = self._optional_text(task.input, "thread_id")
        message_id = self._optional_text(task.input, "message_id")
        mailbox_address = self._optional_text(task.input, "mailbox_address")
        mailbox_id = self._optional_text(task.input, "mailbox_id")
        query = self._optional_text(task.input, "query")
        send = bool(task.input.get("send"))
        subject = self._optional_text(task.input, "subject")
        tone_hint = self._optional_text(task.input, "tone_hint")
        context_brief = self._optional_text(task.input, "context_brief")
        draft_seed = self._optional_text(task.input, "draft_seed")
        draft_id = self._optional_text(task.input, "draft_id")
        attachment_id = self._optional_text(task.input, "attachment_id")
        attachment_name = self._optional_text(task.input, "attachment_name")
        plan = self._build_reason_execution_plan(
            goal=goal,
            query=query,
            context_brief=context_brief,
            draft_seed=draft_seed,
            send=send,
            subject=subject,
            to_recipients=self._normalize_recipient_list(task.input.get("to_recipients")),
            cc_recipients=self._normalize_recipient_list(task.input.get("cc_recipients")),
            bcc_recipients=self._normalize_recipient_list(task.input.get("bcc_recipients")),
            draft_id=draft_id,
        )
        query = plan["query"]
        send = plan["send"]
        subject = plan["subject"]
        draft_seed = plan["draft_seed"]
        recipients = plan["to_recipients"]
        cc_recipients = plan["cc_recipients"]
        bcc_recipients = plan["bcc_recipients"]
        draft_id = plan["draft_id"]
        mode_hint = plan["mode_hint"]
        read_like_goal = plan["read_like_goal"]
        artifacts: list[ArtifactManifest] = []
        default_docs_tools: list[str] = []

        outbound_artifact_reply = bool(task.input_artifacts) and (send or mode_hint == "compose" or self._looks_like_reply(goal))
        if (thread_id or message_id) and not outbound_artifact_reply and self._looks_like_attachment_goal(goal, attachment_name=attachment_name):
            resolution = await self._resolve_attachment_for_reason(
                task=task,
                goal=goal,
                thread_id=thread_id,
                message_id=message_id,
                mailbox_address=mailbox_address,
                attachment_id=attachment_id,
                attachment_name=attachment_name,
            )
            artifacts.append(
                self._write_json_artifact(
                    task_id=task.task_id,
                    name="attachment_resolution.json",
                    payload=resolution,
                    mime="application/json",
                    kind="intermediate",
                    audience="supporting",
                )
            )
            output = {
                "response": self._format_attachment_resolution_response(resolution),
                "action": "resolve_attachment",
                "sent": False,
                "thread_id": self._safe_text(resolution.get("thread_id")) or thread_id,
                "message_id": self._safe_text(resolution.get("message_id")) or message_id,
                "draft_id": None,
                "summary": self._format_attachment_resolution_response(resolution),
                "to_recipients": recipients,
                "cc_recipients": cc_recipients,
                "bcc_recipients": bcc_recipients,
                "search_results": [],
                "bundle_id": self._safe_text(resolution.get("bundle_id")) or None,
                "docs_tools": resolution.get("docs_tools") if isinstance(resolution.get("docs_tools"), list) else [],
                "resolved_attachment": resolution,
                "attachment_resolution_status": self._safe_text(resolution.get("attachment_resolution_status")) or None,
            }
            self._record_session_run(
                task=task,
                intent=self.HANDLE,
                mailbox_address=mailbox_address,
                thread_id=self._safe_text(output.get("thread_id")) or thread_id,
                message_id=self._safe_text(output.get("message_id")) or message_id,
                summary=output,
            )
            return AgentResult(status="completed", output=output, artifacts=artifacts, error=None)

        if draft_id and send and not read_like_goal:
            sent_payload = await self._send_draft_checked(draft_id, origin="supplied in task input")
            delivery = self._normalize_mail_delivery_result(sent_payload, draft_id=draft_id)
            artifacts.append(
                self._write_json_artifact(
                    task_id=task.task_id,
                    name="send_result.json",
                    payload=sent_payload,
                    mime="application/json",
                    kind="output",
                    audience="supporting",
                )
            )
            response_text = "Queued the existing draft for delivery."
            delivery_note = self._mail_delivery_note(delivery)
            if delivery_note:
                response_text = delivery_note
            output = {
                "response": response_text,
                "action": "send_existing_draft",
                "sent": bool(delivery and delivery.get("sent")),
                "thread_id": self._safe_text(delivery.get("thread_id")) if isinstance(delivery, dict) else None,
                "message_id": self._safe_text(delivery.get("message_id")) if isinstance(delivery, dict) else None,
                "draft_id": self._safe_text(delivery.get("draft_id")) if isinstance(delivery, dict) else draft_id,
                "summary": response_text,
                "to_recipients": recipients,
                "cc_recipients": cc_recipients,
                "bcc_recipients": bcc_recipients,
                "search_results": [],
                "bundle_id": None,
                "docs_tools": default_docs_tools,
                "resolved_attachment": None,
                "attachment_resolution_status": None,
                "attached_input_artifact_count": None,
                "attached_input_artifacts": None,
                "failed_input_artifact_count": None,
                "failed_input_artifacts": None,
                "delivery_status": self._safe_text(delivery.get("delivery_status")) if isinstance(delivery, dict) else None,
                "queued_for_approval": bool(delivery.get("queued_for_approval")) if isinstance(delivery, dict) else False,
                "approval_id": self._safe_text(delivery.get("approval_id")) if isinstance(delivery, dict) else None,
            }
            self._record_session_run(
                task=task,
                intent=self.HANDLE,
                mailbox_address=mailbox_address,
                thread_id=self._safe_text(output.get("thread_id")) or thread_id,
                message_id=self._safe_text(output.get("message_id")) or message_id,
                summary=output,
            )
            return AgentResult(status="completed", output=output, artifacts=artifacts, error=None)

        if thread_id:
            context = await self._fetch_thread_context(thread_id=thread_id, message_id=message_id)
            if send or ((draft_seed or mode_hint == "compose") and not read_like_goal) or self._looks_like_reply(goal):
                if bcc_recipients:
                    raise EmailAgentError(
                        code="INVALID_INPUT",
                        message="BCC is not currently supported when replying to an existing email thread. Create a new email draft if BCC recipients are required.",
                        retryable=False,
                        next_action="escalate",
                    )
                drafted = await self._compose_reply(
                    task=task,
                    context=context,
                    goal=goal,
                    context_brief=context_brief,
                    draft_seed=draft_seed,
                    tone_hint=tone_hint,
                    to_recipients=recipients,
                    cc_recipients=cc_recipients,
                )
                sent_payload = None
                delivery = None
                upload_summary: dict[str, Any] = {"attempted": 0, "uploaded": [], "failed": []}
                reply_draft_id: str | None = None
                if send:
                    mailbox = await self._resolve_mailbox(
                        mailbox_address=mailbox_address,
                        mailbox_id=mailbox_id or self._safe_text(context.get("thread", {}).get("mailbox_id")),
                    )
                    if task.input_artifacts:
                        reply_recipients = recipients or self._default_reply_recipients(context, mailbox)
                        if not reply_recipients:
                            raise EmailAgentError(
                                code="INVALID_INPUT",
                                message="Could not determine reply recipients for the thread attachment reply.",
                                retryable=False,
                                next_action="escalate",
                            )
                        draft_payload: dict[str, Any] = {
                            "mailbox_id": mailbox["id"],
                            "thread_id": thread_id,
                            "subject": self._reply_subject(context),
                            "to_recipients": reply_recipients,
                            "cc_recipients": cc_recipients,
                            **self._render_outbound_email_body(drafted["body"]),
                        }
                        reply_to_message_id = self._reply_to_message_id(context)
                        if reply_to_message_id:
                            draft_payload["reply_to_message_id"] = reply_to_message_id
                        draft_response = await self.mail_client.create_draft(draft_payload)
                        reply_draft_id = self._safe_text(draft_response.get("id")) or None
                        if not reply_draft_id:
                            raise EmailAgentError(
                                code="EMAIL_DRAFT_FAILED",
                                message="Cosmic Mail did not return a draft id for the attachment reply.",
                                retryable=True,
                                next_action="retry",
                            )
                        raw_upload_summary = await self._upload_input_artifacts_to_draft(task, draft_id=reply_draft_id)
                        if isinstance(raw_upload_summary, dict):
                            upload_summary = {
                                "attempted": int(raw_upload_summary.get("attempted") or 0),
                                "uploaded": raw_upload_summary.get("uploaded") if isinstance(raw_upload_summary.get("uploaded"), list) else [],
                                "failed": raw_upload_summary.get("failed") if isinstance(raw_upload_summary.get("failed"), list) else [],
                            }
                        sent_payload = await self._send_draft_checked(
                            reply_draft_id, origin="created by this reply"
                        )
                        delivery = self._normalize_mail_delivery_result(sent_payload, draft_id=reply_draft_id, thread_id=thread_id)
                    else:
                        reply_payload: dict[str, Any] = {
                            "mailbox_id": mailbox["id"],
                            **self._render_outbound_email_body(drafted["body"]),
                        }
                        if recipients:
                            reply_payload["to_recipients"] = recipients
                        if cc_recipients:
                            reply_payload["cc_recipients"] = cc_recipients
                        sent_payload = await self.mail_client.reply_to_thread(
                            thread_id,
                            reply_payload,
                        )
                        delivery = self._normalize_mail_delivery_result(sent_payload, thread_id=thread_id)
                    artifacts.append(
                        self._write_json_artifact(
                            task_id=task.task_id,
                            name="reply_result.json",
                            payload=sent_payload,
                            mime="application/json",
                            kind="output",
                            audience="supporting",
                        )
                    )
                response = drafted["summary"]
                attachment_note = self._build_outbound_attachment_note(upload_summary)
                if send and attachment_note:
                    response = f"{response} {attachment_note}".strip()
                delivery_note = self._mail_delivery_note(delivery)
                if send and delivery_note:
                    response = f"{response} {delivery_note}".strip()
                output = {
                    "response": response,
                    "action": "reply_thread",
                    "sent": bool(delivery and delivery.get("sent")),
                    "thread_id": self._safe_text(delivery.get("thread_id")) if isinstance(delivery, dict) else thread_id,
                    "message_id": self._safe_text(delivery.get("message_id")) if isinstance(delivery, dict) else None,
                    "draft_id": reply_draft_id,
                    "summary": response,
                    "to_recipients": recipients,
                    "cc_recipients": cc_recipients,
                    "bcc_recipients": [],
                    "search_results": [],
                    "bundle_id": None,
                    "docs_tools": default_docs_tools,
                    "resolved_attachment": None,
                    "attachment_resolution_status": None,
                    "attached_input_artifact_count": len(upload_summary["uploaded"]) if upload_summary.get("attempted") else None,
                    "attached_input_artifacts": upload_summary["uploaded"] if upload_summary.get("attempted") else None,
                    "failed_input_artifact_count": len(upload_summary["failed"]) if upload_summary.get("attempted") else None,
                    "failed_input_artifacts": upload_summary["failed"] if upload_summary.get("attempted") else None,
                    "delivery_status": self._safe_text(delivery.get("delivery_status")) if isinstance(delivery, dict) else None,
                    "queued_for_approval": bool(delivery.get("queued_for_approval")) if isinstance(delivery, dict) else False,
                    "approval_id": self._safe_text(delivery.get("approval_id")) if isinstance(delivery, dict) else None,
                }
                artifacts.append(
                    self._write_json_artifact(
                        task_id=task.task_id,
                        name="reply_draft.json",
                        payload=drafted,
                        mime="application/json",
                        kind="intermediate",
                        audience="supporting",
                    )
                )
            else:
                summary = await self._summarize_thread(
                    task=task,
                    context=context,
                    matched_instruction=None,
                    operation="email.internal_llm.reason_thread_summary",
                    goal=goal,
                )
                output = {
                    "response": summary,
                    "action": "summarize_thread",
                    "sent": False,
                    "thread_id": thread_id,
                    "message_id": message_id,
                    "draft_id": None,
                    "summary": summary,
                    "to_recipients": recipients,
                    "cc_recipients": cc_recipients,
                    "bcc_recipients": bcc_recipients,
                    "search_results": [],
                    "bundle_id": None,
                    "docs_tools": default_docs_tools,
                    "resolved_attachment": None,
                    "attachment_resolution_status": None,
                }
        elif not read_like_goal and (recipients or cc_recipients or bcc_recipients or send or subject or mode_hint == "compose"):
            drafted = await self._compose_new_email(
                task=task,
                goal=goal,
                context_brief=context_brief,
                draft_seed=draft_seed,
                tone_hint=tone_hint,
                recipients=recipients,
                cc_recipients=cc_recipients,
                bcc_recipients=bcc_recipients,
                subject=subject,
            )
            draft_payload = await self._create_outbound_draft(
                task=task,
                recipients=recipients,
                cc_recipients=cc_recipients,
                bcc_recipients=bcc_recipients,
                subject=drafted["subject"],
                text_body=drafted["body"],
            )
            draft_id = self._safe_text(draft_payload.get("id"))
            upload_summary: dict[str, Any] = {"attempted": 0, "uploaded": [], "failed": []}
            if draft_id:
                raw_upload_summary = await self._upload_input_artifacts_to_draft(task, draft_id=draft_id)
                if isinstance(raw_upload_summary, dict):
                    upload_summary = {
                        "attempted": int(raw_upload_summary.get("attempted") or 0),
                        "uploaded": raw_upload_summary.get("uploaded") if isinstance(raw_upload_summary.get("uploaded"), list) else [],
                        "failed": raw_upload_summary.get("failed") if isinstance(raw_upload_summary.get("failed"), list) else [],
                    }
            sent_payload = None
            delivery = None
            if send and draft_id:
                sent_payload = await self._send_draft_checked(
                    draft_id, origin="created by this compose"
                )
                delivery = self._normalize_mail_delivery_result(sent_payload, draft_id=draft_id)
            artifacts.append(
                self._write_json_artifact(
                    task_id=task.task_id,
                    name="draft_result.json",
                    payload=draft_payload,
                    mime="application/json",
                    kind="output",
                    audience="supporting",
                )
            )
            if sent_payload:
                artifacts.append(
                    self._write_json_artifact(
                        task_id=task.task_id,
                        name="send_result.json",
                        payload=sent_payload,
                        mime="application/json",
                        kind="output",
                        audience="supporting",
                    )
                )
            attachment_note = self._build_outbound_attachment_note(upload_summary)
            response_text = drafted["summary"]
            delivery_note = self._mail_delivery_note(delivery)
            if attachment_note:
                response_text = f"{response_text} {attachment_note}".strip()
            if send and delivery_note:
                response_text = f"{response_text} {delivery_note}".strip()
            output = {
                "response": response_text,
                "action": "compose_email",
                "sent": bool(delivery and delivery.get("sent")),
                "thread_id": self._safe_text(delivery.get("thread_id")) if isinstance(delivery, dict) else None,
                "message_id": self._safe_text(delivery.get("message_id")) if isinstance(delivery, dict) else None,
                "draft_id": self._safe_text(delivery.get("draft_id")) if isinstance(delivery, dict) else draft_id or None,
                "summary": response_text,
                "to_recipients": recipients,
                "cc_recipients": cc_recipients,
                "bcc_recipients": bcc_recipients,
                "search_results": [],
                "bundle_id": None,
                "docs_tools": default_docs_tools,
                "resolved_attachment": None,
                "attachment_resolution_status": None,
                "attached_input_artifact_count": len(upload_summary["uploaded"]),
                "attached_input_artifacts": upload_summary["uploaded"],
                "failed_input_artifact_count": len(upload_summary["failed"]),
                "failed_input_artifacts": upload_summary["failed"],
                "delivery_status": self._safe_text(delivery.get("delivery_status")) if isinstance(delivery, dict) else None,
                "queued_for_approval": bool(delivery.get("queued_for_approval")) if isinstance(delivery, dict) else False,
                "approval_id": self._safe_text(delivery.get("approval_id")) if isinstance(delivery, dict) else None,
            }
        else:
            search_results = await self._search_email(task=task, goal=goal, query=query, mailbox_address=mailbox_address)
            summary = await self._summarize_search_results(task=task, goal=goal, search_results=search_results)
            output = {
                "response": summary,
                "action": "search_email",
                "sent": False,
                "thread_id": None,
                "message_id": None,
                "draft_id": None,
                "summary": summary,
                "to_recipients": recipients,
                "cc_recipients": cc_recipients,
                "bcc_recipients": bcc_recipients,
                "search_results": search_results,
                "bundle_id": None,
                "docs_tools": default_docs_tools,
                "resolved_attachment": None,
                "attachment_resolution_status": None,
            }

        self._record_session_run(
            task=task,
            intent=self.HANDLE,
            mailbox_address=mailbox_address,
            thread_id=thread_id,
            message_id=message_id,
            summary=output,
        )
        return AgentResult(status="completed", output=output, artifacts=artifacts, error=None)

    def _infer_reason_goal_hints(self, goal: str) -> dict[str, Any]:
        text = self._safe_text(goal)
        if not text:
            return {}
        lowered = text.casefold()
        recipient_hints = self._infer_recipient_hints(text)
        email_mentions = recipient_hints["all"]

        subject = ""
        body = ""
        subject_match = re.search(
            r"(?:with\s+)?(?:the\s+)?subject\s*(?::|\bis\b)?\s*['\"“”]?(.+?)['\"“”]?(?=\s*(?:,|\.|;|and\s+(?:the\s+)?following\s+content\s*:|and\s+.+?message|[—\-–]\s*body\s*:|$))",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if subject_match:
            subject = self._safe_text(subject_match.group(1)).strip(" \t\r\n'\"“”")

        body_patterns = (
            r"body\s*:\s*(.+)$",
            r"(?:the\s+)?following\s+content\s*:\s*(.+)$",
            r"something\s+like\s*:\s*(.+)$",
            r"(?:a\s+)?short\s+friendly\s+hello\s+message.+?(?:something\s+like\s*:)\s*(.+)$",
        )
        for pattern in body_patterns:
            body_match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if body_match:
                body = self._safe_text(body_match.group(1)).strip(" \t\r\n'\"“”")
                if body:
                    break

        compose_markers = (
            "send an email",
            "send email",
            "write an email",
            "write email",
            "compose an email",
            "compose email",
            "draft an email",
            "draft email",
            "send a test email",
            "email this",
            "email them",
            "email him",
            "email her",
            "email the",
            "email to",
        )
        is_read_like = self._is_read_like_goal(text)
        is_explicit_compose = any(marker in lowered for marker in compose_markers)
        is_compose = is_explicit_compose and not is_read_like and bool(email_mentions)
        inferred: dict[str, Any] = {
            "subject": subject,
            "body": body,
            "query": text,
        }
        if is_compose:
            inferred["to_recipients"] = recipient_hints["to"] or email_mentions
            inferred["cc_recipients"] = recipient_hints["cc"]
            inferred["bcc_recipients"] = recipient_hints["bcc"]
            inferred["mode"] = "compose"
            inferred["send"] = "send" in lowered and "draft" not in lowered
        return inferred

    def _build_reason_execution_plan(
        self,
        *,
        goal: str,
        query: str | None,
        context_brief: str | None,
        draft_seed: str | None,
        send: bool,
        subject: str | None,
        to_recipients: list[dict[str, Any]],
        cc_recipients: list[dict[str, Any]],
        bcc_recipients: list[dict[str, Any]],
        draft_id: str | None,
    ) -> dict[str, Any]:
        goal_hints = self._infer_reason_goal_hints(goal)
        context_hints = self._infer_reason_goal_hints(context_brief or "")
        structured_draft_hints = self._infer_structured_email_hints(draft_seed or "")
        inferred = self._merge_reason_hints(goal_hints, context_hints, structured_draft_hints)

        if not to_recipients:
            to_recipients = inferred.get("to_recipients") or []
        if not cc_recipients:
            cc_recipients = inferred.get("cc_recipients") or []
        if not bcc_recipients:
            bcc_recipients = inferred.get("bcc_recipients") or []
        to_recipients, cc_recipients, bcc_recipients = self._dedupe_recipient_groups(
            to_recipients,
            cc_recipients,
            bcc_recipients,
        )

        normalized_subject = self._safe_text(subject) or self._safe_text(inferred.get("subject")) or None
        normalized_draft_seed = self._safe_text(draft_seed) or None
        if structured_draft_hints:
            normalized_draft_seed = self._safe_text(structured_draft_hints.get("body")) or None
        if not normalized_draft_seed:
            normalized_draft_seed = self._safe_text(inferred.get("body")) or None
        normalized_query = self._safe_text(query) or self._safe_text(goal_hints.get("query")) or None
        normalized_send = bool(send or goal_hints.get("send") or context_hints.get("send"))
        mode_hint = self._safe_text(inferred.get("mode")).lower()
        read_like_goal = self._is_read_like_goal(goal) or self._is_read_like_goal(normalized_query or "")

        return {
            "query": normalized_query,
            "send": normalized_send,
            "subject": normalized_subject,
            "draft_seed": normalized_draft_seed,
            "to_recipients": to_recipients,
            "cc_recipients": cc_recipients,
            "bcc_recipients": bcc_recipients,
            "draft_id": self._safe_text(draft_id) or None,
            "mode_hint": mode_hint,
            "read_like_goal": read_like_goal,
        }

    def _merge_reason_hints(self, *hints: dict[str, Any]) -> dict[str, Any]:
        merged: dict[str, Any] = {
            "subject": "",
            "body": "",
            "query": "",
            "mode": "",
            "send": False,
            "to_recipients": [],
            "cc_recipients": [],
            "bcc_recipients": [],
        }
        to_recipients: list[dict[str, Any]] = []
        cc_recipients: list[dict[str, Any]] = []
        bcc_recipients: list[dict[str, Any]] = []
        for hint in hints:
            if not isinstance(hint, dict):
                continue
            if not merged["subject"]:
                merged["subject"] = self._safe_text(hint.get("subject"))
            if not merged["body"]:
                merged["body"] = self._safe_text(hint.get("body"))
            if not merged["query"]:
                merged["query"] = self._safe_text(hint.get("query"))
            if not merged["mode"]:
                merged["mode"] = self._safe_text(hint.get("mode"))
            if hint.get("send"):
                merged["send"] = True
            to_recipients.extend(self._normalize_recipient_list(hint.get("to_recipients")))
            cc_recipients.extend(self._normalize_recipient_list(hint.get("cc_recipients")))
            bcc_recipients.extend(self._normalize_recipient_list(hint.get("bcc_recipients")))
        merged["to_recipients"], merged["cc_recipients"], merged["bcc_recipients"] = self._dedupe_recipient_groups(
            to_recipients,
            cc_recipients,
            bcc_recipients,
        )
        return merged

    def _infer_structured_email_hints(self, text: str) -> dict[str, Any]:
        normalized = self._safe_text(text)
        if not normalized:
            return {}

        def _extract_header_segment(label: str) -> str:
            pattern = rf"(?is)(?:^|\n)\s*{label}\s*:\s*(?P<segment>.+?)(?=(?:\n\s*(?:to|cc|bcc|subject)\s*:)|\n{{2,}}|$)"
            match = re.search(pattern, normalized)
            return self._safe_text(match.group("segment")) if match else ""

        subject = _extract_header_segment("subject")
        to_segment = _extract_header_segment("to")
        cc_segment = _extract_header_segment("cc")
        bcc_segment = _extract_header_segment("bcc")
        has_headers = any((subject, to_segment, cc_segment, bcc_segment))
        if not has_headers:
            return {}

        blank_line_split = re.split(r"\n\s*\n", normalized, maxsplit=1)
        body = self._safe_text(blank_line_split[1]) if len(blank_line_split) > 1 else ""
        to_recipients = self._infer_recipient_hints(to_segment)["all"] if to_segment else []
        cc_recipients = self._infer_recipient_hints(cc_segment)["all"] if cc_segment else []
        bcc_recipients = self._infer_recipient_hints(bcc_segment)["all"] if bcc_segment else []
        to_recipients, cc_recipients, bcc_recipients = self._dedupe_recipient_groups(
            to_recipients,
            cc_recipients,
            bcc_recipients,
        )
        return {
            "subject": subject,
            "body": body,
            "mode": "compose" if any((to_recipients, cc_recipients, bcc_recipients, subject, body)) else "",
            "send": False,
            "to_recipients": to_recipients,
            "cc_recipients": cc_recipients,
            "bcc_recipients": bcc_recipients,
        }

    async def _handle_manage_instruction(self, task: TaskEnvelope) -> AgentResult:
        action = self._required_text(task.input, "action").lower()
        mailbox_address = self._optional_text(task.input, "mailbox_address")
        if action == "list":
            instructions = self._list_instructions(mailbox_address=mailbox_address)
            output = {"response": f"Found {len(instructions)} email instructions.", "instructions": instructions, "instruction": None}
            return AgentResult(status="completed", output=output, artifacts=[], error=None)

        instruction_id = self._optional_text(task.input, "instruction_id") or f"eminst_{uuid4().hex[:12]}"
        updated_instruction_ids: list[str] = []
        if action == "set":
            label = self._optional_text(task.input, "label")
            raw_user_instruction = self._optional_text(task.input, "raw_user_instruction") or self._optional_text(task.input, "instruction_text")
            match = self._coerce_instruction_match(task.input.get("match"))
            behavior = task.input.get("behavior") if isinstance(task.input.get("behavior"), dict) else {}
            inferred: dict[str, Any] = {}
            if raw_user_instruction:
                inferred = await self._expand_instruction_from_text(
                    task=task,
                    raw_user_instruction=raw_user_instruction,
                )
            if not label:
                label = self._safe_text(inferred.get("label")) or self._build_instruction_label(raw_user_instruction)
            match = self._merge_instruction_match(
                inferred.get("match") if isinstance(inferred.get("match"), dict) else {},
                match,
            )
            behavior = self._merge_instruction_behavior(
                inferred.get("behavior") if isinstance(inferred.get("behavior"), dict) else {},
                behavior,
            )
            behavior = self._normalize_instruction_behavior(behavior)
            if not label:
                raise EmailAgentError(
                    code="INVALID_INPUT",
                    message="email.manage_instruction set requires a label or natural-language instruction text.",
                    retryable=False,
                    next_action="escalate",
                )
            if not raw_user_instruction and not any(self._safe_text(match.get(key)) for key in ("from_address", "subject_contains", "body_contains")):
                raise EmailAgentError(
                    code="INVALID_INPUT",
                    message="email.manage_instruction set requires either a natural-language instruction text or at least one match condition.",
                    retryable=False,
                    next_action="escalate",
                )
            self._upsert_instruction(
                instruction_id=instruction_id,
                mailbox_address=mailbox_address,
                label=label,
                match=match,
                behavior=behavior,
                raw_user_instruction=raw_user_instruction,
                enabled=True,
            )
            updated_instruction_ids = [instruction_id]
        elif action == "record_delivery":
            updated_instruction_ids = self._normalize_text_list(task.input.get("instruction_ids"))
            if not updated_instruction_ids:
                single_instruction_id = self._optional_text(task.input, "instruction_id")
                if single_instruction_id:
                    updated_instruction_ids = [single_instruction_id]
            if not updated_instruction_ids:
                raise EmailAgentError(
                    code="INVALID_INPUT",
                    message="email.manage_instruction record_delivery requires instruction_ids or instruction_id.",
                    retryable=False,
                    next_action="escalate",
                )
            self._record_instruction_delivery(
                instruction_ids=updated_instruction_ids,
                thread_id=self._optional_text(task.input, "thread_id"),
                message_id=self._optional_text(task.input, "message_id"),
            )
            instruction_id = updated_instruction_ids[0]
        elif action in {"enable", "disable"}:
            self._set_instruction_enabled(instruction_id=instruction_id, enabled=action == "enable")
            updated_instruction_ids = [instruction_id]
        elif action == "remove":
            self._delete_instruction(instruction_id=instruction_id)
            updated_instruction_ids = [instruction_id]
        else:
            raise EmailAgentError(
                code="INVALID_INPUT",
                message=f"Unsupported email.manage_instruction action: {action}",
                retryable=False,
                next_action="escalate",
            )

        instructions = self._list_instructions(mailbox_address=mailbox_address)
        current = next((item for item in instructions if item["instruction_id"] == instruction_id), None)
        output = {
            "response": f"Email instruction action `{action}` applied.",
            "instructions": instructions,
            "instruction": current,
            "updated_instructions": [item for item in instructions if item["instruction_id"] in updated_instruction_ids],
        }
        self._record_session_run(
            task=task,
            intent=self.MANAGE_INSTRUCTION,
            mailbox_address=mailbox_address,
            thread_id=None,
            message_id=None,
            summary=output,
        )
        return AgentResult(status="completed", output=output, artifacts=[], error=None)

    async def _handle_recall_session(self, task: TaskEnvelope) -> AgentResult:
        limit = max(1, min(int(task.input.get("limit") or 5), 20))
        session_id = self._optional_text(task.input, "session_id") or task.session_id
        intent_filter = self._optional_text(task.input, "intent")
        with connect_sync(self.session_db_path) as conn:
            clauses = []
            params: list[Any] = []
            if session_id:
                clauses.append("session_id = ?")
                params.append(session_id)
            if intent_filter:
                clauses.append("intent = ?")
                params.append(intent_filter)
            where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = conn.execute(
                f"""
                SELECT task_id, session_id, intent, mailbox_address, thread_id, message_id, summary_json, created_at
                FROM email_session_runs
                {where_sql}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
        runs = [
            {
                "task_id": row[0],
                "session_id": row[1],
                "intent": row[2],
                "mailbox_address": row[3],
                "thread_id": row[4],
                "message_id": row[5],
                "summary": json.loads(row[6]),
                "created_at": row[7],
            }
            for row in rows
        ]
        return AgentResult(
            status="completed",
            output={
                "response": f"Found {len(runs)} prior email-agent runs.",
                "runs": runs,
            },
            artifacts=[],
            error=None,
        )

    async def _fetch_thread_context(
        self,
        *,
        thread_id: str,
        message_id: str | None,
        mailbox_address: str | None = None,
        mailbox_id: str | None = None,
    ) -> dict[str, Any]:
        thread: dict[str, Any] | None = None
        try:
            messages = await self.mail_client.get_thread_messages(thread_id)
        except CosmicMailClientError as exc:
            raise EmailAgentError(
                code="NETWORK_ERROR" if exc.status_code is None or exc.status_code >= 500 else "INVALID_INPUT",
                message=exc.message,
                retryable=exc.status_code is None or exc.status_code >= 500,
                next_action="retry" if exc.status_code is None or exc.status_code >= 500 else "escalate",
            ) from exc
        try:
            thread = await self.mail_client.get_thread(thread_id)
        except CosmicMailClientError as exc:
            if exc.status_code is None or exc.status_code >= 500:
                raise EmailAgentError(
                    code="NETWORK_ERROR",
                    message=exc.message,
                    retryable=True,
                    next_action="retry",
                ) from exc

        normalized_messages = [self._normalize_message_record(item) for item in messages[: self.config.max_thread_messages]]
        target = None
        if message_id:
            for item in normalized_messages:
                if item["id"] == message_id:
                    target = item
                    break
        if target is None and normalized_messages:
            target = normalized_messages[-1]
        if thread is None:
            thread = await self._fallback_thread_record(
                thread_id=thread_id,
                mailbox_address=mailbox_address,
                mailbox_id=mailbox_id,
                target_message=target,
            )
        latest_body = target.get("text_body") if isinstance(target, dict) else ""
        return {
            "thread": thread,
            "messages": normalized_messages,
            "subject": self._safe_text(thread.get("subject")) or (target.get("subject") if isinstance(target, dict) else None),
            "latest_message": target,
            "latest_body": latest_body,
        }

    async def _fallback_thread_record(
        self,
        *,
        thread_id: str,
        mailbox_address: str | None,
        mailbox_id: str | None,
        target_message: dict[str, Any] | None,
    ) -> dict[str, Any]:
        resolved_mailbox_id = self._safe_text(mailbox_id)
        if not resolved_mailbox_id and self._safe_text(mailbox_address):
            try:
                mailbox = await self._resolve_mailbox(
                    mailbox_address=mailbox_address,
                    mailbox_id=mailbox_id,
                    required=False,
                )
            except EmailAgentError:
                mailbox = {}
            resolved_mailbox_id = self._safe_text(mailbox.get("id")) if isinstance(mailbox, dict) else ""

        if resolved_mailbox_id:
            try:
                threads = await self.mail_client.list_threads(mailbox_id=resolved_mailbox_id, per_page=100)
            except CosmicMailClientError:
                threads = []
            for item in threads:
                if self._safe_text(item.get("id")) == thread_id:
                    return item

        return {
            "id": thread_id,
            "mailbox_id": resolved_mailbox_id or None,
            "subject": self._safe_text(target_message.get("subject")) if isinstance(target_message, dict) else "",
            "snippet": self._safe_text(target_message.get("preview_text")) if isinstance(target_message, dict) else "",
        }

    async def _download_message_attachments(
        self,
        task: TaskEnvelope,
        *,
        message_id: str,
    ) -> tuple[list[dict[str, Any]], list[ArtifactManifest]]:
        try:
            attachments = await self.mail_client.list_message_attachments(message_id)
        except CosmicMailClientError as exc:
            logger.warning("email_agent.list_attachments_failed message_id=%s error=%s", message_id, exc)
            return [], []
        downloaded: list[dict[str, Any]] = []
        manifests: list[ArtifactManifest] = []
        for item in attachments[: self.config.max_attachment_downloads]:
            attachment_id = self._safe_text(item.get("id"))
            if not attachment_id:
                continue
            size_bytes = self._safe_int(item.get("size_bytes") or item.get("size"))
            if size_bytes and size_bytes > self.config.max_attachment_bytes:
                downloaded.append(
                    {
                        "id": attachment_id,
                        "filename": self._safe_text(item.get("filename")) or f"{attachment_id}.bin",
                        "mime_type": self._safe_text(item.get("content_type")) or "application/octet-stream",
                        "size_bytes": size_bytes,
                        "downloaded": False,
                        "reason": "too_large",
                        "parse_status": "skipped_too_large",
                    }
                )
                continue
            try:
                content, mime_type, filename = await self.mail_client.download_attachment(attachment_id)
            except CosmicMailClientError as exc:
                downloaded.append(
                    {
                        "id": attachment_id,
                        "filename": self._safe_text(item.get("filename")) or f"{attachment_id}.bin",
                        "mime_type": self._safe_text(item.get("content_type")) or "application/octet-stream",
                        "size_bytes": size_bytes,
                        "downloaded": False,
                        "reason": exc.message,
                        "parse_status": "download_failed",
                    }
                )
                continue
            target_dir = self._task_artifact_dir(task.task_id) / "attachments" / self._safe_filename(message_id)
            target_dir.mkdir(parents=True, exist_ok=True)
            original_name = self._safe_filename(filename or item.get("filename") or f"{attachment_id}.bin")
            safe_name = f"{self._safe_filename(attachment_id)}__{original_name}"
            target_path = target_dir / safe_name
            target_path.write_bytes(content)
            manifest = self._artifact_manifest(
                task_id=task.task_id,
                path=target_path,
                mime=str(mime_type or item.get("content_type") or "application/octet-stream"),
                kind="input",
                audience="supporting",
            )
            manifests.append(manifest)
            downloaded.append(
                {
                    "id": attachment_id,
                    "filename": original_name,
                    "stored_filename": safe_name,
                    "mime_type": manifest.mime,
                    "size_bytes": len(content),
                    "downloaded": True,
                    "artifact_id": manifest.artifact_id,
                    "path": manifest.path,
                    "sha256": manifest.sha256,
                    "parse_status": "queued",
                }
            )
        return downloaded, manifests

    async def _reconcile_inbound_attachments(
        self,
        task: TaskEnvelope,
        *,
        mailbox_address: str | None,
        thread_id: str,
        message_id: str,
        attachments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        parseable: list[dict[str, Any]] = []
        docs_tools = ["docs_browse", "docs_search", "docs_read", "docs_fetch_asset", "docs_reinspect_asset"]

        for raw_attachment in attachments:
            if not isinstance(raw_attachment, dict):
                continue
            attachment = dict(raw_attachment)
            attachment_id = self._safe_text(attachment.get("id"))
            if not attachment_id:
                continue
            attachment["thread_id"] = thread_id
            attachment["message_id"] = message_id
            attachment["mailbox_address"] = mailbox_address
            attachment["parse_cached"] = False
            record_id = self._attachment_record_id(message_id=message_id, attachment_id=attachment_id)
            attachment["attachment_record_id"] = record_id

            if not attachment.get("downloaded"):
                parse_status = self._safe_text(attachment.get("parse_status")) or "not_downloaded"
                attachment["parse_status"] = parse_status
                self._upsert_attachment_record(
                    record_id=record_id,
                    mailbox_address=mailbox_address,
                    thread_id=thread_id,
                    message_id=message_id,
                    attachment=attachment,
                    download_status=parse_status,
                    parse_status=parse_status,
                )
                normalized.append(attachment)
                continue

            if not is_supported_document_artifact(attachment):
                attachment["parse_status"] = "skipped_unsupported"
                self._upsert_attachment_record(
                    record_id=record_id,
                    mailbox_address=mailbox_address,
                    thread_id=thread_id,
                    message_id=message_id,
                    attachment=attachment,
                    download_status="downloaded",
                    parse_status="skipped_unsupported",
                )
                normalized.append(attachment)
                continue

            cached = self._find_cached_attachment_parse(attachment=attachment)
            if cached:
                parsed_summary = cached.get("parsed_summary") if isinstance(cached.get("parsed_summary"), dict) else None
                attachment["parse_status"] = "parsed"
                attachment["parse_cached"] = True
                attachment["parse_task_id"] = cached.get("parse_task_id")
                attachment["parsed_bundle_id"] = cached.get("parsed_bundle_id")
                attachment["parsed_summary"] = parsed_summary
                attachment["docs_tools"] = docs_tools
                self._upsert_attachment_record(
                    record_id=record_id,
                    mailbox_address=mailbox_address,
                    thread_id=thread_id,
                    message_id=message_id,
                    attachment=attachment,
                    download_status="downloaded",
                    parse_status="parsed",
                    parse_task_id=self._safe_text(cached.get("parse_task_id")) or None,
                    parsed_bundle_id=self._safe_text(cached.get("parsed_bundle_id")) or None,
                    parsed_summary=parsed_summary,
                    parse_error=None,
                )
                self._record_attachment_parse_run(
                    record_id=record_id,
                    parse_task_id=self._safe_text(cached.get("parse_task_id")) or None,
                    status="cached",
                    parsed_bundle_id=self._safe_text(cached.get("parsed_bundle_id")) or None,
                    parsed_summary=parsed_summary,
                    error_message=None,
                )
                normalized.append(attachment)
                continue

            attachment["parse_status"] = "queued"
            self._upsert_attachment_record(
                record_id=record_id,
                mailbox_address=mailbox_address,
                thread_id=thread_id,
                message_id=message_id,
                attachment=attachment,
                download_status="downloaded",
                parse_status="queued",
            )
            parseable.append(attachment)
            normalized.append(attachment)

        if not parseable or not self.config.attachment_docs_auto_parse_enabled:
            if parseable and not self.config.attachment_docs_auto_parse_enabled:
                for attachment in parseable:
                    attachment["parse_status"] = "parse_disabled"
                    self._upsert_attachment_record(
                        record_id=self._safe_text(attachment.get("attachment_record_id")),
                        mailbox_address=mailbox_address,
                        thread_id=thread_id,
                        message_id=message_id,
                        attachment=attachment,
                        download_status="downloaded",
                        parse_status="parse_disabled",
                    )
            return normalized

        parse_result = await self._dispatch_docs_parse_bundle(
            task=task,
            thread_id=thread_id,
            message_id=message_id,
            input_artifacts=[
                {
                    "artifact_id": self._safe_text(item.get("artifact_id")),
                    "path": self._safe_text(item.get("path")),
                    "mime": self._safe_text(item.get("mime_type")),
                    "filename": self._safe_text(item.get("filename")),
                    "sha256": self._safe_text(item.get("sha256")),
                }
                for item in parseable
            ],
        )
        status = self._safe_text(parse_result.get("status")) or "failed"
        parse_task_id = self._safe_text(parse_result.get("task_id")) or None
        if status == "completed":
            output = parse_result.get("output") if isinstance(parse_result.get("output"), dict) else {}
            parsed_bundle_id = self._safe_text(output.get("bundle_id")) or None
            docs_by_artifact_id: dict[str, dict[str, Any]] = {}
            for item in output.get("documents", []) if isinstance(output.get("documents"), list) else []:
                if not isinstance(item, dict):
                    continue
                artifact_id = self._safe_text(item.get("artifact_id"))
                if artifact_id:
                    docs_by_artifact_id[artifact_id] = item
            for attachment in parseable:
                artifact_id = self._safe_text(attachment.get("artifact_id"))
                doc_summary = docs_by_artifact_id.get(artifact_id)
                if doc_summary is None:
                    attachment["parse_status"] = "parse_failed"
                    attachment["parse_error"] = "docs.parse_bundle completed without a document summary for this attachment."
                    self._upsert_attachment_record(
                        record_id=self._safe_text(attachment.get("attachment_record_id")),
                        mailbox_address=mailbox_address,
                        thread_id=thread_id,
                        message_id=message_id,
                        attachment=attachment,
                        download_status="downloaded",
                        parse_status="parse_failed",
                        parse_task_id=parse_task_id,
                        parse_error=self._safe_text(attachment.get("parse_error")) or None,
                    )
                    self._record_attachment_parse_run(
                        record_id=self._safe_text(attachment.get("attachment_record_id")),
                        parse_task_id=parse_task_id,
                        status="failed",
                        parsed_bundle_id=None,
                        parsed_summary=None,
                        error_message=self._safe_text(attachment.get("parse_error")) or None,
                    )
                    continue
                attachment["parse_status"] = "parsed"
                attachment["parse_task_id"] = parse_task_id
                attachment["parsed_bundle_id"] = parsed_bundle_id
                attachment["parsed_summary"] = doc_summary
                attachment["docs_tools"] = docs_tools
                self._upsert_attachment_record(
                    record_id=self._safe_text(attachment.get("attachment_record_id")),
                    mailbox_address=mailbox_address,
                    thread_id=thread_id,
                    message_id=message_id,
                    attachment=attachment,
                    download_status="downloaded",
                    parse_status="parsed",
                    parse_task_id=parse_task_id,
                    parsed_bundle_id=parsed_bundle_id,
                    parsed_summary=doc_summary,
                    parse_error=None,
                )
                self._record_attachment_parse_run(
                    record_id=self._safe_text(attachment.get("attachment_record_id")),
                    parse_task_id=parse_task_id,
                    status="completed",
                    parsed_bundle_id=parsed_bundle_id,
                    parsed_summary=doc_summary,
                    error_message=None,
                )
            return normalized

        error_text = self._safe_text(parse_result.get("error_message")) or "docs.parse_bundle failed"
        failed_status = "parse_pending" if status == "pending" else "parse_failed"
        for attachment in parseable:
            attachment["parse_status"] = failed_status
            attachment["parse_task_id"] = parse_task_id
            attachment["parse_error"] = error_text
            self._upsert_attachment_record(
                record_id=self._safe_text(attachment.get("attachment_record_id")),
                mailbox_address=mailbox_address,
                thread_id=thread_id,
                message_id=message_id,
                attachment=attachment,
                download_status="downloaded",
                parse_status=failed_status,
                parse_task_id=parse_task_id,
                parse_error=error_text,
            )
            self._record_attachment_parse_run(
                record_id=self._safe_text(attachment.get("attachment_record_id")),
                parse_task_id=parse_task_id,
                status=status,
                parsed_bundle_id=None,
                parsed_summary=None,
                error_message=error_text,
            )
        if status == "pending" and parse_task_id:
            self._track_background_job(
                self._reconcile_attachment_parse_bundle(
                    parse_task_id=parse_task_id,
                    mailbox_address=mailbox_address,
                    thread_id=thread_id,
                    message_id=message_id,
                    attachments=[
                        {
                            "attachment_record_id": self._safe_text(item.get("attachment_record_id")),
                            "artifact_id": self._safe_text(item.get("artifact_id")),
                        }
                        for item in parseable
                    ],
                )
            )
        return normalized

    def _attachment_record_id(self, *, message_id: str, attachment_id: str) -> str:
        digest = hashlib.sha256(f"{message_id}:{attachment_id}".encode("utf-8")).hexdigest()[:16]
        return f"ear_{digest}"

    def _upsert_attachment_record(
        self,
        *,
        record_id: str,
        mailbox_address: str | None,
        thread_id: str,
        message_id: str,
        attachment: dict[str, Any],
        download_status: str,
        parse_status: str,
        parse_task_id: str | None = None,
        parsed_bundle_id: str | None = None,
        parsed_summary: dict[str, Any] | None = None,
        parse_error: str | None = None,
    ) -> None:
        attachment_id = self._safe_text(attachment.get("id"))
        existing_row: tuple[Any, ...] | None = None
        if record_id and not attachment_id:
            with connect_sync(self.session_db_path) as conn:
                existing_row = conn.execute(
                    """
                    SELECT attachment_id, filename, mime_type, size_bytes, sha256, artifact_id, local_path
                    FROM email_attachment_records
                    WHERE record_id = ?
                    """,
                    (record_id,),
                ).fetchone()
            if existing_row is not None:
                attachment_id = self._safe_text(existing_row[0])
                attachment = {
                    "id": attachment_id,
                    "filename": self._safe_text(attachment.get("filename")) or self._safe_text(existing_row[1]),
                    "mime_type": self._safe_text(attachment.get("mime_type")) or self._safe_text(existing_row[2]),
                    "size_bytes": self._safe_int(attachment.get("size_bytes")) or self._safe_int(existing_row[3]),
                    "sha256": self._safe_text(attachment.get("sha256")) or self._safe_text(existing_row[4]),
                    "artifact_id": self._safe_text(attachment.get("artifact_id")) or self._safe_text(existing_row[5]),
                    "path": self._safe_text(attachment.get("path")) or self._safe_text(existing_row[6]),
                }
        if not record_id or not attachment_id:
            return
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with connect_sync(self.session_db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO email_attachment_records (
                    record_id,
                    mailbox_address,
                    thread_id,
                    message_id,
                    attachment_id,
                    filename,
                    mime_type,
                    size_bytes,
                    sha256,
                    artifact_id,
                    local_path,
                    download_status,
                    parse_status,
                    parse_task_id,
                    parsed_bundle_id,
                    parsed_summary_json,
                    parse_error,
                    created_at,
                    updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    COALESCE((SELECT created_at FROM email_attachment_records WHERE record_id = ?), ?),
                    ?
                )
                """,
                (
                    record_id,
                    mailbox_address,
                    thread_id,
                    message_id,
                    attachment_id,
                    self._safe_text(attachment.get("filename")) or f"{attachment_id}.bin",
                    self._safe_text(attachment.get("mime_type")) or None,
                    self._safe_int(attachment.get("size_bytes")),
                    self._safe_text(attachment.get("sha256")) or None,
                    self._safe_text(attachment.get("artifact_id")) or None,
                    self._safe_text(attachment.get("path")) or None,
                    download_status,
                    parse_status,
                    parse_task_id,
                    parsed_bundle_id,
                    json.dumps(parsed_summary, ensure_ascii=False) if isinstance(parsed_summary, dict) else None,
                    parse_error,
                    record_id,
                    now,
                    now,
                ),
            )
            conn.commit()

    def _find_cached_attachment_parse(self, *, attachment: dict[str, Any]) -> dict[str, Any] | None:
        sha256 = self._safe_text(attachment.get("sha256"))
        attachment_id = self._safe_text(attachment.get("id"))
        message_id = self._safe_text(attachment.get("message_id"))
        if not attachment_id:
            return None
        row: tuple[Any, ...] | None = None
        with connect_sync(self.session_db_path) as conn:
            if sha256:
                row = conn.execute(
                    """
                    SELECT parse_task_id, parsed_bundle_id, parsed_summary_json, parse_status
                    FROM email_attachment_records
                    WHERE sha256 = ? AND parse_status = 'parsed' AND parsed_bundle_id IS NOT NULL
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (sha256,),
                ).fetchone()
            if row is None and message_id:
                row = conn.execute(
                    """
                    SELECT parse_task_id, parsed_bundle_id, parsed_summary_json, parse_status
                    FROM email_attachment_records
                    WHERE message_id = ? AND attachment_id = ? AND parse_status = 'parsed' AND parsed_bundle_id IS NOT NULL
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (message_id, attachment_id),
                ).fetchone()
        if row is None:
            return None
        try:
            parsed_summary = json.loads(row[2]) if row[2] else None
        except Exception:
            parsed_summary = None
        return {
            "parse_task_id": row[0],
            "parsed_bundle_id": row[1],
            "parsed_summary": parsed_summary if isinstance(parsed_summary, dict) else None,
            "parse_status": row[3],
        }

    def _record_attachment_parse_run(
        self,
        *,
        record_id: str | None,
        parse_task_id: str | None,
        status: str,
        parsed_bundle_id: str | None,
        parsed_summary: dict[str, Any] | None,
        error_message: str | None,
    ) -> None:
        if not record_id:
            return
        parse_run_id = f"epr_{uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with connect_sync(self.session_db_path) as conn:
            conn.execute(
                """
                INSERT INTO email_attachment_parse_runs (
                    parse_run_id,
                    record_id,
                    parse_task_id,
                    status,
                    parsed_bundle_id,
                    parsed_summary_json,
                    error_message,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    parse_run_id,
                    record_id,
                    parse_task_id,
                    status,
                    parsed_bundle_id,
                    json.dumps(parsed_summary, ensure_ascii=False) if isinstance(parsed_summary, dict) else None,
                    error_message,
                    now,
                    now,
                ),
            )
            conn.commit()

    def _track_background_job(self, coro) -> None:
        job = asyncio.create_task(coro)
        self._background_jobs.add(job)
        job.add_done_callback(lambda finished: self._background_jobs.discard(finished))

    async def _reconcile_attachment_parse_bundle(
        self,
        *,
        parse_task_id: str,
        mailbox_address: str | None,
        thread_id: str,
        message_id: str,
        attachments: list[dict[str, Any]],
    ) -> None:
        try:
            parse_result = await self._wait_for_agent_terminal_result(
                parse_task_id,
                timeout_sec=self.config.attachment_docs_parse_reconcile_timeout_sec,
                poll_interval_sec=self.config.attachment_docs_parse_poll_interval_sec,
            )
        except Exception:
            logger.exception(
                "email_agent.attachment_parse_reconcile_failed task_id=%s thread_id=%s message_id=%s",
                parse_task_id,
                thread_id,
                message_id,
            )
            return
        status = self._safe_text(parse_result.get("status")) or "failed"
        if status == "pending":
            logger.warning(
                "email_agent.attachment_parse_reconcile_still_pending task_id=%s thread_id=%s message_id=%s",
                parse_task_id,
                thread_id,
                message_id,
            )
            return
        docs_tools = ["docs_browse", "docs_search", "docs_read", "docs_fetch_asset", "docs_reinspect_asset"]
        output = parse_result.get("output") if isinstance(parse_result.get("output"), dict) else {}
        parsed_bundle_id = self._safe_text(output.get("bundle_id")) or None
        docs_by_artifact_id: dict[str, dict[str, Any]] = {}
        for item in output.get("documents", []) if isinstance(output.get("documents"), list) else []:
            if not isinstance(item, dict):
                continue
            artifact_id = self._safe_text(item.get("artifact_id"))
            if artifact_id:
                docs_by_artifact_id[artifact_id] = item
        error_text = self._safe_text(parse_result.get("error_message")) or "docs.parse_bundle failed"
        for attachment in attachments:
            record_id = self._safe_text(attachment.get("attachment_record_id"))
            artifact_id = self._safe_text(attachment.get("artifact_id"))
            if not record_id:
                continue
            if status == "completed" and artifact_id and artifact_id in docs_by_artifact_id:
                doc_summary = docs_by_artifact_id[artifact_id]
                attachment_payload = {
                    "id": "",
                    "filename": self._safe_text(doc_summary.get("filename")),
                    "artifact_id": artifact_id,
                }
                self._upsert_attachment_record(
                    record_id=record_id,
                    mailbox_address=mailbox_address,
                    thread_id=thread_id,
                    message_id=message_id,
                    attachment=attachment_payload,
                    download_status="downloaded",
                    parse_status="parsed",
                    parse_task_id=parse_task_id,
                    parsed_bundle_id=parsed_bundle_id,
                    parsed_summary=doc_summary,
                    parse_error=None,
                )
                self._record_attachment_parse_run(
                    record_id=record_id,
                    parse_task_id=parse_task_id,
                    status="completed",
                    parsed_bundle_id=parsed_bundle_id,
                    parsed_summary=doc_summary,
                    error_message=None,
                )
                continue
            self._upsert_attachment_record(
                record_id=record_id,
                mailbox_address=mailbox_address,
                thread_id=thread_id,
                message_id=message_id,
                attachment={"id": "", "artifact_id": artifact_id},
                download_status="downloaded",
                parse_status="parse_failed",
                parse_task_id=parse_task_id,
                parse_error=error_text,
            )
            self._record_attachment_parse_run(
                record_id=record_id,
                parse_task_id=parse_task_id,
                status="failed",
                parsed_bundle_id=None,
                parsed_summary=None,
                error_message=error_text,
            )

    async def _dispatch_docs_parse_bundle(
        self,
        *,
        task: TaskEnvelope,
        thread_id: str,
        message_id: str,
        input_artifacts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not input_artifacts:
            return {"status": "failed", "error_message": "No input artifacts were provided for docs parsing."}
        if self.redis is None:
            return {"status": "failed", "error_message": "Redis is not available for docs parsing dispatch."}

        fingerprint = hashlib.sha256(
            json.dumps(
                [
                    {
                        "artifact_id": self._safe_text(item.get("artifact_id")),
                        "path": self._safe_text(item.get("path")),
                        "sha256": self._safe_text(item.get("sha256")),
                    }
                    for item in input_artifacts
                ],
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:16]
        request_id = self._optional_text(task.input, "request_id")
        child_task = TaskEnvelope(
            task_id=generate_task_id(),
            task_list_id=task.task_list_id,
            parent_task_id=task.task_id,
            session_id=task.session_id,
            sender=self.agent_id,
            recipient=self.config.docs_parser_agent_id,
            intent="docs.parse_bundle",
            input={
                "bundle_label": f"email:{thread_id}:{message_id}",
                "ocr_mode": "auto",
                "generate_page_images": False,
                "generate_picture_images": False,
                "request_id": request_id or task.task_id,
            },
            input_artifacts=input_artifacts,
            idempotency_key=f"email-docs-parse:{message_id}:{fingerprint}",
            priority=task.priority,
            signature="",
            created_at=utcnow(),
            source=task.source,
            source_id=task.source_id,
            channel=task.channel,
        )
        child_task = child_task.model_copy(update={"signature": sign_task_envelope(child_task, self.agent_secret)})
        await dispatch_task(child_task, self.redis)
        return await self._wait_for_agent_terminal_result(
            child_task.task_id,
            timeout_sec=self.config.attachment_docs_parse_timeout_sec,
            poll_interval_sec=self.config.attachment_docs_parse_poll_interval_sec,
        )

    async def _wait_for_agent_terminal_result(
        self,
        task_id: str,
        *,
        timeout_sec: float,
        poll_interval_sec: float,
    ) -> dict[str, Any]:
        if self.redis is None:
            return {"status": "failed", "error_message": "Redis is not available for agent result tracking."}
        event_ids_key = f"task_events:{task_id}"
        seen_message_ids: set[str] = set()
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            message_ids = await self.redis.lrange(event_ids_key, 0, -1)
            for message_id in message_ids:
                if message_id in seen_message_ids:
                    continue
                seen_message_ids.add(message_id)
                stream_entries = await self.redis.xrange("streams:events", min=message_id, max=message_id)
                for _, fields in stream_entries:
                    event = parse_event_envelope(fields)
                    if event.task_id != task_id:
                        continue
                    if event.event_type == "task.completed":
                        return {
                            "status": "completed",
                            "task_id": task_id,
                            "output": event.payload.get("output") if isinstance(event.payload, dict) else {},
                            "artifacts": event.payload.get("artifacts") if isinstance(event.payload, dict) else [],
                        }
                    if event.event_type == "task.failed":
                        error = event.payload.get("error") if isinstance(event.payload, dict) else {}
                        return {
                            "status": "failed",
                            "task_id": task_id,
                            "error_message": self._safe_text(error.get("message")) or "Agent task failed.",
                            "error": error,
                        }
                    if event.event_type == "task.rejected":
                        return {
                            "status": "failed",
                            "task_id": task_id,
                            "error_message": self._safe_text(event.payload.get("reason")) or "Agent task was rejected.",
                        }
            await asyncio.sleep(poll_interval_sec)
        return {
            "status": "pending",
            "task_id": task_id,
            "error_message": f"Timed out waiting for {task_id}.",
        }

    def _augment_thread_summary_with_attachments(self, *, summary: str, attachments: list[dict[str, Any]]) -> str:
        attachment_brief = self._build_attachment_brief_for_opus(attachments=attachments)
        if not attachment_brief:
            return summary
        base = self._safe_text(summary).strip()
        if not base:
            return attachment_brief
        return f"{base}\n\n{attachment_brief}"

    def _build_attachment_brief_for_opus(self, *, attachments: list[dict[str, Any]]) -> str:
        if not attachments:
            return ""
        parsed = [
            item for item in attachments
            if isinstance(item, dict) and self._safe_text(item.get("parse_status")) == "parsed"
        ]
        pending = [
            item for item in attachments
            if isinstance(item, dict) and self._safe_text(item.get("parse_status")) == "parse_pending"
        ]
        raw_only = [
            item for item in attachments
            if isinstance(item, dict)
            and self._safe_text(item.get("parse_status")) in {"skipped_unsupported", "skipped_too_large", "parse_disabled"}
        ]
        failed = [
            item for item in attachments
            if isinstance(item, dict) and self._safe_text(item.get("parse_status")) == "parse_failed"
        ]
        parts: list[str] = []
        if parsed:
            names = ", ".join(self._safe_text(item.get("filename")) for item in parsed[:3] if self._safe_text(item.get("filename")))
            detail = f" Parsed document attachments: {names}." if names else ""
            parts.append(
                f"{len(parsed)} attachment(s) were parsed into canonical docs bundles for later read/search.{detail}"
            )
        if pending:
            parts.append(f"{len(pending)} attachment(s) are still waiting on docs parsing.")
        if raw_only:
            parts.append(f"{len(raw_only)} attachment(s) were kept as raw email artifacts only.")
        if failed:
            parts.append(f"{len(failed)} attachment(s) failed document parsing but remain available as raw artifacts.")
        return "Attachments: " + " ".join(parts) if parts else ""

    def _looks_like_attachment_goal(self, goal: str, *, attachment_name: str | None = None) -> bool:
        if attachment_name:
            return True
        lowered = self._safe_text(goal).casefold()
        if not lowered:
            return False
        attachment_tokens = (
            "attachment",
            "attached",
            "pdf",
            "docx",
            "ppt",
            "pptx",
            "presentation",
            "deck",
            "slides",
            "slide ",
            "document",
            "file",
        )
        read_tokens = (
            "read ",
            "open ",
            "search ",
            "summarize ",
            "what does",
            "what is in",
            "show me",
            "look at",
            "inspect ",
            "analyze ",
            "page ",
            "chunk ",
            "section ",
            "slide ",
        )
        return any(token in lowered for token in attachment_tokens) and any(token in lowered for token in read_tokens)

    async def _resolve_attachment_for_reason(
        self,
        *,
        task: TaskEnvelope,
        goal: str,
        thread_id: str | None,
        message_id: str | None,
        mailbox_address: str | None,
        attachment_id: str | None,
        attachment_name: str | None,
    ) -> dict[str, Any]:
        if not thread_id and not message_id:
            raise EmailAgentError(
                code="INVALID_INPUT",
                message="Attachment resolution requires thread_id or message_id context.",
                retryable=False,
                next_action="escalate",
            )
        records = self._list_attachment_records_for_resolution(
            thread_id=thread_id,
            message_id=message_id,
            mailbox_address=mailbox_address,
        )
        if not records:
            raise EmailAgentError(
                code="INVALID_INPUT",
                message="No attachment records were found for this email thread or message.",
                retryable=False,
                next_action="escalate",
            )

        attachment_name_hint = attachment_name or self._extract_attachment_name_hint(goal)
        type_hint = self._infer_attachment_type_hint(goal)
        ordinal_hint = self._extract_attachment_ordinal_hint(goal)
        chosen = self._select_attachment_record(
            records=records,
            attachment_id=attachment_id,
            attachment_name_hint=attachment_name_hint,
            type_hint=type_hint,
            ordinal_hint=ordinal_hint,
        )
        resolved = await self._ensure_attachment_bundle_for_resolution(
            task=task,
            record=chosen,
            mailbox_address=mailbox_address,
        )
        docs_tools = (
            ["docs_browse", "docs_search", "docs_read", "docs_fetch_asset", "docs_reinspect_asset"]
            if self._safe_text(resolved.get("parse_status")) == "parsed" and self._safe_text(resolved.get("parsed_bundle_id"))
            else []
        )
        return {
            "attachment_record_id": self._safe_text(resolved.get("record_id")) or None,
            "attachment_id": self._safe_text(resolved.get("attachment_id")) or None,
            "filename": self._safe_text(resolved.get("filename")) or None,
            "mime_type": self._safe_text(resolved.get("mime_type")) or None,
            "size_bytes": self._safe_int(resolved.get("size_bytes")),
            "thread_id": self._safe_text(resolved.get("thread_id")) or thread_id,
            "message_id": self._safe_text(resolved.get("message_id")) or message_id,
            "mailbox_address": self._safe_text(resolved.get("mailbox_address")) or mailbox_address,
            "download_status": self._safe_text(resolved.get("download_status")) or None,
            "parse_status": self._safe_text(resolved.get("parse_status")) or None,
            "parse_task_id": self._safe_text(resolved.get("parse_task_id")) or None,
            "parse_error": self._safe_text(resolved.get("parse_error")) or None,
            "bundle_id": self._safe_text(resolved.get("parsed_bundle_id")) or None,
            "parsed_summary": resolved.get("parsed_summary") if isinstance(resolved.get("parsed_summary"), dict) else None,
            "docs_tools": docs_tools,
            "path": self._safe_text(resolved.get("local_path")) or None,
            "attachment_resolution_status": self._resolution_status_from_record(resolved),
        }

    def _list_attachment_records_for_resolution(
        self,
        *,
        thread_id: str | None,
        message_id: str | None,
        mailbox_address: str | None,
    ) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        params: list[Any] = []
        if message_id:
            clauses.append("message_id = ?")
            params.append(message_id)
        elif thread_id:
            clauses.append("thread_id = ?")
            params.append(thread_id)
        if mailbox_address:
            clauses.append("(mailbox_address = ? OR mailbox_address IS NULL)")
            params.append(mailbox_address)
        with connect_sync(self.session_db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT
                    record_id,
                    mailbox_address,
                    thread_id,
                    message_id,
                    attachment_id,
                    filename,
                    mime_type,
                    size_bytes,
                    sha256,
                    artifact_id,
                    local_path,
                    download_status,
                    parse_status,
                    parse_task_id,
                    parsed_bundle_id,
                    parsed_summary_json,
                    parse_error,
                    created_at,
                    updated_at
                FROM email_attachment_records
                WHERE {' AND '.join(clauses)}
                ORDER BY updated_at DESC, created_at DESC, attachment_id ASC
                """,
                tuple(params),
            ).fetchall()
        return [self._attachment_record_row_to_dict(row) for row in rows]

    def _attachment_record_row_to_dict(self, row: tuple[Any, ...]) -> dict[str, Any]:
        try:
            parsed_summary = json.loads(row[15]) if row[15] else None
        except Exception:
            parsed_summary = None
        return {
            "record_id": row[0],
            "mailbox_address": row[1],
            "thread_id": row[2],
            "message_id": row[3],
            "attachment_id": row[4],
            "filename": row[5],
            "mime_type": row[6],
            "size_bytes": row[7],
            "sha256": row[8],
            "artifact_id": row[9],
            "local_path": row[10],
            "download_status": row[11],
            "parse_status": row[12],
            "parse_task_id": row[13],
            "parsed_bundle_id": row[14],
            "parsed_summary": parsed_summary if isinstance(parsed_summary, dict) else None,
            "parse_error": row[16],
            "created_at": row[17],
            "updated_at": row[18],
        }

    def _select_attachment_record(
        self,
        *,
        records: list[dict[str, Any]],
        attachment_id: str | None,
        attachment_name_hint: str | None,
        type_hint: str | None,
        ordinal_hint: int | None,
    ) -> dict[str, Any]:
        if len(records) == 1:
            return dict(records[0])
        has_specific_hint = bool(attachment_id or attachment_name_hint or type_hint or ordinal_hint is not None)
        if not has_specific_hint:
            raise EmailAgentError(
                code="INVALID_INPUT",
                message="Multiple attachments exist in this thread. Specify the attachment name or file type more clearly.",
                retryable=False,
                next_action="revise_input",
            )
        scored: list[tuple[int, int, dict[str, Any]]] = []
        for index, record in enumerate(records, start=1):
            score = self._score_attachment_record(
                record,
                attachment_id=attachment_id,
                attachment_name_hint=attachment_name_hint,
                type_hint=type_hint,
                ordinal_hint=ordinal_hint,
                index=index,
            )
            scored.append((score, -index, record))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        if not scored:
            raise EmailAgentError(
                code="INVALID_INPUT",
                message="No attachment records were available to resolve.",
                retryable=False,
                next_action="escalate",
            )
        if len(scored) > 1:
            top_score = scored[0][0]
            second_score = scored[1][0]
            if top_score <= 0 or (has_specific_hint and top_score == second_score):
                raise EmailAgentError(
                    code="INVALID_INPUT",
                    message="Multiple attachment candidates matched this request. Specify the attachment name or file type more clearly.",
                    retryable=False,
                    next_action="revise_input",
                )
        return dict(scored[0][2])

    def _score_attachment_record(
        self,
        record: dict[str, Any],
        *,
        attachment_id: str | None,
        attachment_name_hint: str | None,
        type_hint: str | None,
        ordinal_hint: int | None,
        index: int,
    ) -> int:
        score = 0
        record_attachment_id = self._safe_text(record.get("attachment_id"))
        filename = self._safe_text(record.get("filename")).casefold()
        if attachment_id and record_attachment_id == attachment_id:
            score += 1000
        if attachment_name_hint:
            hint = attachment_name_hint.casefold()
            if filename == hint:
                score += 600
            elif hint in filename:
                score += 420
            elif filename and filename in hint:
                score += 240
        if type_hint and self._attachment_matches_type_hint(record=record, type_hint=type_hint):
            score += 140
        if ordinal_hint is not None:
            if ordinal_hint == -1 and index == 1:
                score += 180
            elif ordinal_hint > 0 and index == ordinal_hint:
                score += 180
            elif ordinal_hint > 0:
                score -= 20
        if self._safe_text(record.get("parse_status")) == "parsed":
            score += 25
        if index == 1:
            score += 12
        return score

    def _attachment_matches_type_hint(self, *, record: dict[str, Any], type_hint: str) -> bool:
        filename = self._safe_text(record.get("filename")).casefold()
        mime = self._safe_text(record.get("mime_type")).casefold()
        if type_hint == "pdf":
            return filename.endswith(".pdf") or mime == "application/pdf"
        if type_hint == "pptx":
            return filename.endswith(".pptx") or mime == "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        if type_hint == "docx":
            return filename.endswith(".docx") or mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        return False

    def _extract_attachment_name_hint(self, goal: str) -> str | None:
        match = re.search(r"([A-Za-z0-9._-]+\.(?:pdf|docx|pptx))", self._safe_text(goal), re.IGNORECASE)
        if not match:
            return None
        return self._safe_text(match.group(1)) or None

    def _infer_attachment_type_hint(self, goal: str) -> str | None:
        lowered = self._safe_text(goal).casefold()
        if "pdf" in lowered:
            return "pdf"
        if any(token in lowered for token in ("pptx", "presentation", "deck", "slides", "slide ")):
            return "pptx"
        if any(token in lowered for token in ("docx", "word doc", "word file")):
            return "docx"
        return None

    def _extract_attachment_ordinal_hint(self, goal: str) -> int | None:
        lowered = self._safe_text(goal).casefold()
        if any(token in lowered for token in ("latest", "last", "most recent")):
            return -1
        mapping = {
            "first": 1,
            "1st": 1,
            "second": 2,
            "2nd": 2,
            "third": 3,
            "3rd": 3,
        }
        for token, ordinal in mapping.items():
            if token in lowered:
                return ordinal
        return None

    async def _ensure_attachment_bundle_for_resolution(
        self,
        *,
        task: TaskEnvelope,
        record: dict[str, Any],
        mailbox_address: str | None,
    ) -> dict[str, Any]:
        if self._safe_text(record.get("parse_status")) == "parsed" and self._safe_text(record.get("parsed_bundle_id")):
            return record
        candidate_artifact = self._artifact_input_from_attachment_record(record)
        if candidate_artifact is None:
            return record
        if not is_supported_document_artifact(candidate_artifact):
            return record
        parse_result = await self._dispatch_docs_parse_bundle(
            task=task,
            thread_id=self._safe_text(record.get("thread_id")),
            message_id=self._safe_text(record.get("message_id")),
            input_artifacts=[candidate_artifact],
        )
        return self._apply_attachment_parse_result(
            mailbox_address=mailbox_address,
            record=record,
            parse_result=parse_result,
        )

    def _artifact_input_from_attachment_record(self, record: dict[str, Any]) -> dict[str, Any] | None:
        artifact_id = self._safe_text(record.get("artifact_id"))
        path = self._safe_text(record.get("local_path"))
        if not artifact_id or not path:
            return None
        return {
            "artifact_id": artifact_id,
            "path": path,
            "mime": self._safe_text(record.get("mime_type")),
            "filename": self._safe_text(record.get("filename")),
            "sha256": self._safe_text(record.get("sha256")),
        }

    def _apply_attachment_parse_result(
        self,
        *,
        mailbox_address: str | None,
        record: dict[str, Any],
        parse_result: dict[str, Any],
    ) -> dict[str, Any]:
        status = self._safe_text(parse_result.get("status")) or "failed"
        parse_task_id = self._safe_text(parse_result.get("task_id")) or None
        updated = dict(record)
        if status == "completed":
            output = parse_result.get("output") if isinstance(parse_result.get("output"), dict) else {}
            parsed_bundle_id = self._safe_text(output.get("bundle_id")) or None
            docs_by_artifact_id: dict[str, dict[str, Any]] = {}
            for item in output.get("documents", []) if isinstance(output.get("documents"), list) else []:
                if not isinstance(item, dict):
                    continue
                artifact_id = self._safe_text(item.get("artifact_id"))
                if artifact_id:
                    docs_by_artifact_id[artifact_id] = item
            doc_summary = docs_by_artifact_id.get(self._safe_text(record.get("artifact_id")))
            if doc_summary is not None:
                updated["parse_status"] = "parsed"
                updated["parse_task_id"] = parse_task_id
                updated["parsed_bundle_id"] = parsed_bundle_id
                updated["parsed_summary"] = doc_summary
                updated["parse_error"] = None
                self._upsert_attachment_record(
                    record_id=self._safe_text(record.get("record_id")),
                    mailbox_address=mailbox_address,
                    thread_id=self._safe_text(record.get("thread_id")),
                    message_id=self._safe_text(record.get("message_id")),
                    attachment={
                        "id": self._safe_text(record.get("attachment_id")),
                        "filename": self._safe_text(record.get("filename")),
                        "mime_type": self._safe_text(record.get("mime_type")),
                        "size_bytes": self._safe_int(record.get("size_bytes")),
                        "sha256": self._safe_text(record.get("sha256")),
                        "artifact_id": self._safe_text(record.get("artifact_id")),
                        "path": self._safe_text(record.get("local_path")),
                    },
                    download_status=self._safe_text(record.get("download_status")) or "downloaded",
                    parse_status="parsed",
                    parse_task_id=parse_task_id,
                    parsed_bundle_id=parsed_bundle_id,
                    parsed_summary=doc_summary,
                    parse_error=None,
                )
                self._record_attachment_parse_run(
                    record_id=self._safe_text(record.get("record_id")),
                    parse_task_id=parse_task_id,
                    status="completed",
                    parsed_bundle_id=parsed_bundle_id,
                    parsed_summary=doc_summary,
                    error_message=None,
                )
                return updated
        error_text = self._safe_text(parse_result.get("error_message")) or "docs.parse_bundle failed"
        failed_status = "parse_pending" if status == "pending" else "parse_failed"
        updated["parse_status"] = failed_status
        updated["parse_task_id"] = parse_task_id
        updated["parse_error"] = error_text
        self._upsert_attachment_record(
            record_id=self._safe_text(record.get("record_id")),
            mailbox_address=mailbox_address,
            thread_id=self._safe_text(record.get("thread_id")),
            message_id=self._safe_text(record.get("message_id")),
            attachment={
                "id": self._safe_text(record.get("attachment_id")),
                "filename": self._safe_text(record.get("filename")),
                "mime_type": self._safe_text(record.get("mime_type")),
                "size_bytes": self._safe_int(record.get("size_bytes")),
                "sha256": self._safe_text(record.get("sha256")),
                "artifact_id": self._safe_text(record.get("artifact_id")),
                "path": self._safe_text(record.get("local_path")),
            },
            download_status=self._safe_text(record.get("download_status")) or "downloaded",
            parse_status=failed_status,
            parse_task_id=parse_task_id,
            parse_error=error_text,
        )
        self._record_attachment_parse_run(
            record_id=self._safe_text(record.get("record_id")),
            parse_task_id=parse_task_id,
            status=status,
            parsed_bundle_id=None,
            parsed_summary=None,
            error_message=error_text,
        )
        if status == "pending" and parse_task_id:
            self._track_background_job(
                self._reconcile_attachment_parse_bundle(
                    parse_task_id=parse_task_id,
                    mailbox_address=mailbox_address,
                    thread_id=self._safe_text(record.get("thread_id")),
                    message_id=self._safe_text(record.get("message_id")),
                    attachments=[
                        {
                            "attachment_record_id": self._safe_text(record.get("record_id")),
                            "artifact_id": self._safe_text(record.get("artifact_id")),
                        }
                    ],
                )
            )
        return updated

    def _resolution_status_from_record(self, record: dict[str, Any]) -> str:
        parse_status = self._safe_text(record.get("parse_status"))
        if parse_status == "parsed" and self._safe_text(record.get("parsed_bundle_id")):
            return "parsed"
        if parse_status == "parse_pending":
            return "parse_pending"
        if parse_status in {"skipped_unsupported", "skipped_too_large", "parse_disabled"}:
            return "raw_only"
        if parse_status == "parse_failed":
            return "parse_failed"
        return parse_status or "resolved"

    def _format_attachment_resolution_response(self, resolution: dict[str, Any]) -> str:
        filename = self._safe_text(resolution.get("filename")) or "the attachment"
        status = self._safe_text(resolution.get("attachment_resolution_status"))
        bundle_id = self._safe_text(resolution.get("bundle_id"))
        if status == "parsed" and bundle_id:
            return (
                f"Resolved attachment `{filename}` from the email thread. "
                f"It is already parsed and ready for the docs tools under bundle `{bundle_id}`."
            )
        if status == "parse_pending":
            return (
                f"Resolved attachment `{filename}` from the email thread. "
                "It is still being parsed, so the docs bundle is not ready yet."
            )
        if status == "raw_only":
            return (
                f"Resolved attachment `{filename}` from the email thread. "
                "It is available as a raw email artifact, but it is not in the docs parsing path."
            )
        if status == "parse_failed":
            return (
                f"Resolved attachment `{filename}` from the email thread, but docs parsing failed. "
                "The raw attachment is still available."
            )
        return f"Resolved attachment `{filename}` from the email thread."

    async def _summarize_thread(
        self,
        *,
        task: TaskEnvelope,
        context: dict[str, Any],
        matched_instruction: dict[str, Any] | None,
        matched_instructions: list[dict[str, Any]] | None = None,
        operation: str,
        goal: str | None = None,
    ) -> str:
        messages = context.get("messages") if isinstance(context.get("messages"), list) else []
        transcript = "\n\n".join(
            f"From: {self._safe_text(item.get('from_address')) or 'unknown'}\nSubject: {self._safe_text(item.get('subject'))}\n{self._safe_text(item.get('text_body'))[:1200]}"
            for item in messages[-8:]
            if isinstance(item, dict)
        )
        prompt = (
            "Summarize this email thread for Opus.\n"
            "Be concise, operational, and explicit about the latest actionable item.\n"
            "Return plain text only.\n"
        )
        if goal:
            prompt += f"\nUser goal:\n{goal}\n"
        if matched_instructions:
            prompt += (
                "\nMatched standing instructions:\n"
                f"{json.dumps(matched_instructions[:6], ensure_ascii=False)}\n"
            )
        elif matched_instruction:
            prompt += f"\nMatched standing instruction:\n{json.dumps(matched_instruction, ensure_ascii=False)}\n"
        prompt += f"\nThread subject: {self._safe_text(context.get('subject'))}\n\nThread:\n{transcript[:24000]}"
        summary = await invoke_email_internal_llm(
            cfg=self.config,
            http_client=self._http_client,
            system_content=self._build_system_prompt(),
            user_message=prompt,
            task_id=task.task_id,
            session_id=task.session_id,
            request_id=self._optional_text(task.input, "request_id"),
            source=task.source,
            source_id=task.source_id,
            channel=task.channel,
            operation=operation,
            max_output_chars=8000,
            temperature=0.2,
        )
        if summary:
            return summary
        latest = context.get("latest_message") if isinstance(context.get("latest_message"), dict) else {}
        latest_excerpt = self._safe_text(latest.get("text_body"))[:600]
        return f"Email thread summary: {self._safe_text(context.get('subject')) or '(no subject)'}\n\nLatest message:\n{latest_excerpt}".strip()

    async def _apply_auto_reply(
        self,
        task: TaskEnvelope,
        *,
        context: dict[str, Any],
        instruction: dict[str, Any],
    ) -> dict[str, Any] | None:
        behavior = instruction.get("behavior") if isinstance(instruction.get("behavior"), dict) else {}
        reply_template = self._safe_text(behavior.get("reply_template"))
        if not reply_template:
            return None
        thread_id = self._safe_text(context.get("thread", {}).get("id"))
        if not thread_id:
            return None
        latest_body = self._safe_text(context.get("latest_body"))
        generated = await invoke_email_internal_llm(
            cfg=self.config,
            http_client=self._http_client,
            system_content=self._build_system_prompt(),
            user_message=(
                "Write a short email reply using this instruction template. "
                "Keep it aligned with the incoming thread and do not mention COSMIC.\n\n"
                f"Instruction template:\n{reply_template}\n\n"
                f"Incoming message:\n{latest_body[:6000]}"
            ),
            task_id=task.task_id,
            session_id=task.session_id,
            request_id=self._optional_text(task.input, "request_id"),
            source=task.source,
            source_id=task.source_id,
            channel=task.channel,
            operation="email.internal_llm.auto_reply",
            max_output_chars=6000,
            temperature=0.2,
        )
        body = generated or reply_template
        mailbox = await self._resolve_mailbox(
            mailbox_address=self._optional_text(task.input, "mailbox_address"),
            mailbox_id=self._optional_text(task.input, "mailbox_id")
            or self._safe_text(context.get("thread", {}).get("mailbox_id")),
        )
        try:
            payload = await self.mail_client.reply_to_thread(
                thread_id,
                {
                    "mailbox_id": mailbox["id"],
                    **self._render_outbound_email_body(body),
                },
            )
        except CosmicMailClientError as exc:
            raise EmailAgentError(
                code="NETWORK_ERROR" if exc.status_code is None or exc.status_code >= 500 else "AUTH_ERROR",
                message=exc.message,
                retryable=exc.status_code is None or exc.status_code >= 500,
                next_action="retry" if exc.status_code is None or exc.status_code >= 500 else "escalate",
            ) from exc
        delivery = self._normalize_mail_delivery_result(payload, thread_id=thread_id)
        return {
            "sent": bool(delivery.get("sent")),
            "delivery_status": self._safe_text(delivery.get("delivery_status")) or None,
            "queued_for_approval": bool(delivery.get("queued_for_approval")),
            "approval_id": self._safe_text(delivery.get("approval_id")) or None,
            "thread_id": self._safe_text(delivery.get("thread_id")) or thread_id,
            "message_id": self._safe_text(delivery.get("message_id")) or None,
            "body": body,
        }

    async def _compose_reply(
        self,
        *,
        task: TaskEnvelope,
        context: dict[str, Any],
        goal: str,
        context_brief: str | None,
        draft_seed: str | None,
        tone_hint: str | None,
        to_recipients: list[dict[str, Any]],
        cc_recipients: list[dict[str, Any]],
    ) -> dict[str, Any]:
        latest_body = self._safe_text(context.get("latest_body"))
        user_message = (
            "Write an email reply draft.\n"
            "Return plain text only.\n"
            f"Goal: {goal}\n"
            f"Tone hint: {tone_hint or 'follow the thread tone'}\n"
            f"Context brief: {context_brief or '(none)'}\n"
            f"Draft seed: {draft_seed or '(none)'}\n"
            f"Explicit reply-to recipients: {self._format_recipients_for_prompt(to_recipients) or '(default thread targets)'}\n"
            f"Explicit CC recipients: {self._format_recipients_for_prompt(cc_recipients) or '(none)'}\n"
            f"Thread subject: {self._safe_text(context.get('subject'))}\n\n"
            f"Latest inbound message:\n{latest_body[:6000]}"
        )
        body = await invoke_email_internal_llm(
            cfg=self.config,
            http_client=self._http_client,
            system_content=self._build_system_prompt(),
            user_message=user_message,
            task_id=task.task_id,
            session_id=task.session_id,
            request_id=self._optional_text(task.input, "request_id"),
            source=task.source,
            source_id=task.source_id,
            channel=task.channel,
            operation="email.internal_llm.compose_reply",
            max_output_chars=8000,
            temperature=0.2,
        )
        reply_body = body or draft_seed or f"Following up on: {goal}"
        return {
            "body": reply_body,
            "summary": "Prepared a reply draft for the existing email thread.",
        }

    async def _compose_new_email(
        self,
        *,
        task: TaskEnvelope,
        goal: str,
        context_brief: str | None,
        draft_seed: str | None,
        tone_hint: str | None,
        recipients: list[dict[str, Any]],
        cc_recipients: list[dict[str, Any]],
        bcc_recipients: list[dict[str, Any]],
        subject: str | None,
    ) -> dict[str, Any]:
        payload = await invoke_email_internal_llm_json(
            cfg=self.config,
            http_client=self._http_client,
            system_content=self._build_system_prompt(),
            user_message=(
                "Write a professional email draft.\n"
                "Return JSON with keys: subject, body, summary.\n"
                "Do not wrap the JSON in markdown.\n\n"
                f"Goal: {goal}\n"
                f"Tone hint: {tone_hint or '(none)'}\n"
                f"Context brief: {context_brief or '(none)'}\n"
                f"Draft seed: {draft_seed or '(none)'}\n"
                f"To recipients: {self._format_recipients_for_prompt(recipients) or '(none)'}\n"
                f"CC recipients: {self._format_recipients_for_prompt(cc_recipients) or '(none)'}\n"
                f"BCC recipients: {self._format_recipients_for_prompt(bcc_recipients) or '(none)'}\n"
                f"Requested subject: {subject or '(none)'}"
            ),
            task_id=task.task_id,
            session_id=task.session_id,
            request_id=self._optional_text(task.input, "request_id"),
            source=task.source,
            source_id=task.source_id,
            channel=task.channel,
            operation="email.internal_llm.compose_new",
        ) or {}
        resolved_subject = self._safe_text(payload.get("subject")) or subject or "COSMIC update"
        resolved_body = self._safe_text(payload.get("body")) or draft_seed
        if not resolved_body:
            raw_body = await invoke_email_internal_llm(
                cfg=self.config,
                http_client=self._http_client,
                system_content=self._build_system_prompt(),
                user_message=(
                    "Write only the final email body text for this request.\n"
                    "Do not include a subject line.\n"
                    "Do not describe what you are doing.\n"
                    "Return only the body that should be sent.\n\n"
                    f"Goal: {goal}\n"
                    f"Tone hint: {tone_hint or '(none)'}\n"
                    f"Context brief: {context_brief or '(none)'}\n"
                    f"Requested subject: {subject or '(none)'}"
                ),
                task_id=task.task_id,
                session_id=task.session_id,
                request_id=self._optional_text(task.input, "request_id"),
                source=task.source,
                source_id=task.source_id,
                channel=task.channel,
                operation="email.internal_llm.compose_new_body",
                max_output_chars=12_000,
                temperature=0.2,
            )
            resolved_body = self._safe_text(raw_body)
        if not resolved_body:
            raise EmailAgentError(
                code="EMAIL_DRAFT_FAILED",
                message="Failed to draft a usable email body.",
                retryable=True,
                next_action="retry",
            )
        resolved_summary = self._safe_text(payload.get("summary")) or "Prepared an outbound email draft."
        return {
            "subject": resolved_subject,
            "body": resolved_body,
            "summary": resolved_summary,
        }

    async def _create_outbound_draft(
        self,
        *,
        task: TaskEnvelope,
        recipients: list[dict[str, Any]],
        cc_recipients: list[dict[str, Any]],
        bcc_recipients: list[dict[str, Any]],
        subject: str,
        text_body: str,
    ) -> dict[str, Any]:
        mailbox = await self._resolve_mailbox(
            mailbox_address=self._optional_text(task.input, "mailbox_address"),
            mailbox_id=self._optional_text(task.input, "mailbox_id"),
        )
        try:
            return await self.mail_client.create_draft(
                {
                    "mailbox_id": mailbox["id"],
                    "subject": subject,
                    "to_recipients": recipients,
                    "cc_recipients": cc_recipients,
                    "bcc_recipients": bcc_recipients,
                    **self._render_outbound_email_body(text_body),
                }
            )
        except CosmicMailClientError as exc:
            raise EmailAgentError(
                code="NETWORK_ERROR" if exc.status_code is None or exc.status_code >= 500 else "AUTH_ERROR",
                message=exc.message,
                retryable=exc.status_code is None or exc.status_code >= 500,
                next_action="retry" if exc.status_code is None or exc.status_code >= 500 else "escalate",
            ) from exc

    async def _send_draft_checked(self, draft_id: str, *, origin: str) -> dict[str, Any]:
        """Send a draft, and make a missing draft a terminal, explicit outcome.

        A `draft_id` can arrive from task input, which means it can be a value a
        model produced rather than an id Cosmic Mail ever issued. When that id
        does not exist the send 404s, and the failure used to surface as a
        generic INTERNAL_ERROR -- indistinguishable from a transient fault, and
        therefore something a caller would sensibly retry "another way". The way
        it retried was to compose a brand new email and send that instead, so a
        request to send one specific draft turned into several unrelated emails
        arriving in the user's inbox.

        Nothing here can stop a caller trying again, but it can refuse to be
        ambiguous about what went wrong and what the acceptable next step is.
        """
        normalized = self._safe_text(draft_id)
        if not normalized:
            raise EmailAgentError(
                code="DRAFT_NOT_FOUND",
                message=(
                    "No draft id was supplied, so there is nothing to send. Do not compose "
                    "and send a replacement email: ask the user which draft they meant."
                ),
                retryable=False,
                next_action="ask_user",
            )
        try:
            return await self.mail_client.send_draft(normalized)
        except CosmicMailClientError as exc:
            if exc.status_code == 404:
                raise EmailAgentError(
                    code="DRAFT_NOT_FOUND",
                    message=(
                        f"Cosmic Mail has no draft {normalized!r} ({origin}), so nothing was sent. "
                        "This id was never issued by Cosmic Mail. Do NOT compose and send a "
                        "replacement email as a fallback -- that delivers mail the user never "
                        "asked for. Either send an existing draft by its real id, or ask the user."
                    ),
                    retryable=False,
                    next_action="ask_user",
                ) from exc
            raise

    async def _upload_input_artifacts_to_draft(self, task: TaskEnvelope, *, draft_id: str) -> dict[str, Any]:
        uploaded: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for artifact in task.input_artifacts:
            if not isinstance(artifact, dict):
                continue
            artifact_id = self._safe_text(artifact.get("artifact_id")) or None
            declared_filename = self._safe_text(artifact.get("filename")) or None
            artifact_path = self._resolve_artifact_path(artifact)
            if artifact_path is None or not artifact_path.exists() or not artifact_path.is_file():
                failed.append(
                    {
                        "artifact_id": artifact_id,
                        "filename": declared_filename,
                        "reason": "artifact_unavailable",
                    }
                )
                continue
            content = artifact_path.read_bytes()
            if not content:
                failed.append(
                    {
                        "artifact_id": artifact_id,
                        "filename": artifact_path.name,
                        "reason": "artifact_empty",
                    }
                )
                continue
            try:
                await self.mail_client.upload_draft_attachment(
                    draft_id,
                    filename=artifact_path.name,
                    content=content,
                    mime_type=self._safe_text(artifact.get("mime")) or None,
                )
                uploaded.append(
                    {
                        "artifact_id": artifact_id,
                        "filename": artifact_path.name,
                        "mime": self._safe_text(artifact.get("mime")) or None,
                    }
                )
            except CosmicMailClientError as exc:
                logger.warning(
                    "email_agent.upload_draft_attachment_failed draft_id=%s path=%s error=%s",
                    draft_id,
                    artifact_path,
                    exc,
                )
                failed.append(
                    {
                        "artifact_id": artifact_id,
                        "filename": artifact_path.name,
                        "reason": "upload_failed",
                    }
                )
        return {
            "attempted": len(uploaded) + len(failed),
            "uploaded": uploaded,
            "failed": failed,
        }

    def _build_outbound_attachment_note(self, upload_summary: dict[str, Any]) -> str | None:
        uploaded = upload_summary.get("uploaded") if isinstance(upload_summary, dict) else None
        failed = upload_summary.get("failed") if isinstance(upload_summary, dict) else None
        uploaded_count = len(uploaded) if isinstance(uploaded, list) else 0
        failed_count = len(failed) if isinstance(failed, list) else 0
        if uploaded_count <= 0 and failed_count <= 0:
            return None
        if uploaded_count > 0 and failed_count <= 0:
            noun = "file" if uploaded_count == 1 else "files"
            return f"Attached {uploaded_count} {noun} to the draft."
        if uploaded_count > 0 and failed_count > 0:
            uploaded_noun = "file" if uploaded_count == 1 else "files"
            failed_noun = "file" if failed_count == 1 else "files"
            return f"Attached {uploaded_count} {uploaded_noun}; {failed_count} {failed_noun} failed to upload."
        noun = "file" if failed_count == 1 else "files"
        return f"{failed_count} {noun} failed to upload."

    async def _search_email(
        self,
        *,
        task: TaskEnvelope,
        goal: str,
        query: str | None,
        mailbox_address: str | None,
    ) -> list[dict[str, Any]]:
        search_query = self._rewrite_search_query_for_sender_reference(query=query or goal, goal=goal)
        mailbox = await self._resolve_mailbox(
            mailbox_address=mailbox_address,
            mailbox_id=self._optional_text(task.input, "mailbox_id"),
            required=True,
        )
        mailbox_id = self._safe_text(mailbox.get("id")) if isinstance(mailbox, dict) else None
        threads: list[dict[str, Any]] = []
        messages: list[dict[str, Any]] = []
        search_threads_error: CosmicMailClientError | None = None
        search_messages_error: CosmicMailClientError | None = None
        try:
            threads = await self.mail_client.search_threads(
                query=search_query,
                mailbox_id=mailbox_id,
                per_page=self.config.max_search_results,
            )
        except CosmicMailClientError as exc:
            search_threads_error = exc
        try:
            messages = await self.mail_client.search_messages(
                query=search_query,
                mailbox_id=mailbox_id,
                per_page=self.config.max_search_results,
            )
        except CosmicMailClientError as exc:
            search_messages_error = exc
            logger.warning(
                "email_agent.search_messages_failed task_id=%s mailbox_id=%s status=%s error=%s",
                task.task_id,
                mailbox_id,
                exc.status_code,
                exc.message,
            )

        if search_threads_error is not None and search_messages_error is not None:
            primary = search_messages_error if (search_messages_error.status_code or 0) >= 500 else search_threads_error
            raise EmailAgentError(
                code="NETWORK_ERROR" if primary.status_code is None or primary.status_code >= 500 else "AUTH_ERROR",
                message=primary.message,
                retryable=primary.status_code is None or primary.status_code >= 500,
                next_action="retry" if primary.status_code is None or primary.status_code >= 500 else "escalate",
            ) from primary
        thread_results = [
            {
                "kind": "thread",
                "id": self._safe_text(item.get("id")),
                "subject": self._safe_text(item.get("subject")),
                "snippet": self._safe_text(item.get("snippet") or item.get("body_preview")),
                "last_message_at": self._safe_text(item.get("last_message_at") or item.get("updated_at")),
            }
            for item in threads[: self.config.max_search_results]
            if isinstance(item, dict)
        ]
        message_results = [
            {
                "kind": "message",
                "id": self._safe_text(item.get("id")),
                "thread_id": self._safe_text(item.get("thread_id")),
                "subject": self._safe_text(item.get("subject")),
                "snippet": self._safe_text(item.get("snippet") or item.get("body_preview") or item.get("text_body")),
            }
            for item in messages[: self.config.max_search_results]
            if isinstance(item, dict)
        ]
        if self._is_read_like_goal(goal) and (not thread_results or search_messages_error is not None):
            fallback_threads = await self._fallback_recent_thread_results(
                goal=goal,
                mailbox_id=mailbox_id,
                limit=self.config.max_search_results,
            )
            if fallback_threads:
                seen_ids = {self._safe_text(item.get("id")) for item in thread_results}
                for item in fallback_threads:
                    item_id = self._safe_text(item.get("id"))
                    if item_id and item_id in seen_ids:
                        continue
                    thread_results.append(item)
                    if item_id:
                        seen_ids.add(item_id)
                thread_results = thread_results[: self.config.max_search_results]
        return thread_results + message_results

    def _is_read_like_goal(self, goal: str) -> bool:
        lowered = self._safe_text(goal).casefold()
        if not lowered:
            return False
        read_markers = (
            "check the inbox",
            "check inbox",
            "read the inbox",
            "read inbox",
            "search inbox",
            "search my inbox",
            "look in the inbox",
            "look in my inbox",
            "most recent emails",
            "most recent email",
            "latest emails",
            "latest email",
            "show me what messages",
            "show me my emails",
            "read and display",
            "reply from",
            "read the reply",
            "check my reply",
            "i replied",
            "my reply",
            "tell me what",
            "last email",
            "latest email",
            "last message",
            "latest message",
            "what was last email",
            "what was the last email",
            "what was my last email",
            "what was the latest email",
            "what was the latest message",
            "what did i send",
            "did i email",
            "did i send",
            "you got from me",
            "from me",
        )
        read_verbs = ("check ", "read ", "search ", "find ", "look for", "show me", "tell me")
        return any(marker in lowered for marker in read_markers) or " inbox" in lowered or lowered.startswith(read_verbs)

    def _rewrite_search_query_for_sender_reference(self, *, query: str, goal: str) -> str:
        base_query = self._safe_text(query) or self._safe_text(goal)
        if not base_query:
            return ""
        lowered = self._safe_text(goal).casefold()
        if any(marker in lowered for marker in ("from me", "my last email", "what did i send", "did i email", "did i send")):
            trusted = sorted(self._trusted_sender_set)
            if trusted:
                if all(item.casefold() not in base_query.casefold() for item in trusted):
                    return f"{base_query} {' '.join(trusted[:3])}".strip()
        return base_query

    async def _fallback_recent_thread_results(
        self,
        *,
        goal: str,
        mailbox_id: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        try:
            recent_threads = await self.mail_client.list_threads(
                mailbox_id=mailbox_id,
                per_page=max(limit * 3, 10),
            )
        except CosmicMailClientError as exc:
            logger.warning(
                "email_agent.list_threads_fallback_failed mailbox_id=%s status=%s error=%s",
                mailbox_id,
                exc.status_code,
                exc.message,
            )
            return []

        email_mentions = {
            self._safe_text(match.group(1)).casefold()
            for match in re.finditer(r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})", goal)
        }
        tokens = [
            token
            for token in re.findall(r"[A-Za-z0-9@._+\-]{3,}", self._safe_text(goal).casefold())
            if token not in {"the", "and", "that", "reply", "email", "inbox", "from", "check", "read", "what", "said"}
        ]
        wants_reply = "reply" in self._safe_text(goal).casefold() or "replied" in self._safe_text(goal).casefold()

        scored: list[tuple[int, dict[str, Any]]] = []
        for item in recent_threads:
            if not isinstance(item, dict):
                continue
            subject = self._safe_text(item.get("subject"))
            snippet = self._safe_text(item.get("snippet") or item.get("body_preview"))
            haystack = f"{subject}\n{snippet}".casefold()
            score = 0
            if wants_reply and subject.casefold().startswith("re:"):
                score += 40
            for email in email_mentions:
                if email and email in haystack:
                    score += 60
            for token in tokens:
                if token and token in haystack:
                    score += 8
            recency = self._safe_text(item.get("last_message_at") or item.get("updated_at"))
            if score <= 0 and not wants_reply and not tokens and not email_mentions:
                continue
            scored.append(
                (
                    score,
                    {
                        "kind": "thread",
                        "id": self._safe_text(item.get("id")),
                        "subject": subject,
                        "snippet": snippet,
                        "last_message_at": recency,
                    },
                )
            )

        scored.sort(key=lambda pair: (pair[0], self._safe_text(pair[1].get("last_message_at"))), reverse=True)
        results = [item for _, item in scored[:limit]]
        if results:
            return results

        if wants_reply:
            fallback_results: list[dict[str, Any]] = []
            for item in recent_threads[:limit]:
                if not isinstance(item, dict):
                    continue
                fallback_results.append(
                    {
                        "kind": "thread",
                        "id": self._safe_text(item.get("id")),
                        "subject": self._safe_text(item.get("subject")),
                        "snippet": self._safe_text(item.get("snippet") or item.get("body_preview")),
                        "last_message_at": self._safe_text(item.get("last_message_at") or item.get("updated_at")),
                    }
                )
            return fallback_results
        return []

    async def _summarize_search_results(self, *, task: TaskEnvelope, goal: str, search_results: list[dict[str, Any]]) -> str:
        if not search_results:
            return "No matching email threads or messages were found."
        if self._is_read_like_goal(goal):
            top_result = search_results[0] if search_results else {}
            if isinstance(top_result, dict):
                snippet = self._clean_reply_snippet(self._safe_text(top_result.get("snippet")))
                if snippet:
                    return f"The latest reply says: {snippet}"
        summary = await invoke_email_internal_llm(
            cfg=self.config,
            http_client=self._http_client,
            system_content=self._build_system_prompt(),
            user_message=(
                "Summarize these email search results for Opus.\n"
                "Be concise and point out the most relevant matches.\n\n"
                f"Goal: {goal}\n\nResults:\n{json.dumps(search_results[:10], ensure_ascii=False, indent=2)}"
            ),
            task_id=task.task_id,
            session_id=task.session_id,
            request_id=self._optional_text(task.input, "request_id"),
            source=task.source,
            source_id=task.source_id,
            channel=task.channel,
            operation="email.internal_llm.search_summary",
            max_output_chars=6000,
            temperature=0.2,
        )
        return summary or f"Found {len(search_results)} matching email search results."

    def _clean_reply_snippet(self, snippet: str) -> str:
        text = self._safe_text(snippet)
        if not text:
            return ""
        text = re.sub(r"\s+", " ", text).strip()
        text = re.split(r"\bOn\s+[A-Z][a-z]{2},.+?\bwrote:\s*", text, maxsplit=1)[0].strip()
        text = re.split(r"\bFrom:\s*", text, maxsplit=1)[0].strip()
        text = re.split(r"\s+>+\s*", text, maxsplit=1)[0].strip()
        if len(text) > 320:
            text = text[:317].rstrip() + "..."
        return text

    async def _resolve_mailbox(
        self,
        *,
        mailbox_address: str | None,
        mailbox_id: str | None,
        required: bool = True,
    ) -> dict[str, Any]:
        await self._refresh_mail_client_from_store()
        normalized_id = self._safe_text(mailbox_id)
        normalized_address = self._safe_text(mailbox_address) or self.config.primary_mailbox_address
        if not normalized_id and not normalized_address and not required:
            return {}
        if not self.config.cosmic_mail_base_url or not self.config.cosmic_mail_api_token:
            raise EmailAgentError(
                code="AUTH_ERROR",
                message="Cosmic Mail credentials are not configured for the email agent.",
                retryable=False,
                next_action="escalate",
            )
        if not normalized_id and not normalized_address:
            try:
                mailboxes = await self.mail_client.list_mailboxes()
            except CosmicMailClientError as exc:
                raise EmailAgentError(
                    code="NETWORK_ERROR" if exc.status_code is None or exc.status_code >= 500 else "AUTH_ERROR",
                    message=exc.message,
                    retryable=exc.status_code is None or exc.status_code >= 500,
                    next_action="retry" if exc.status_code is None or exc.status_code >= 500 else "escalate",
                ) from exc
            active_mailboxes = [
                mailbox
                for mailbox in mailboxes
                if self._safe_text(mailbox.get("status")).lower() == "active"
            ]
            fallback_mailbox = (active_mailboxes or mailboxes)[0] if (active_mailboxes or mailboxes) else None
            normalized_address = self._safe_text(fallback_mailbox.get("address")) if isinstance(fallback_mailbox, dict) else ""
            if not normalized_address:
                raise EmailAgentError(
                    code="INVALID_INPUT",
                    message="No mailbox_id or mailbox_address was provided, and no default Agent Email mailbox is available.",
                    retryable=False,
                    next_action="configure_mailbox",
                )
        try:
            return await self.mail_client.resolve_mailbox(
                mailbox_id=normalized_id or None,
                mailbox_address=normalized_address or None,
            )
        except ValueError as exc:
            raise EmailAgentError(
                code="INVALID_INPUT",
                message=str(exc),
                retryable=False,
                next_action="configure_mailbox",
            ) from exc
        except CosmicMailClientError as exc:
            raise EmailAgentError(
                code="INVALID_INPUT" if exc.status_code == 404 else "NETWORK_ERROR",
                message=exc.message,
                retryable=exc.status_code is None or exc.status_code >= 500,
                next_action="retry" if exc.status_code is None or exc.status_code >= 500 else "escalate",
            ) from exc

    def _normalize_mail_delivery_result(
        self,
        payload: dict[str, Any] | None,
        *,
        draft_id: str | None = None,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        body = payload if isinstance(payload, dict) else {}
        queued_for_approval = bool(body.get("queued_for_approval"))
        approval_id = self._safe_text(body.get("approval_id")) or None
        delivery_status = "queued_for_approval" if queued_for_approval or approval_id else "sent"
        message_payload = body.get("message") if isinstance(body.get("message"), dict) else {}
        draft_payload = body.get("draft") if isinstance(body.get("draft"), dict) else {}
        thread_payload = body.get("thread") if isinstance(body.get("thread"), dict) else {}
        resolved_message_id = (
            self._safe_text(message_payload.get("id"))
            or self._safe_text(body.get("message_id"))
            or self._safe_text(body.get("sent_message_id"))
            or self._safe_text(body.get("id"))
            or None
        )
        resolved_thread_id = (
            self._safe_text(thread_payload.get("id"))
            or self._safe_text(body.get("thread_id"))
            or self._safe_text(message_payload.get("thread_id"))
            or self._safe_text(draft_payload.get("thread_id"))
            or self._safe_text(thread_id)
            or None
        )
        resolved_draft_id = self._safe_text(draft_payload.get("id")) or self._safe_text(body.get("draft_id")) or self._safe_text(draft_id) or None
        return {
            "delivery_status": delivery_status,
            "queued_for_approval": queued_for_approval,
            "approval_id": approval_id,
            "sent": delivery_status == "sent",
            "acted": delivery_status in {"sent", "queued_for_approval"},
            "message_id": resolved_message_id,
            "thread_id": resolved_thread_id,
            "draft_id": resolved_draft_id,
        }

    def _mail_delivery_note(self, delivery: dict[str, Any] | None) -> str:
        if not isinstance(delivery, dict):
            return ""
        status = self._safe_text(delivery.get("delivery_status"))
        if status == "sent":
            return "Email sent."
        if status == "queued_for_approval":
            return "Email queued for approval."
        return ""

    async def _ensure_mail_client_ready(self) -> None:
        await self._refresh_mail_client_from_store()
        if not self.config.cosmic_mail_base_url or not self.config.cosmic_mail_api_token:
            raise EmailAgentError(
                code="AUTH_ERROR",
                message="Cosmic Mail credentials are not configured for the email agent.",
                retryable=False,
                next_action="configure_auth",
            )

    async def _refresh_mail_client_from_store(self) -> None:
        stored = self.integration_store.get_primary()
        if agent_email_integration_is_disabled(stored):
            next_base_url = ""
            next_api_token = ""
            next_mailbox = ""
            next_trusted_senders: tuple[str, ...] = ()
        elif agent_email_integration_is_configured(stored):
            next_base_url = str(stored.base_url or "").strip()
            next_api_token = str(stored.api_token or "").strip()
            next_mailbox = str(stored.primary_mailbox_address or "").strip()
            next_trusted_senders = stored.trusted_senders
        else:
            next_base_url = self._env_cosmic_mail_base_url
            next_api_token = self._env_cosmic_mail_api_token
            next_mailbox = self._env_primary_mailbox_address
            next_trusted_senders = stored.trusted_senders if stored is not None else ()

        current_base_url = str(self.mail_client.base_url or "").strip()
        current_api_token = str(getattr(self.mail_client, "api_token", "") or "").strip()
        self.config.primary_mailbox_address = next_mailbox
        self._trusted_sender_set = {item.casefold() for item in next_trusted_senders if item}
        if current_base_url == next_base_url and current_api_token == next_api_token:
            self.config.cosmic_mail_base_url = next_base_url
            self.config.cosmic_mail_api_token = next_api_token
            return

        await self.mail_client.aclose()
        self.config.cosmic_mail_base_url = next_base_url
        self.config.cosmic_mail_api_token = next_api_token
        self.mail_client = CosmicMailClient(
            base_url=self.config.cosmic_mail_base_url,
            api_token=self.config.cosmic_mail_api_token,
            timeout_sec=self.config.cosmic_mail_timeout_sec,
            client=self._http_client,
        )

    def _is_trusted_sender(self, from_address: str | None) -> bool:
        sender = self._safe_text(from_address).casefold()
        if not sender:
            return False
        return sender in self._trusted_sender_set

    def _record_session_run(
        self,
        *,
        task: TaskEnvelope,
        intent: str,
        mailbox_address: str | None,
        thread_id: str | None,
        message_id: str | None,
        summary: dict[str, Any],
    ) -> None:
        payload = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
        created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with connect_sync(self.session_db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO email_session_runs (
                    task_id, session_id, intent, mailbox_address, thread_id, message_id, summary_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    task.session_id,
                    intent,
                    mailbox_address,
                    thread_id,
                    message_id,
                    payload,
                    created_at,
                ),
            )
            conn.commit()

    def _list_instructions(self, *, mailbox_address: str | None) -> list[dict[str, Any]]:
        with connect_sync(self.session_db_path) as conn:
            if mailbox_address:
                rows = conn.execute(
                    """
                    SELECT instruction_id, mailbox_address, label, enabled,
                           match_from_address, match_subject_contains, match_body_contains,
                           raw_user_instruction, behavior_json,
                           last_triggered_at, completed_at, last_action_thread_id, last_action_message_id,
                           created_at, updated_at
                    FROM email_instructions
                    WHERE mailbox_address = ? OR mailbox_address IS NULL
                    ORDER BY updated_at DESC
                    """,
                    (mailbox_address,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT instruction_id, mailbox_address, label, enabled,
                           match_from_address, match_subject_contains, match_body_contains,
                           raw_user_instruction, behavior_json,
                           last_triggered_at, completed_at, last_action_thread_id, last_action_message_id,
                           created_at, updated_at
                    FROM email_instructions
                    ORDER BY updated_at DESC
                    """
                ).fetchall()
        return [self._instruction_row_to_dict(row) for row in rows]

    async def _resolve_matched_instructions(
        self,
        *,
        task: TaskEnvelope,
        mailbox_address: str | None,
        from_address: str | None,
        subject: str | None,
        body: str | None,
        context: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], str | None]:
        instructions = []
        for instruction in self._list_instructions(mailbox_address=mailbox_address):
            if not instruction.get("enabled"):
                continue
            if instruction.get("mailbox_address") and mailbox_address and instruction["mailbox_address"] != mailbox_address:
                continue
            instructions.append(instruction)
        if not instructions:
            return [], None

        llm_ids, llm_reason = await self._match_instructions_with_llm(
            task=task,
            instructions=instructions,
            from_address=from_address,
            subject=subject,
            body=body,
            context=context,
        )
        if llm_ids:
            matched = [item for item in instructions if self._safe_text(item.get("instruction_id")) in llm_ids]
            if matched:
                return matched, llm_reason

        fallback = self._match_instructions_deterministic(
            instructions=instructions,
            from_address=from_address,
            subject=subject,
            body=body,
        )
        return fallback, ("Matched using deterministic instruction fields." if fallback else llm_reason)

    def _match_instructions_deterministic(
        self,
        *,
        instructions: list[dict[str, Any]],
        from_address: str | None,
        subject: str | None,
        body: str | None,
    ) -> list[dict[str, Any]]:
        matched: list[dict[str, Any]] = []
        for instruction in instructions:
            match = instruction.get("match") if isinstance(instruction.get("match"), dict) else {}
            if not any(self._safe_text(match.get(key)) for key in ("from_address", "subject_contains", "body_contains")):
                continue
            if self._instruction_matches(
                match=match,
                from_address=from_address,
                subject=subject,
                body=body,
            ):
                matched.append(instruction)
        return matched

    def _instruction_matches(
        self,
        *,
        match: dict[str, Any],
        from_address: str | None,
        subject: str | None,
        body: str | None,
    ) -> bool:
        sender = (from_address or "").strip().casefold()
        subj = (subject or "").strip().casefold()
        text = (body or "").strip().casefold()
        expected_sender = self._safe_text(match.get("from_address")).casefold()
        expected_subject = self._safe_text(match.get("subject_contains")).casefold()
        expected_body = self._safe_text(match.get("body_contains")).casefold()
        if expected_sender and sender != expected_sender:
            return False
        if expected_subject and expected_subject not in subj:
            return False
        if expected_body and expected_body not in text:
            return False
        return True

    async def _match_instructions_with_llm(
        self,
        *,
        task: TaskEnvelope,
        instructions: list[dict[str, Any]],
        from_address: str | None,
        subject: str | None,
        body: str | None,
        context: dict[str, Any],
    ) -> tuple[list[str], str | None]:
        if not instructions:
            return [], None
        instruction_payload = [self._instruction_prompt_payload(item) for item in instructions[:48]]
        thread_messages = context.get("messages") if isinstance(context.get("messages"), list) else []
        recent_transcript = [
            {
                "from_address": self._safe_text(item.get("from_address")),
                "subject": self._safe_text(item.get("subject")),
                "text_body": self._safe_text(item.get("text_body"))[:1200],
            }
            for item in thread_messages[-4:]
            if isinstance(item, dict)
        ]
        payload = await invoke_email_internal_llm_json(
            cfg=self.config,
            http_client=self._http_client,
            system_content=self._build_system_prompt(),
            user_message=(
                "Match this inbound email against the provided standing instructions.\n"
                "Return JSON with keys: matched_instruction_ids (array of ids), rationale (string), ambiguous (boolean).\n"
                "Only use ids from the provided instruction list.\n"
                "Prefer precision over recall.\n"
                "If nothing matches, return an empty matched_instruction_ids array.\n\n"
                f"Inbound from: {from_address or '(unknown)'}\n"
                f"Subject: {subject or '(no subject)'}\n"
                f"Latest body:\n{(body or '')[:6000]}\n\n"
                f"Recent thread context:\n{json.dumps(recent_transcript, ensure_ascii=False)}\n\n"
                f"Standing instructions:\n{json.dumps(instruction_payload, ensure_ascii=False)}"
            ),
            task_id=task.task_id,
            session_id=task.session_id,
            request_id=self._optional_text(task.input, "request_id"),
            source=task.source,
            source_id=task.source_id,
            channel=task.channel,
            operation="email.internal_llm.match_instructions",
        ) or {}
        matched_ids = self._normalize_text_list(payload.get("matched_instruction_ids"))
        rationale = self._safe_text(payload.get("rationale")) or None
        if not matched_ids:
            return [], rationale
        allowed = {self._safe_text(item.get("instruction_id")) for item in instructions}
        normalized = [item for item in matched_ids if item in allowed]
        return normalized, rationale

    async def _expand_instruction_from_text(
        self,
        *,
        task: TaskEnvelope,
        raw_user_instruction: str,
    ) -> dict[str, Any]:
        if not raw_user_instruction:
            return {}
        payload = await invoke_email_internal_llm_json(
            cfg=self.config,
            http_client=self._http_client,
            system_content=self._build_system_prompt(),
            user_message=(
                "Convert this standing email instruction into a compact JSON policy.\n"
                "Return JSON with keys: label, match, behavior.\n"
                "behavior must contain mode and completion_mode.\n"
                "Allowed mode values: notify_only, auto_reply.\n"
                "Allowed completion_mode values: perpetual, one_shot.\n"
                "Only include reply_template when mode is auto_reply.\n"
                "Use match fields only when they are explicit and helpful: from_address, subject_contains, body_contains.\n"
                "Do not invent exact email addresses if the instruction only names a person informally.\n\n"
                f"Instruction:\n{raw_user_instruction}"
            ),
            task_id=task.task_id,
            session_id=task.session_id,
            request_id=self._optional_text(task.input, "request_id"),
            source=task.source,
            source_id=task.source_id,
            channel=task.channel,
            operation="email.internal_llm.parse_instruction",
        ) or {}
        match = self._coerce_instruction_match(payload.get("match"))
        behavior = self._normalize_instruction_behavior(payload.get("behavior") if isinstance(payload.get("behavior"), dict) else {})
        label = self._safe_text(payload.get("label")) or self._build_instruction_label(raw_user_instruction)
        return {
            "label": label,
            "match": match,
            "behavior": behavior,
        }

    def _upsert_instruction(
        self,
        *,
        instruction_id: str,
        mailbox_address: str | None,
        label: str,
        match: dict[str, Any],
        behavior: dict[str, Any],
        raw_user_instruction: str | None,
        enabled: bool,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with connect_sync(self.session_db_path) as conn:
            conn.execute(
                """
                INSERT INTO email_instructions (
                    instruction_id, mailbox_address, label, enabled,
                    match_from_address, match_subject_contains, match_body_contains,
                    raw_user_instruction, behavior_json,
                    created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?,
                    ?, ?
                )
                ON CONFLICT(instruction_id) DO UPDATE SET
                    mailbox_address = excluded.mailbox_address,
                    label = excluded.label,
                    enabled = excluded.enabled,
                    match_from_address = excluded.match_from_address,
                    match_subject_contains = excluded.match_subject_contains,
                    match_body_contains = excluded.match_body_contains,
                    raw_user_instruction = excluded.raw_user_instruction,
                    behavior_json = excluded.behavior_json,
                    updated_at = excluded.updated_at
                """,
                (
                    instruction_id,
                    mailbox_address,
                    label,
                    1 if enabled else 0,
                    self._safe_text(match.get("from_address")) or None,
                    self._safe_text(match.get("subject_contains")) or None,
                    self._safe_text(match.get("body_contains")) or None,
                    self._safe_text(raw_user_instruction) or None,
                    json.dumps(behavior, ensure_ascii=False, separators=(",", ":")),
                    now,
                    now,
                ),
            )
            conn.commit()

    def _set_instruction_enabled(self, *, instruction_id: str, enabled: bool) -> None:
        completed_at = None if enabled else None
        with connect_sync(self.session_db_path) as conn:
            cursor = conn.execute(
                "UPDATE email_instructions SET enabled = ?, completed_at = ?, updated_at = ? WHERE instruction_id = ?",
                (
                    1 if enabled else 0,
                    completed_at,
                    datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    instruction_id,
                ),
            )
            conn.commit()
        if cursor.rowcount == 0:
            raise EmailAgentError(
                code="INVALID_INPUT",
                message=f"Unknown email instruction: {instruction_id}",
                retryable=False,
                next_action="escalate",
            )

    def _delete_instruction(self, *, instruction_id: str) -> None:
        with connect_sync(self.session_db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM email_instructions WHERE instruction_id = ?",
                (instruction_id,),
            )
            conn.commit()
        if cursor.rowcount == 0:
            raise EmailAgentError(
                code="INVALID_INPUT",
                message=f"Unknown email instruction: {instruction_id}",
                retryable=False,
                next_action="escalate",
            )

    def _instruction_row_to_dict(self, row: tuple[Any, ...]) -> dict[str, Any]:
        try:
            behavior = json.loads(row[8]) if row[8] else {}
        except Exception:
            behavior = {}
        return {
            "instruction_id": row[0],
            "mailbox_address": row[1],
            "label": row[2],
            "enabled": bool(row[3]),
            "match": {
                "from_address": row[4],
                "subject_contains": row[5],
                "body_contains": row[6],
            },
            "raw_user_instruction": row[7],
            "behavior": behavior if isinstance(behavior, dict) else {},
            "last_triggered_at": row[9],
            "completed_at": row[10],
            "last_action_thread_id": row[11],
            "last_action_message_id": row[12],
            "created_at": row[13],
            "updated_at": row[14],
        }

    def _instruction_mode(self, instruction: dict[str, Any]) -> str:
        behavior = instruction.get("behavior") if isinstance(instruction.get("behavior"), dict) else {}
        mode = self._safe_text(behavior.get("mode")).lower()
        return mode or "notify_only"

    def _instruction_completion_mode(self, instruction: dict[str, Any]) -> str:
        behavior = instruction.get("behavior") if isinstance(instruction.get("behavior"), dict) else {}
        mode = self._safe_text(behavior.get("completion_mode")).lower()
        if mode in {"one_shot", "perpetual"}:
            return mode
        return "perpetual"

    def _coerce_instruction_match(self, raw: Any) -> dict[str, Any]:
        match = raw if isinstance(raw, dict) else {}
        return {
            "from_address": self._safe_text(match.get("from_address")) or None,
            "subject_contains": self._safe_text(match.get("subject_contains")) or None,
            "body_contains": self._safe_text(match.get("body_contains")) or None,
        }

    def _normalize_instruction_behavior(self, behavior: dict[str, Any] | None) -> dict[str, Any]:
        raw = behavior if isinstance(behavior, dict) else {}
        mode = self._safe_text(raw.get("mode")).lower()
        if mode not in {"notify_only", "auto_reply"}:
            mode = "notify_only"
        completion_mode = self._safe_text(raw.get("completion_mode")).lower()
        if completion_mode not in {"perpetual", "one_shot"}:
            completion_mode = "perpetual"
        normalized = {
            "mode": mode,
            "completion_mode": completion_mode,
        }
        reply_template = self._safe_text(raw.get("reply_template")) or None
        if reply_template:
            normalized["reply_template"] = reply_template
        return normalized

    def _merge_instruction_match(self, inferred: dict[str, Any], explicit: dict[str, Any]) -> dict[str, Any]:
        return {
            "from_address": self._safe_text(explicit.get("from_address")) or self._safe_text(inferred.get("from_address")) or None,
            "subject_contains": self._safe_text(explicit.get("subject_contains")) or self._safe_text(inferred.get("subject_contains")) or None,
            "body_contains": self._safe_text(explicit.get("body_contains")) or self._safe_text(inferred.get("body_contains")) or None,
        }

    def _merge_instruction_behavior(self, inferred: dict[str, Any], explicit: dict[str, Any]) -> dict[str, Any]:
        merged = dict(inferred or {})
        for key, value in (explicit or {}).items():
            if value not in (None, "", []):
                merged[key] = value
        return merged

    def _build_instruction_label(self, raw_user_instruction: str | None) -> str | None:
        text = self._safe_text(raw_user_instruction)
        if not text:
            return None
        compact = re.sub(r"\s+", " ", text).strip()
        return compact[:120]

    def _instruction_prompt_payload(self, instruction: dict[str, Any]) -> dict[str, Any]:
        return {
            "instruction_id": self._safe_text(instruction.get("instruction_id")),
            "label": self._safe_text(instruction.get("label")),
            "raw_user_instruction": self._safe_text(instruction.get("raw_user_instruction")) or None,
            "match": instruction.get("match") if isinstance(instruction.get("match"), dict) else {},
            "behavior": instruction.get("behavior") if isinstance(instruction.get("behavior"), dict) else {},
        }

    def _normalize_text_list(self, values: Any) -> list[str]:
        if not isinstance(values, list):
            return []
        seen: set[str] = set()
        items: list[str] = []
        for value in values:
            text = self._safe_text(value)
            normalized = text.casefold()
            if not text or normalized in seen:
                continue
            seen.add(normalized)
            items.append(text)
        return items

    def _mark_instructions_triggered(self, *, instruction_ids: list[str]) -> None:
        normalized_ids = self._normalize_text_list(instruction_ids)
        if not normalized_ids:
            return
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with connect_sync(self.session_db_path) as conn:
            conn.executemany(
                "UPDATE email_instructions SET last_triggered_at = ?, updated_at = ? WHERE instruction_id = ?",
                [(now, now, instruction_id) for instruction_id in normalized_ids],
            )
            conn.commit()

    def _record_instruction_delivery(
        self,
        *,
        instruction_ids: list[str],
        thread_id: str | None,
        message_id: str | None,
    ) -> None:
        normalized_ids = self._normalize_text_list(instruction_ids)
        if not normalized_ids:
            return
        instructions = {item["instruction_id"]: item for item in self._list_instructions(mailbox_address=None)}
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with connect_sync(self.session_db_path) as conn:
            for instruction_id in normalized_ids:
                instruction = instructions.get(instruction_id)
                if not instruction:
                    continue
                completion_mode = self._instruction_completion_mode(instruction)
                complete_now = completion_mode == "one_shot"
                conn.execute(
                    """
                    UPDATE email_instructions
                    SET last_triggered_at = ?,
                        completed_at = CASE WHEN ? THEN ? ELSE completed_at END,
                        enabled = CASE WHEN ? THEN 0 ELSE enabled END,
                        last_action_thread_id = COALESCE(?, last_action_thread_id),
                        last_action_message_id = COALESCE(?, last_action_message_id),
                        updated_at = ?
                    WHERE instruction_id = ?
                    """,
                    (
                        now,
                        1 if complete_now else 0,
                        now,
                        1 if complete_now else 0,
                        thread_id,
                        message_id,
                        now,
                        instruction_id,
                    ),
                )
            conn.commit()

    def _normalize_message_record(self, message: dict[str, Any]) -> dict[str, Any]:
        from_contacts = message.get("from_recipients") if isinstance(message.get("from_recipients"), list) else []
        from_address = self._safe_text(message.get("from_address")) or None
        if from_contacts and isinstance(from_contacts[0], dict):
            from_address = self._safe_text(from_contacts[0].get("email") or from_contacts[0].get("address"))
        return {
            "id": self._safe_text(message.get("id")),
            "thread_id": self._safe_text(message.get("thread_id")),
            "subject": self._safe_text(message.get("subject")),
            "from_address": from_address,
            "text_body": self._safe_text(
                message.get("text_body")
                or message.get("body_text")
                or message.get("snippet")
                or message.get("body_preview")
                or message.get("body")
            ),
            "direction": self._safe_text(message.get("direction")),
            "sent_at": self._safe_text(message.get("sent_at") or message.get("created_at")),
            "internet_message_id": self._safe_text(message.get("internet_message_id")),
            "from_name": self._safe_text(message.get("from_name")) or None,
            "to_recipients": self._normalize_recipient_list(message.get("to_recipients")),
            "reply_to_recipients": self._normalize_recipient_list(message.get("reply_to_recipients")),
        }

    def _thread_sender(self, context: dict[str, Any]) -> str | None:
        latest = context.get("latest_message") if isinstance(context.get("latest_message"), dict) else {}
        return self._safe_text(latest.get("from_address")) or None

    def _reply_subject(self, context: dict[str, Any]) -> str:
        subject = self._safe_text(context.get("subject")) or "email thread"
        if subject.casefold().startswith("re:"):
            return subject
        return f"Re: {subject}"

    def _reply_to_message_id(self, context: dict[str, Any]) -> str | None:
        latest = context.get("latest_message") if isinstance(context.get("latest_message"), dict) else {}
        return self._safe_text(latest.get("internet_message_id")) or None

    def _default_reply_recipients(self, context: dict[str, Any], mailbox: dict[str, Any]) -> list[dict[str, Any]]:
        latest = context.get("latest_message") if isinstance(context.get("latest_message"), dict) else {}
        from_address = self._safe_text(latest.get("from_address"))
        from_name = self._safe_text(latest.get("from_name")) or None
        mailbox_address = self._safe_text(mailbox.get("address"))
        if from_address and (not mailbox_address or from_address.casefold() != mailbox_address.casefold()):
            return [{"email": from_address, "name": from_name}]
        reply_to_recipients = latest.get("reply_to_recipients") if isinstance(latest.get("reply_to_recipients"), list) else []
        if reply_to_recipients:
            return self._normalize_recipient_list(reply_to_recipients)
        to_recipients = latest.get("to_recipients") if isinstance(latest.get("to_recipients"), list) else []
        if to_recipients:
            return self._normalize_recipient_list(to_recipients)
        if from_address:
            return [{"email": from_address, "name": from_name}]
        return []

    def _build_system_prompt(self) -> str:
        parts = []
        for relative in ("prompts/system.md", "prompts/policies.md", "skills/SKILLS.md"):
            path = self.agent_root / relative
            if path.exists():
                parts.append(path.read_text(encoding="utf-8").strip())
        return "\n\n".join(part for part in parts if part)

    def _task_artifact_dir(self, task_id: str) -> Path:
        return self.artifacts_root / task_id / "email_agent"

    def _write_json_artifact(
        self,
        *,
        task_id: str,
        name: str,
        payload: Any,
        mime: str,
        kind: str,
        audience: str,
    ) -> ArtifactManifest:
        target_dir = self._task_artifact_dir(task_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / name
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return self._artifact_manifest(task_id=task_id, path=path, mime=mime, kind=kind, audience=audience)

    def _artifact_manifest(
        self,
        *,
        task_id: str,
        path: Path,
        mime: str,
        kind: str,
        audience: str,
    ) -> ArtifactManifest:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        relative_path = self._logical_artifact_path(path)
        return ArtifactManifest(
            artifact_id=f"art_{uuid4().hex[:12]}",
            task_id=task_id,
            mime=mime,
            sha256=digest,
            path=relative_path,
            created_by_agent=self.agent_id,
            kind=kind,
            audience=audience,
        )

    def _logical_artifact_path(self, path: Path) -> str:
        resolved = path.resolve()
        try:
            relative_to_artifacts = resolved.relative_to(self.artifacts_root.resolve())
            return (Path("runs") / "artifacts" / relative_to_artifacts).as_posix()
        except ValueError:
            return resolved.relative_to(BACKEND_ROOT.resolve()).as_posix()

    def _resolve_artifact_path(self, artifact: dict[str, Any]) -> Path | None:
        raw_path = self._safe_text(artifact.get("path"))
        if not raw_path:
            return None
        path = Path(raw_path)
        if path.is_absolute():
            return path
        normalized = raw_path.replace("\\", "/").strip("/")
        if normalized.startswith("runs/artifacts/"):
            rel = normalized.split("runs/artifacts/", 1)[1]
            return (self.artifacts_root / rel).resolve()
        return (BACKEND_ROOT / path).resolve()

    def _normalize_recipient_list(self, raw: Any) -> list[dict[str, Any]]:
        recipients: list[dict[str, Any]] = []
        if not isinstance(raw, list):
            return recipients
        for item in raw:
            if not isinstance(item, dict):
                continue
            email = self._safe_text(item.get("email"))
            if not email:
                continue
            recipients.append({"email": email, "name": self._safe_text(item.get("name")) or None})
        return recipients

    def _dedupe_recipient_groups(
        self,
        to_recipients: list[dict[str, Any]],
        cc_recipients: list[dict[str, Any]],
        bcc_recipients: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        seen: set[str] = set()

        def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            normalized_items: list[dict[str, Any]] = []
            for item in items:
                email = self._safe_text(item.get("email")).casefold()
                if not email or email in seen:
                    continue
                seen.add(email)
                normalized_items.append(
                    {
                        "email": self._safe_text(item.get("email")),
                        "name": self._safe_text(item.get("name")) or None,
                    }
                )
            return normalized_items

        return (
            _dedupe(to_recipients),
            _dedupe(cc_recipients),
            _dedupe(bcc_recipients),
        )

    def _infer_recipient_hints(self, text: str) -> dict[str, list[dict[str, Any]]]:
        normalized = self._safe_text(text)
        if not normalized:
            return {"to": [], "cc": [], "bcc": [], "all": []}

        email_pattern = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)
        to_recipients: list[dict[str, Any]] = []
        cc_recipients: list[dict[str, Any]] = []
        bcc_recipients: list[dict[str, Any]] = []
        all_recipients: list[dict[str, Any]] = []

        def _recipients_from_segment(segment: str) -> list[dict[str, Any]]:
            items: list[dict[str, Any]] = []
            for segment_match in email_pattern.finditer(segment):
                email = self._safe_text(segment_match.group(0))
                if email:
                    items.append({"email": email, "name": None})
            return items

        def _last_keyword_position(prefix: str, patterns: tuple[str, ...]) -> int:
            last = -1
            for pattern in patterns:
                for match in re.finditer(pattern, prefix, re.IGNORECASE):
                    last = max(last, match.start())
            return last

        stop_markers = r"(?=(?:\bbcc\b|\bcc\b|\bwith\s+subject\b|\bsubject\b|\bbody\b|\bthe\s+following\s+content\b|\bsomething\s+like\b|$))"
        segment_patterns = (
            ("bcc", rf"\bbcc\b\s*:?\s*(?P<segment>.+?){stop_markers}"),
            ("cc", rf"\bcc\b\s*:?\s*(?P<segment>.+?){stop_markers}"),
            (
                "to",
                rf"(?:\bsend(?:\s+an?\s+email)?\s+to\b|\bemail\s+to\b|\bwrite(?:\s+an?\s+email)?\s+to\b|\bto\b)\s*(?P<segment>.+?){stop_markers}",
            ),
        )
        classified_emails: set[str] = set()
        for label, pattern in segment_patterns:
            for segment_match in re.finditer(pattern, normalized, re.IGNORECASE | re.DOTALL):
                recipients = _recipients_from_segment(self._safe_text(segment_match.group("segment")))
                for recipient in recipients:
                    email_key = self._safe_text(recipient.get("email")).casefold()
                    if not email_key or email_key in classified_emails:
                        continue
                    classified_emails.add(email_key)
                    if label == "bcc":
                        bcc_recipients.append(recipient)
                    elif label == "cc":
                        cc_recipients.append(recipient)
                    else:
                        to_recipients.append(recipient)

        for match in email_pattern.finditer(normalized):
            email = self._safe_text(match.group(0))
            if not email:
                continue
            recipient = {"email": email, "name": None}
            all_recipients.append(recipient)
            email_key = email.casefold()
            if email_key in classified_emails:
                continue
            prefix = normalized[max(0, match.start() - 96) : match.start()]
            clause = re.split(r"[\n.;]", prefix)[-1]
            bcc_pos = _last_keyword_position(clause, (r"\bbcc\b", r"\bblind carbon copy\b"))
            cc_pos = _last_keyword_position(clause, (r"\bcc\b", r"\bcarbon copy\b"))
            to_pos = _last_keyword_position(
                clause,
                (
                    r"\bsend(?:\s+an?\s+email)?\s+to\b",
                    r"\bemail\s+to\b",
                    r"\bwrite(?:\s+an?\s+email)?\s+to\b",
                    r"\bto\b",
                ),
            )
            if bcc_pos >= cc_pos and bcc_pos >= to_pos and bcc_pos >= 0:
                bcc_recipients.append(recipient)
            elif cc_pos >= bcc_pos and cc_pos >= to_pos and cc_pos >= 0:
                cc_recipients.append(recipient)
            else:
                to_recipients.append(recipient)

        to_recipients, cc_recipients, bcc_recipients = self._dedupe_recipient_groups(
            to_recipients,
            cc_recipients,
            bcc_recipients,
        )
        all_recipients, _, _ = self._dedupe_recipient_groups(all_recipients, [], [])
        return {
            "to": to_recipients,
            "cc": cc_recipients,
            "bcc": bcc_recipients,
            "all": all_recipients,
        }

    def _format_recipients_for_prompt(self, recipients: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for item in recipients:
            email = self._safe_text(item.get("email"))
            if not email:
                continue
            name = self._safe_text(item.get("name"))
            parts.append(f"{name} <{email}>" if name else email)
        return ", ".join(parts)

    def _looks_like_reply(self, goal: str) -> bool:
        lowered = (goal or "").strip().casefold()
        return any(token in lowered for token in ("reply", "respond", "answer", "send back"))

    def _safe_filename(self, raw: Any) -> str:
        candidate = re.sub(r"[^A-Za-z0-9._-]+", "_", self._safe_text(raw)).strip("._")
        return candidate or f"attachment_{uuid4().hex[:8]}.bin"

    def _safe_text(self, value: Any) -> str:
        return str(value or "").strip()

    def _safe_int(self, value: Any) -> int:
        try:
            return int(value or 0)
        except Exception:
            return 0

    def _required_text(self, payload: dict[str, Any], key: str) -> str:
        value = self._safe_text(payload.get(key))
        if not value:
            raise EmailAgentError(
                code="INVALID_INPUT",
                message=f"{key} is required",
                retryable=False,
                next_action="escalate",
            )
        return value

    def _optional_text(self, payload: dict[str, Any], key: str) -> str | None:
        value = self._safe_text(payload.get(key))
        return value or None

    def _err(self, code: str, message: str, retryable: bool, next_action: str) -> AgentResult:
        return AgentResult(
            status="failed",
            output={},
            artifacts=[],
            error=AgentError(code=code, retryable=retryable, message=message, next_action=next_action),
        )
