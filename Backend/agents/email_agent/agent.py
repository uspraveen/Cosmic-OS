from __future__ import annotations

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
)
from shared.agent_runtime import AgentRuntime
from shared.contracts import AgentError, AgentResult, ArtifactManifest, TaskEnvelope
from shared.sqlite_client import connect_sync

from .config import AGENT_ROOT, BACKEND_ROOT, EmailAgentConfig
from .email_usage import log_email_specialist_operation, monotonic_ms_since
from .internal_llm import invoke_email_mimo, invoke_email_mimo_json

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
    behavior_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_email_instructions_mailbox
ON email_instructions (mailbox_address, enabled);
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
        self.mail_client = CosmicMailClient(
            base_url=self.config.cosmic_mail_base_url,
            api_token=self.config.cosmic_mail_api_token,
            timeout_sec=self.config.cosmic_mail_timeout_sec,
            client=self._http_client,
        )

    async def on_startup(self) -> None:
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.artifacts_root.mkdir(parents=True, exist_ok=True)
        self.integration_store.initialize()
        self._init_db()
        await self._refresh_mail_client_from_store()
        if self.config.cosmic_mail_base_url and self.config.cosmic_mail_api_token:
            await self.mail_client.get_auth_context()

    def _init_db(self) -> None:
        with connect_sync(self.session_db_path) as conn:
            conn.executescript(_RUNS_SQL)
            conn.commit()

    async def execute(self, task: TaskEnvelope) -> AgentResult:
        started = time.perf_counter()
        operation = self._operation_for_intent(task.intent)
        try:
            if task.intent == self.PROCESS_INBOUND:
                result = await self._handle_process_inbound(task)
            elif task.intent == self.REASON:
                result = await self._handle_reason(task)
            elif task.intent == self.MANAGE_INSTRUCTION:
                result = await self._handle_manage_instruction(task)
            elif task.intent == self.RECALL_SESSION:
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
            metadata=self._usage_metadata(task.intent, result),
        )
        return result

    def _operation_for_intent(self, intent: str) -> str:
        return {
            self.PROCESS_INBOUND: "email.process_inbound",
            self.REASON: "email.reason",
            self.MANAGE_INSTRUCTION: "email.manage_instruction",
            self.RECALL_SESSION: "email.recall_session",
        }.get(intent, intent)

    def _usage_metadata(self, intent: str, result: AgentResult) -> dict[str, Any]:
        output = result.output if isinstance(result.output, dict) else {}
        metadata: dict[str, Any] = {"status": result.status}
        if intent in {self.PROCESS_INBOUND, self.REASON}:
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

        context = await self._fetch_thread_context(thread_id=thread_id, message_id=message_id)
        attachments = await self._download_message_attachments(task, message_id=message_id)
        matched_instruction = self._match_instruction(
            mailbox_address=mailbox_address,
            from_address=self._thread_sender(context),
            subject=context.get("subject"),
            body=context.get("latest_body"),
        )
        summary = await self._summarize_thread(
            task=task,
            context=context,
            matched_instruction=matched_instruction,
            operation="email.internal_llm.process_inbound",
        )
        auto_reply = None
        if matched_instruction and self._instruction_mode(matched_instruction) == "auto_reply":
            auto_reply = await self._apply_auto_reply(task, context=context, instruction=matched_instruction)

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
            "subject": context.get("subject"),
            "matched_instruction": matched_instruction,
            "auto_reply": auto_reply,
            "attachments": attachments,
            "action": "processed_inbound",
            "sent": bool(auto_reply and auto_reply.get("sent")),
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
        query = self._optional_text(task.input, "query")
        send = bool(task.input.get("send"))
        subject = self._optional_text(task.input, "subject")
        tone_hint = self._optional_text(task.input, "tone_hint")
        context_brief = self._optional_text(task.input, "context_brief")
        draft_seed = self._optional_text(task.input, "draft_seed")
        recipients = self._normalize_recipient_list(task.input.get("to_recipients"))
        artifacts: list[ArtifactManifest] = []

        if thread_id:
            context = await self._fetch_thread_context(thread_id=thread_id, message_id=message_id)
            if send or draft_seed or self._looks_like_reply(goal):
                drafted = await self._compose_reply(
                    task=task,
                    context=context,
                    goal=goal,
                    context_brief=context_brief,
                    draft_seed=draft_seed,
                    tone_hint=tone_hint,
                )
                sent_payload = None
                if send:
                    sent_payload = await self.mail_client.reply_to_thread(
                        thread_id,
                        {"text_body": drafted["body"]},
                    )
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
                output = {
                    "response": response,
                    "action": "reply_thread",
                    "sent": bool(sent_payload),
                    "thread_id": thread_id,
                    "message_id": self._safe_text(sent_payload.get("id")) if isinstance(sent_payload, dict) else None,
                    "draft_id": None,
                    "summary": response,
                    "search_results": [],
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
                    "search_results": [],
                }
        elif recipients or send or subject:
            drafted = await self._compose_new_email(
                task=task,
                goal=goal,
                context_brief=context_brief,
                draft_seed=draft_seed,
                tone_hint=tone_hint,
                recipients=recipients,
                subject=subject,
            )
            draft_payload = await self._create_outbound_draft(
                task=task,
                recipients=recipients,
                subject=drafted["subject"],
                text_body=drafted["body"],
            )
            draft_id = self._safe_text(draft_payload.get("id"))
            if draft_id:
                await self._upload_input_artifacts_to_draft(task, draft_id=draft_id)
            sent_payload = None
            if send and draft_id:
                sent_payload = await self.mail_client.send_draft(draft_id)
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
            output = {
                "response": drafted["summary"],
                "action": "compose_email",
                "sent": bool(sent_payload),
                "thread_id": self._safe_text(sent_payload.get("thread_id")) if isinstance(sent_payload, dict) else None,
                "message_id": self._safe_text(sent_payload.get("id")) if isinstance(sent_payload, dict) else None,
                "draft_id": draft_id or None,
                "summary": drafted["summary"],
                "search_results": [],
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
                "search_results": search_results,
            }

        self._record_session_run(
            task=task,
            intent=self.REASON,
            mailbox_address=mailbox_address,
            thread_id=thread_id,
            message_id=message_id,
            summary=output,
        )
        return AgentResult(status="completed", output=output, artifacts=artifacts, error=None)

    async def _handle_manage_instruction(self, task: TaskEnvelope) -> AgentResult:
        action = self._required_text(task.input, "action").lower()
        mailbox_address = self._optional_text(task.input, "mailbox_address")
        if action == "list":
            instructions = self._list_instructions(mailbox_address=mailbox_address)
            output = {"response": f"Found {len(instructions)} email instructions.", "instructions": instructions, "instruction": None}
            return AgentResult(status="completed", output=output, artifacts=[], error=None)

        instruction_id = self._optional_text(task.input, "instruction_id") or f"eminst_{uuid4().hex[:12]}"
        if action == "set":
            label = self._required_text(task.input, "label")
            match = task.input.get("match") if isinstance(task.input.get("match"), dict) else {}
            behavior = task.input.get("behavior") if isinstance(task.input.get("behavior"), dict) else {}
            if not any(self._safe_text(match.get(key)) for key in ("from_address", "subject_contains", "body_contains")):
                raise EmailAgentError(
                    code="INVALID_INPUT",
                    message="email.manage_instruction set requires at least one match condition.",
                    retryable=False,
                    next_action="escalate",
                )
            self._upsert_instruction(
                instruction_id=instruction_id,
                mailbox_address=mailbox_address,
                label=label,
                match=match,
                behavior=behavior,
                enabled=True,
            )
        elif action in {"enable", "disable"}:
            self._set_instruction_enabled(instruction_id=instruction_id, enabled=action == "enable")
        elif action == "remove":
            self._delete_instruction(instruction_id=instruction_id)
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

    async def _fetch_thread_context(self, *, thread_id: str, message_id: str | None) -> dict[str, Any]:
        try:
            thread = await self.mail_client.get_thread(thread_id)
            messages = await self.mail_client.get_thread_messages(thread_id)
        except CosmicMailClientError as exc:
            raise EmailAgentError(
                code="NETWORK_ERROR" if exc.status_code is None or exc.status_code >= 500 else "INVALID_INPUT",
                message=exc.message,
                retryable=exc.status_code is None or exc.status_code >= 500,
                next_action="retry" if exc.status_code is None or exc.status_code >= 500 else "escalate",
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
        latest_body = target.get("text_body") if isinstance(target, dict) else ""
        return {
            "thread": thread,
            "messages": normalized_messages,
            "subject": self._safe_text(thread.get("subject")) or (target.get("subject") if isinstance(target, dict) else None),
            "latest_message": target,
            "latest_body": latest_body,
        }

    async def _download_message_attachments(self, task: TaskEnvelope, *, message_id: str) -> list[dict[str, Any]]:
        try:
            attachments = await self.mail_client.list_message_attachments(message_id)
        except CosmicMailClientError as exc:
            logger.warning("email_agent.list_attachments_failed message_id=%s error=%s", message_id, exc)
            return []
        downloaded: list[dict[str, Any]] = []
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
                    }
                )
                continue
            target_dir = self._task_artifact_dir(task.task_id) / "attachments"
            target_dir.mkdir(parents=True, exist_ok=True)
            safe_name = self._safe_filename(filename or item.get("filename") or f"{attachment_id}.bin")
            target_path = target_dir / safe_name
            target_path.write_bytes(content)
            manifest = self._artifact_manifest(
                task_id=task.task_id,
                path=target_path,
                mime=str(mime_type or item.get("content_type") or "application/octet-stream"),
                kind="input",
                audience="supporting",
            )
            downloaded.append(
                {
                    "id": attachment_id,
                    "filename": safe_name,
                    "mime_type": manifest.mime,
                    "size_bytes": len(content),
                    "downloaded": True,
                    "artifact_id": manifest.artifact_id,
                    "path": manifest.path,
                }
            )
        return downloaded

    async def _summarize_thread(
        self,
        *,
        task: TaskEnvelope,
        context: dict[str, Any],
        matched_instruction: dict[str, Any] | None,
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
        if matched_instruction:
            prompt += f"\nMatched standing instruction:\n{json.dumps(matched_instruction, ensure_ascii=False)}\n"
        prompt += f"\nThread subject: {self._safe_text(context.get('subject'))}\n\nThread:\n{transcript[:24000]}"
        summary = await invoke_email_mimo(
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
        generated = await invoke_email_mimo(
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
        try:
            payload = await self.mail_client.reply_to_thread(thread_id, {"text_body": body})
        except CosmicMailClientError as exc:
            raise EmailAgentError(
                code="NETWORK_ERROR" if exc.status_code is None or exc.status_code >= 500 else "AUTH_ERROR",
                message=exc.message,
                retryable=exc.status_code is None or exc.status_code >= 500,
                next_action="retry" if exc.status_code is None or exc.status_code >= 500 else "escalate",
            ) from exc
        return {
            "sent": True,
            "thread_id": thread_id,
            "message_id": self._safe_text(payload.get("id")) or None,
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
    ) -> dict[str, Any]:
        latest_body = self._safe_text(context.get("latest_body"))
        user_message = (
            "Write an email reply draft.\n"
            "Return plain text only.\n"
            f"Goal: {goal}\n"
            f"Tone hint: {tone_hint or 'follow the thread tone'}\n"
            f"Context brief: {context_brief or '(none)'}\n"
            f"Draft seed: {draft_seed or '(none)'}\n"
            f"Thread subject: {self._safe_text(context.get('subject'))}\n\n"
            f"Latest inbound message:\n{latest_body[:6000]}"
        )
        body = await invoke_email_mimo(
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
        subject: str | None,
    ) -> dict[str, Any]:
        payload = await invoke_email_mimo_json(
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
                f"Recipient count: {len(recipients)}\n"
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
        resolved_body = self._safe_text(payload.get("body")) or draft_seed or goal
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
                    "text_body": text_body,
                }
            )
        except CosmicMailClientError as exc:
            raise EmailAgentError(
                code="NETWORK_ERROR" if exc.status_code is None or exc.status_code >= 500 else "AUTH_ERROR",
                message=exc.message,
                retryable=exc.status_code is None or exc.status_code >= 500,
                next_action="retry" if exc.status_code is None or exc.status_code >= 500 else "escalate",
            ) from exc

    async def _upload_input_artifacts_to_draft(self, task: TaskEnvelope, *, draft_id: str) -> None:
        for artifact in task.input_artifacts:
            if not isinstance(artifact, dict):
                continue
            artifact_path = self._resolve_artifact_path(artifact)
            if artifact_path is None or not artifact_path.exists() or not artifact_path.is_file():
                continue
            content = artifact_path.read_bytes()
            if not content:
                continue
            try:
                await self.mail_client.upload_draft_attachment(
                    draft_id,
                    filename=artifact_path.name,
                    content=content,
                    mime_type=self._safe_text(artifact.get("mime")) or None,
                )
            except CosmicMailClientError as exc:
                logger.warning(
                    "email_agent.upload_draft_attachment_failed draft_id=%s path=%s error=%s",
                    draft_id,
                    artifact_path,
                    exc,
                )

    async def _search_email(
        self,
        *,
        task: TaskEnvelope,
        goal: str,
        query: str | None,
        mailbox_address: str | None,
    ) -> list[dict[str, Any]]:
        search_query = query or goal
        mailbox = await self._resolve_mailbox(
            mailbox_address=mailbox_address,
            mailbox_id=self._optional_text(task.input, "mailbox_id"),
            required=False,
        )
        mailbox_id = self._safe_text(mailbox.get("id")) if isinstance(mailbox, dict) else None
        try:
            threads = await self.mail_client.search_threads(
                query=search_query,
                mailbox_id=mailbox_id,
                per_page=self.config.max_search_results,
            )
            messages = await self.mail_client.search_messages(
                query=search_query,
                mailbox_id=mailbox_id,
                per_page=self.config.max_search_results,
            )
        except CosmicMailClientError as exc:
            raise EmailAgentError(
                code="NETWORK_ERROR" if exc.status_code is None or exc.status_code >= 500 else "AUTH_ERROR",
                message=exc.message,
                retryable=exc.status_code is None or exc.status_code >= 500,
                next_action="retry" if exc.status_code is None or exc.status_code >= 500 else "escalate",
            ) from exc
        thread_results = [
            {
                "kind": "thread",
                "id": self._safe_text(item.get("id")),
                "subject": self._safe_text(item.get("subject")),
                "snippet": self._safe_text(item.get("snippet") or item.get("body_preview")),
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
        return thread_results + message_results

    async def _summarize_search_results(self, *, task: TaskEnvelope, goal: str, search_results: list[dict[str, Any]]) -> str:
        if not search_results:
            return "No matching email threads or messages were found."
        summary = await invoke_email_mimo(
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
        try:
            return await self.mail_client.resolve_mailbox(
                mailbox_id=normalized_id or None,
                mailbox_address=normalized_address or None,
            )
        except CosmicMailClientError as exc:
            raise EmailAgentError(
                code="INVALID_INPUT" if exc.status_code == 404 else "NETWORK_ERROR",
                message=exc.message,
                retryable=exc.status_code is None or exc.status_code >= 500,
                next_action="retry" if exc.status_code is None or exc.status_code >= 500 else "escalate",
            ) from exc

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
        elif agent_email_integration_is_configured(stored):
            next_base_url = str(stored.base_url or "").strip()
            next_api_token = str(stored.api_token or "").strip()
            next_mailbox = str(stored.primary_mailbox_address or "").strip()
        else:
            next_base_url = self._env_cosmic_mail_base_url
            next_api_token = self._env_cosmic_mail_api_token
            next_mailbox = self._env_primary_mailbox_address

        current_base_url = str(self.mail_client.base_url or "").strip()
        current_api_token = str(getattr(self.mail_client, "api_token", "") or "").strip()
        self.config.primary_mailbox_address = next_mailbox
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
                           behavior_json, created_at, updated_at
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
                           behavior_json, created_at, updated_at
                    FROM email_instructions
                    ORDER BY updated_at DESC
                    """
                ).fetchall()
        return [self._instruction_row_to_dict(row) for row in rows]

    def _match_instruction(
        self,
        *,
        mailbox_address: str | None,
        from_address: str | None,
        subject: str | None,
        body: str | None,
    ) -> dict[str, Any] | None:
        for instruction in self._list_instructions(mailbox_address=mailbox_address):
            if not instruction.get("enabled"):
                continue
            if instruction.get("mailbox_address") and mailbox_address and instruction["mailbox_address"] != mailbox_address:
                continue
            match = instruction.get("match") if isinstance(instruction.get("match"), dict) else {}
            if not self._instruction_matches(
                match=match,
                from_address=from_address,
                subject=subject,
                body=body,
            ):
                continue
            return instruction
        return None

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

    def _upsert_instruction(
        self,
        *,
        instruction_id: str,
        mailbox_address: str | None,
        label: str,
        match: dict[str, Any],
        behavior: dict[str, Any],
        enabled: bool,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with connect_sync(self.session_db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO email_instructions (
                    instruction_id, mailbox_address, label, enabled,
                    match_from_address, match_subject_contains, match_body_contains,
                    behavior_json, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?,
                    COALESCE((SELECT created_at FROM email_instructions WHERE instruction_id = ?), ?),
                    ?
                )
                """,
                (
                    instruction_id,
                    mailbox_address,
                    label,
                    1 if enabled else 0,
                    self._safe_text(match.get("from_address")) or None,
                    self._safe_text(match.get("subject_contains")) or None,
                    self._safe_text(match.get("body_contains")) or None,
                    json.dumps(behavior, ensure_ascii=False, separators=(",", ":")),
                    instruction_id,
                    now,
                    now,
                ),
            )
            conn.commit()

    def _set_instruction_enabled(self, *, instruction_id: str, enabled: bool) -> None:
        with connect_sync(self.session_db_path) as conn:
            cursor = conn.execute(
                "UPDATE email_instructions SET enabled = ?, updated_at = ? WHERE instruction_id = ?",
                (
                    1 if enabled else 0,
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
            behavior = json.loads(row[7]) if row[7] else {}
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
            "behavior": behavior if isinstance(behavior, dict) else {},
            "created_at": row[8],
            "updated_at": row[9],
        }

    def _instruction_mode(self, instruction: dict[str, Any]) -> str:
        behavior = instruction.get("behavior") if isinstance(instruction.get("behavior"), dict) else {}
        mode = self._safe_text(behavior.get("mode")).lower()
        return mode or "notify_only"

    def _normalize_message_record(self, message: dict[str, Any]) -> dict[str, Any]:
        from_contacts = message.get("from_recipients") if isinstance(message.get("from_recipients"), list) else []
        from_address = None
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
        }

    def _thread_sender(self, context: dict[str, Any]) -> str | None:
        latest = context.get("latest_message") if isinstance(context.get("latest_message"), dict) else {}
        return self._safe_text(latest.get("from_address")) or None

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
