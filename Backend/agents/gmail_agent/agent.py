"""Gmail Agent — user-owned Gmail inbox specialist for COSMIC."""

from __future__ import annotations

import json
import logging
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx

from shared import utcnow
from shared.agent_runtime import AgentRuntime
from shared.contracts import AgentError, AgentResult, TaskEnvelope
from shared.sqlite_client import connect_sync

from .config import AGENT_ROOT, BACKEND_ROOT, GmailAgentConfig
from .google_gmail_client import (
    GoogleGmailClient,
    compact_message_for_llm,
    extract_domain,
    extract_email_address,
)
from .internal_llm import invoke_gmail_draft_llm, invoke_gmail_triage_llm
from .sender_prefilter import SenderPrefilter

logger = logging.getLogger(__name__)

_GMAIL_SESSIONS_SQL = """
CREATE TABLE IF NOT EXISTS gmail_session_runs (
    task_id TEXT PRIMARY KEY,
    session_id TEXT,
    intent TEXT NOT NULL,
    account_id TEXT,
    account_email TEXT,
    query TEXT,
    result_summary_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gmail_runs_session_created
ON gmail_session_runs (session_id, created_at DESC);

CREATE TABLE IF NOT EXISTS gmail_history_cursors (
    account_id TEXT PRIMARY KEY,
    account_email TEXT,
    history_id TEXT,
    watch_expiration_ms TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gmail_triage_decisions (
    decision_id TEXT PRIMARY KEY,
    account_id TEXT,
    account_email TEXT,
    message_id TEXT NOT NULL,
    thread_id TEXT,
    sender TEXT,
    sender_email TEXT,
    sender_domain TEXT,
    subject TEXT,
    message_date TEXT,
    snippet TEXT,
    category TEXT NOT NULL,
    confidence REAL,
    priority INTEGER,
    surface_to_user INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    suggested_action TEXT,
    source TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gmail_triage_message
ON gmail_triage_decisions (account_id, message_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_gmail_triage_account_created
ON gmail_triage_decisions (account_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_gmail_triage_surface
ON gmail_triage_decisions (account_id, surface_to_user, created_at DESC);
"""


class GmailAgent(AgentRuntime):
    SEARCH = "gmail.search"
    READ_THREAD = "gmail.read_thread"
    TRIAGE_INBOX = "gmail.triage_inbox"
    DRAFT_REPLY = "gmail.draft_reply"
    PROCESS_INBOUND = "gmail.process_inbound"
    HEARTBEAT_DIGEST = "gmail.heartbeat_digest"
    MORNING_BRIEFING_DIGEST = "gmail.morning_briefing_digest"
    MANAGE_PREFILTER = "gmail.manage_prefilter"
    RECALL_SESSION = "gmail.recall_session"
    SYNC_WATCH = "gmail.sync_watch"
    STOP_WATCH = "gmail.stop_watch"

    def __init__(
        self,
        *,
        redis_client,
        config: GmailAgentConfig | None = None,
        instance_id: str | None = None,
        agent_secret: str | None = None,
        registry_db_path: str | Path | None = None,
        http_client: httpx.AsyncClient | None = None,
        agent_root: str | Path | None = None,
        store_root: str | Path | None = None,
    ) -> None:
        self.config = config or GmailAgentConfig.from_env()
        self.agent_root = (Path(agent_root).expanduser() if agent_root else AGENT_ROOT).resolve()
        self.store_root = (Path(store_root).expanduser() if store_root else self.agent_root / "store").resolve()
        self.data_root = self.store_root / "data"
        self.session_db_path = self.data_root / "gmail_agent.db"
        self.prefilter = SenderPrefilter(self.store_root / "sender_prefilter.json")
        self.prompts_dir = self.agent_root / "prompts"
        self.learnings_path = self.store_root / "learnings.md"
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

    async def on_startup(self) -> None:
        self.store_root.mkdir(parents=True, exist_ok=True)
        self.data_root.mkdir(parents=True, exist_ok=True)
        (self.agent_root / "runtime" / "cache").mkdir(parents=True, exist_ok=True)
        (self.agent_root / "runtime" / "logs").mkdir(parents=True, exist_ok=True)
        if not self.learnings_path.exists():
            self.learnings_path.write_text("# Gmail Agent - Learnings\n", encoding="utf-8")
        with connect_sync(self.session_db_path) as conn:
            conn.executescript(_GMAIL_SESSIONS_SQL)
            self._ensure_triage_schema(conn)
            conn.commit()

    def _ensure_triage_schema(self, conn) -> None:
        existing = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(gmail_triage_decisions)").fetchall()
        }
        additions = {
            "sender": "TEXT",
            "sender_domain": "TEXT",
            "subject": "TEXT",
            "message_date": "TEXT",
            "snippet": "TEXT",
            "priority": "INTEGER",
            "suggested_action": "TEXT",
            "source": "TEXT",
        }
        for column, ddl in additions.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE gmail_triage_decisions ADD COLUMN {column} {ddl}")

    async def execute(self, task: TaskEnvelope) -> AgentResult:
        started = time.perf_counter()
        handler = getattr(self, f"handle_{task.intent.replace('.', '_')}", None)
        if not handler:
            return self._err("INVALID_INPUT", f"Unknown intent: {task.intent}", False, "escalate")
        try:
            result = await handler(task)
            self._save_session(task, result.output)
            return result
        except PermissionError:
            return await self._handle_auth_error(task)
        except ValueError as exc:
            return self._err("INVALID_INPUT", str(exc), False, "escalate")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                return await self._handle_auth_error(task)
            return self._err(
                "NETWORK_ERROR",
                f"Gmail API error: {exc.response.status_code}",
                True,
                "retry",
            )
        except httpx.TimeoutException:
            return self._err("TIMEOUT", "Gmail API timed out.", True, "retry")
        except Exception as exc:
            logger.exception("gmail_agent.error task_id=%s intent=%s elapsed_ms=%.1f", task.task_id, task.intent, (time.perf_counter() - started) * 1000)
            return self._err("INTERNAL_ERROR", str(exc), False, "escalate")

    async def handle_gmail_search(self, task: TaskEnvelope) -> AgentResult:
        await self._maybe_create_plan(task, ["Resolve Gmail account", "Search messages", "Return compact results"])
        client = self._client()
        query = self._build_query(task.input)
        max_results = self._bounded_int(task.input.get("max_results"), self.config.max_search_results, 1, 50)
        messages = await client.search_messages(query=query, max_results=max_results)
        compact = [self._attach_account(compact_message_for_llm(msg, max_body_chars=600), task) for msg in messages]
        await self._maybe_step(3, "completed", f"Found {len(compact)} Gmail messages.")
        return AgentResult(
            status="completed",
            output={
                "status": "completed",
                "query": query,
                "account": self._account_info(),
                "messages": compact,
                "count": len(compact),
            },
            artifacts=[],
        )

    async def handle_gmail_read_thread(self, task: TaskEnvelope) -> AgentResult:
        thread_id = str(task.input.get("thread_id") or "").strip()
        message_id = str(task.input.get("message_id") or "").strip()
        if not thread_id and message_id:
            msg = await self._client().get_message(message_id)
            thread_id = str(msg.get("thread_id") or "").strip()
        if not thread_id:
            raise ValueError("gmail.read_thread requires thread_id or message_id.")
        thread = await self._client().get_thread(thread_id)
        max_messages = self._bounded_int(task.input.get("max_thread_messages"), self.config.max_thread_messages, 1, 100)
        messages = thread.get("messages") or []
        thread["messages"] = [
            self._attach_account(compact_message_for_llm(msg, max_body_chars=self.config.max_body_chars), task)
            for msg in messages[-max_messages:]
        ]
        thread["account"] = self._account_info()
        return AgentResult(status="completed", output={"status": "completed", "thread": thread}, artifacts=[])

    async def handle_gmail_triage_inbox(self, task: TaskEnvelope) -> AgentResult:
        await self._maybe_create_plan(task, ["Fetch recent Gmail messages", "Apply learned sender prefilter", "Run LLM triage", "Update learned prefilter"])
        max_results = self._bounded_int(task.input.get("max_results"), self.config.max_triage_messages, 1, 40)
        query = self._build_query(task.input, default_query="newer_than:7d -in:trash")
        messages = await self._client().search_messages(query=query, max_results=max_results)
        await self._maybe_step(1, "completed", f"Fetched {len(messages)} recent messages.")
        triage_payload = await self._triage_message_batch(
            task,
            messages,
            context_brief=str(task.input.get("context_brief") or ""),
            source="triage_inbox",
        )
        skipped = triage_payload["skipped_by_prefilter"]
        decisions = triage_payload["items"]
        added_prefilters = triage_payload["added_prefilters"]
        await self._maybe_step(2, "completed", f"Skipped {len(skipped)} learned-noise senders.")
        await self._maybe_step(3, "completed", f"Classified {len(decisions)} messages.")
        await self._maybe_step(4, "completed", f"Updated {len(added_prefilters)} sender prefilters.")
        return AgentResult(
            status="completed",
            output={
                "status": "completed",
                "account": self._account_info(),
                "query": query,
                "llm_used": bool(triage_payload["llm_used"]),
                "summary": triage_payload.get("summary") or "",
                "items": [self._attach_account(item, task) for item in decisions],
                "skipped_by_prefilter": skipped,
                "added_prefilters": added_prefilters,
            },
            artifacts=[],
        )

    async def handle_gmail_draft_reply(self, task: TaskEnvelope) -> AgentResult:
        await self._maybe_create_plan(task, ["Read thread context if needed", "Draft response", "Create Gmail draft"])
        client = self._client()
        thread = None
        thread_id = str(task.input.get("thread_id") or "").strip()
        message_id = str(task.input.get("message_id") or "").strip()
        if thread_id or message_id:
            if not thread_id:
                msg = await client.get_message(message_id)
                thread_id = str(msg.get("thread_id") or "").strip()
            thread = await client.get_thread(thread_id)
        await self._maybe_step(1, "completed", "Thread context prepared.")
        request = str(task.input.get("request") or task.input.get("query") or task.input.get("body") or "").strip()
        if not request:
            raise ValueError("gmail.draft_reply requires request, query, or body.")
        draft_plan = await self._build_draft_plan(task, request=request, thread=thread)
        await self._maybe_step(2, "completed", "Draft body prepared.")
        draft = await client.create_draft(
            to=self._string_list(draft_plan.get("to") or task.input.get("to")),
            cc=self._string_list(draft_plan.get("cc") or task.input.get("cc")),
            bcc=self._string_list(draft_plan.get("bcc") or task.input.get("bcc")),
            subject=str(draft_plan.get("subject") or task.input.get("subject") or ""),
            body=str(draft_plan.get("body") or task.input.get("body") or ""),
            thread_id=thread_id or None,
            in_reply_to=self._latest_header(thread, "message_id_header"),
            references=self._latest_header(thread, "references") or self._latest_header(thread, "message_id_header"),
        )
        await self._maybe_step(3, "completed", "Created Gmail draft.")
        return AgentResult(
            status="completed",
            output={
                "status": "draft_created",
                "account": self._account_info(),
                "draft_id": draft.get("id"),
                "message": draft.get("message"),
                "approval_required": True,
                "delivery_status": "draft_created_pending_user_approval",
                "draft": draft_plan,
                "notes": draft_plan.get("notes") or "",
            },
            artifacts=[],
        )

    async def handle_gmail_process_inbound(self, task: TaskEnvelope) -> AgentResult:
        message_id = str(task.input.get("message_id") or "").strip()
        thread_id = str(task.input.get("thread_id") or "").strip()
        history_id = str(task.input.get("history_id") or "").strip()
        if message_id or thread_id:
            client = self._client()
            if thread_id:
                thread = await client.get_thread(thread_id)
                messages = [
                    item
                    for item in (thread.get("messages") or [])[-3:]
                    if isinstance(item, dict)
                ]
            else:
                message = await client.get_message(message_id)
                thread_id = str(message.get("thread_id") or "").strip()
                thread = {
                    "thread_id": thread_id,
                    "messages": [message],
                    "message_count": 1,
                    "latest_message": message,
                }
                messages = [message]
            triage_payload = await self._triage_message_batch(
                task,
                messages,
                context_brief=str(task.input.get("context_brief") or "Inbound Gmail webhook/change notification."),
                source="process_inbound",
            )
            items = [self._attach_account(item, task) for item in triage_payload["items"]]
            return AgentResult(
                status="completed",
                output={
                    "status": "processed",
                    "account": self._account_info(),
                    "reason": "inbound_message_triaged",
                    "thread": {
                        "thread_id": thread.get("thread_id"),
                        "message_count": thread.get("message_count") or len(messages),
                        "latest_message": compact_message_for_llm(
                            thread.get("latest_message") or messages[-1],
                            max_body_chars=600,
                        )
                        if messages
                        else None,
                    },
                    "items": items,
                    "messages": items,
                    "llm_used": bool(triage_payload["llm_used"]),
                    "skipped_by_prefilter": triage_payload["skipped_by_prefilter"],
                    "added_prefilters": triage_payload["added_prefilters"],
                    "summary": triage_payload.get("summary") or "",
                },
                artifacts=[],
            )
        if history_id:
            return await self._process_history_notification(task, history_id=history_id)
        return AgentResult(
            status="completed",
            output={
                "status": "accepted",
                "account": self._account_info(),
                "history_id": history_id,
                "message": "Gmail inbound notification accepted. Provide message_id/thread_id or configure stored history cursor replay for full processing.",
            },
            artifacts=[],
        )

    async def handle_gmail_heartbeat_digest(self, task: TaskEnvelope) -> AgentResult:
        max_items = self._bounded_int(task.input.get("max_items"), self.config.max_digest_items, 1, 12)
        lookback_hours = self._bounded_int(task.input.get("lookback_hours"), 24, 1, 168)
        cached_items = self._recent_triage_decisions(
            max_items=max_items,
            lookback_hours=lookback_hours,
            surface_only=True,
        )
        if cached_items:
            return AgentResult(
                status="completed",
                output={
                    "status": "completed",
                    "account": self._account_info(),
                    "reason": "cached_triage_reconciliation",
                    "source": "cached_triage_decisions",
                    "mode": "heartbeat_reconcile",
                    "lookback_hours": lookback_hours,
                    "items": cached_items,
                    "messages": cached_items,
                    "cached_item_count": len(cached_items),
                    "live_triage_used": False,
                    "llm_used": False,
                    "skipped_by_prefilter": [],
                    "summary": "Heartbeat used cached Gmail triage state; no live inbox triage was run.",
                },
                artifacts=[],
            )

        allow_live_check = self._bool_input(task.input.get("allow_live_check"), default=False)
        if not allow_live_check:
            return AgentResult(
                status="completed",
                output={
                    "status": "completed",
                    "account": self._account_info(),
                    "reason": "no_cached_actionable_items",
                    "source": "cached_triage_decisions",
                    "mode": "heartbeat_reconcile",
                    "lookback_hours": lookback_hours,
                    "items": [],
                    "messages": [],
                    "cached_item_count": 0,
                    "live_triage_used": False,
                    "llm_used": False,
                    "skipped_by_prefilter": [],
                    "summary": "No cached actionable Gmail items were found for this heartbeat.",
                },
                artifacts=[],
            )

        query = self._build_query(task.input, default_query="newer_than:1d is:unread -in:trash")
        triage_task = task.model_copy(update={"intent": self.TRIAGE_INBOX, "input": {**task.input, "query": query, "max_results": max(max_items * 2, max_items)}})
        triage = await self.handle_gmail_triage_inbox(triage_task)
        items = self._actionable_items(triage.output.get("items") or [], max_items=max_items)
        reason = "live_reconciliation_items_found" if items else "live_reconciliation_nothing_actionable"
        return AgentResult(
            status="completed",
            output={
                "status": "completed",
                "account": self._account_info(),
                "reason": reason,
                "source": "live_bounded_check",
                "mode": "heartbeat_reconcile",
                "lookback_hours": lookback_hours,
                "items": items,
                "messages": items,
                "cached_item_count": 0,
                "live_triage_used": True,
                "llm_used": bool(triage.output.get("llm_used")),
                "skipped_by_prefilter": triage.output.get("skipped_by_prefilter") or [],
                "summary": triage.output.get("summary") or "",
            },
            artifacts=[],
        )

    async def handle_gmail_morning_briefing_digest(self, task: TaskEnvelope) -> AgentResult:
        max_items = self._bounded_int(task.input.get("max_items"), 12, 1, 30)
        query = self._build_query(task.input, default_query="newer_than:1d -in:trash")
        triage_task = task.model_copy(
            update={
                "intent": self.TRIAGE_INBOX,
                "input": {
                    **task.input,
                    "query": query,
                    "max_results": max(max_items * 3, max_items),
                    "context_brief": str(task.input.get("context_brief") or "Morning briefing Gmail scan."),
                },
            }
        )
        triage = await self.handle_gmail_triage_inbox(triage_task)
        items = self._actionable_items(triage.output.get("items") or [], max_items=max_items)
        return AgentResult(
            status="completed",
            output={
                "status": "completed",
                "account": self._account_info(),
                "reason": "morning_briefing_scan",
                "source": "live_morning_briefing_scan",
                "mode": "morning_briefing",
                "query": query,
                "items": items,
                "messages": items,
                "llm_used": bool(triage.output.get("llm_used")),
                "skipped_by_prefilter": triage.output.get("skipped_by_prefilter") or [],
                "summary": triage.output.get("summary") or "",
            },
            artifacts=[],
        )

    async def handle_gmail_manage_prefilter(self, task: TaskEnvelope) -> AgentResult:
        action = str(task.input.get("action") or "list").strip().lower()
        value = str(task.input.get("value") or "").strip()
        reason = str(task.input.get("reason") or "User requested Gmail prefilter update.").strip()
        result: dict[str, Any]
        if action == "add_sender":
            result = self.prefilter.add_sender(value, reason=reason, source="manual")
        elif action == "add_domain":
            result = self.prefilter.add_domain(value, reason=reason, source="manual")
        elif action == "remove":
            result = {"removed": self.prefilter.remove(value), "value": value}
        elif action == "list":
            result = {"prefilter": self.prefilter.load()}
        else:
            raise ValueError("action must be one of: list, add_sender, add_domain, remove.")
        return AgentResult(status="completed", output={"status": "completed", **result}, artifacts=[])

    async def handle_gmail_recall_session(self, task: TaskEnvelope) -> AgentResult:
        limit = self._bounded_int(task.input.get("limit"), 8, 1, 50)
        session_id = str(task.input.get("session_id") or task.session_id or "").strip()
        with connect_sync(self.session_db_path) as conn:
            rows = conn.execute(
                """
                SELECT task_id, session_id, intent, account_email, query, result_summary_json, created_at
                FROM gmail_session_runs
                WHERE (? = '' OR session_id = ?)
                ORDER BY created_at DESC
                LIMIT ?
                """,
                [session_id, session_id, limit],
            ).fetchall()
        runs = []
        for row in rows:
            runs.append(
                {
                    "task_id": row[0],
                    "session_id": row[1],
                    "intent": row[2],
                    "account_email": row[3],
                    "query": row[4],
                    "summary": self._json_loads(row[5], {}),
                    "created_at": row[6],
                }
            )
        return AgentResult(status="completed", output={"status": "completed", "runs": runs}, artifacts=[])

    async def handle_gmail_sync_watch(self, task: TaskEnvelope) -> AgentResult:
        topic_name = str(task.input.get("topic_name") or self.config.gmail_watch_topic_name).strip()
        if not topic_name:
            return self._err(
                "CONFIG_REQUIRED",
                "Gmail watch requires GMAIL_WATCH_TOPIC_NAME or input.topic_name.",
                False,
                "configure",
            )
        label_ids = self._string_list(task.input.get("label_ids")) or [
            item.strip()
            for item in self.config.gmail_watch_label_ids.split(",")
            if item.strip()
        ]
        result = await self._client().start_watch(topic_name=topic_name, label_ids=label_ids)
        self._save_history_cursor(
            history_id=str(result.get("historyId") or ""),
            watch_expiration_ms=str(result.get("expiration") or ""),
        )
        return AgentResult(
            status="completed",
            output={
                "status": "watch_registered",
                "account": self._account_info(),
                "topic_name": topic_name,
                "label_ids": label_ids,
                "history_id": result.get("historyId"),
                "expiration": result.get("expiration"),
            },
            artifacts=[],
        )

    async def handle_gmail_stop_watch(self, task: TaskEnvelope) -> AgentResult:
        await self._client().stop_watch()
        self._clear_history_cursor()
        return AgentResult(
            status="completed",
            output={
                "status": "watch_stopped",
                "account": self._account_info(),
                "message": "Gmail watch stopped and local history cursor cleared.",
            },
            artifacts=[],
        )

    def _client(self) -> GoogleGmailClient:
        token = self._require_auth()
        return GoogleGmailClient(token, timeout_sec=30.0)

    async def _process_history_notification(
        self,
        task: TaskEnvelope,
        *,
        history_id: str,
    ) -> AgentResult:
        await self._maybe_create_plan(
            task,
            [
                "Load Gmail history cursor",
                "Replay Gmail history changes",
                "Fetch changed messages",
                "Run LLM triage",
                "Advance history cursor",
            ],
        )
        client = self._client()
        cursor = self._history_cursor()
        start_history_id = str(cursor.get("history_id") or "").strip()
        max_results = self._bounded_int(
            task.input.get("max_results"),
            self.config.max_triage_messages,
            1,
            40,
        )
        if not start_history_id:
            self._save_history_cursor(history_id=history_id)
            await self._maybe_step(1, "completed", "Seeded Gmail history cursor.")
            return AgentResult(
                status="completed",
                output={
                    "status": "cursor_seeded",
                    "account": self._account_info(),
                    "history_id": history_id,
                    "reason": "No previous Gmail history cursor existed; seeded cursor without replay.",
                    "items": [],
                    "messages": [],
                    "llm_used": False,
                },
                artifacts=[],
            )

        await self._maybe_step(1, "completed", f"Using Gmail history cursor {start_history_id}.")
        try:
            history = await client.list_history(
                start_history_id=start_history_id,
                history_types=["messageAdded"],
                label_id="INBOX",
                max_results=max_results,
            )
            message_ids = self._message_ids_from_history(history)
            await self._maybe_step(2, "completed", f"Found {len(message_ids)} changed Gmail messages.")
            messages = [await client.get_message(message_id) for message_id in message_ids[:max_results]]
            await self._maybe_step(3, "completed", f"Fetched {len(messages)} changed Gmail messages.")
            triage_payload = await self._triage_message_batch(
                task,
                messages,
                context_brief=str(task.input.get("context_brief") or "Inbound Gmail push notification."),
                source="process_inbound",
            )
            items = [self._attach_account(item, task) for item in triage_payload["items"]]
            next_history_id = str(history.get("historyId") or history_id).strip() or history_id
            self._save_history_cursor(history_id=next_history_id)
            await self._maybe_step(4, "completed", f"Classified {len(items)} changed messages.")
            await self._maybe_step(5, "completed", f"Advanced Gmail history cursor to {next_history_id}.")
            return AgentResult(
                status="completed",
                output={
                    "status": "processed",
                    "account": self._account_info(),
                    "reason": "gmail_history_replayed",
                    "history_id": next_history_id,
                    "previous_history_id": start_history_id,
                    "message_count": len(messages),
                    "items": items,
                    "messages": items,
                    "llm_used": bool(triage_payload["llm_used"]),
                    "skipped_by_prefilter": triage_payload["skipped_by_prefilter"],
                    "added_prefilters": triage_payload["added_prefilters"],
                    "summary": triage_payload.get("summary") or "",
                },
                artifacts=[],
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {404, 400}:
                raise
            logger.warning(
                "gmail_agent.history_replay_failed_fallback task_id=%s status=%s",
                task.task_id,
                exc.response.status_code,
            )
            query = str(task.input.get("fallback_query") or "newer_than:1d in:inbox -in:trash").strip()
            messages = await client.search_messages(query=query, max_results=max_results)
            triage_payload = await self._triage_message_batch(
                task,
                messages,
                context_brief="Gmail history cursor was invalid or expired; ran bounded recent inbox fallback.",
                source="process_inbound_fallback",
            )
            items = [self._attach_account(item, task) for item in triage_payload["items"]]
            self._save_history_cursor(history_id=history_id)
            return AgentResult(
                status="completed",
                output={
                    "status": "processed",
                    "account": self._account_info(),
                    "reason": "history_cursor_expired_fallback_recent_scan",
                    "history_id": history_id,
                    "previous_history_id": start_history_id,
                    "query": query,
                    "message_count": len(messages),
                    "items": items,
                    "messages": items,
                    "llm_used": bool(triage_payload["llm_used"]),
                    "skipped_by_prefilter": triage_payload["skipped_by_prefilter"],
                    "added_prefilters": triage_payload["added_prefilters"],
                    "summary": triage_payload.get("summary") or "",
                },
                artifacts=[],
            )

    def _message_ids_from_history(self, history: dict[str, Any]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for entry in history.get("history") or []:
            if not isinstance(entry, dict):
                continue
            for added in entry.get("messagesAdded") or []:
                if not isinstance(added, dict):
                    continue
                message = added.get("message") if isinstance(added.get("message"), dict) else {}
                message_id = str(message.get("id") or "").strip()
                labels = {str(item) for item in (message.get("labelIds") or [])}
                if message_id and message_id not in seen and (not labels or "INBOX" in labels):
                    seen.add(message_id)
                    result.append(message_id)
        return result

    def _require_auth(self) -> str:
        if not self.auth or not self.auth.get("access_token"):
            raise PermissionError("No Google credentials provided for Gmail operation.")
        return str(self.auth["access_token"])

    async def _triage_message_batch(
        self,
        task: TaskEnvelope,
        messages: list[dict[str, Any]],
        *,
        context_brief: str,
        source: str,
    ) -> dict[str, Any]:
        candidates, skipped = self._apply_prefilter(messages)
        compact = [compact_message_for_llm(msg, max_body_chars=1200) for msg in candidates]
        memory_context = await self._memory_context_for_messages(compact)
        llm_used = False
        triage_payload: dict[str, Any] = {"items": [], "summary": ""}
        if compact:
            try:
                triage_payload = await invoke_gmail_triage_llm(
                    cfg=self.config,
                    http_client=self._http_client,
                    messages=compact,
                    context_brief=context_brief,
                    memory_context=memory_context,
                )
                llm_used = True
            except Exception as exc:
                logger.warning("gmail_agent.triage_llm_failed task_id=%s error=%s", task.task_id, exc)
                triage_payload = self._fallback_triage(compact, reason=str(exc))
        decisions = self._normalize_decisions(triage_payload.get("items"), candidates)
        added_prefilters = self._maybe_update_prefilter(decisions, candidates)
        self._record_triage_decisions(decisions, source=source)
        return {
            "items": decisions,
            "skipped_by_prefilter": skipped,
            "added_prefilters": added_prefilters,
            "llm_used": llm_used,
            "summary": triage_payload.get("summary") or "",
        }

    async def _handle_auth_error(self, task: TaskEnvelope) -> AgentResult:
        if self.auth and self.auth.get("credential_ref"):
            try:
                await self.submit_reverse_task(
                    current_task=task,
                    intent="orchestrator.refresh_credential",
                    input_payload={
                        "credential_ref": self.auth.get("credential_ref", ""),
                        "provider": "google",
                        "parent_task_id": task.task_id,
                    },
                )
            except Exception:
                logger.exception("gmail_agent.credential_refresh_request_failed task_id=%s", task.task_id)
            return self._err("AUTH_ERROR", "Gmail credential expired. Requested refresh.", True, "retry")
        return self._err("AUTH_ERROR", "No Google credentials available for Gmail operation.", False, "escalate")

    async def _build_draft_plan(
        self,
        task: TaskEnvelope,
        *,
        request: str,
        thread: dict[str, Any] | None,
    ) -> dict[str, Any]:
        explicit_body = str(task.input.get("body") or "").strip()
        explicit_to = self._string_list(task.input.get("to"))
        if explicit_body and explicit_to:
            return {
                "subject": str(task.input.get("subject") or self._reply_subject(thread)).strip(),
                "body": explicit_body,
                "to": explicit_to,
                "cc": self._string_list(task.input.get("cc")),
                "bcc": self._string_list(task.input.get("bcc")),
                "notes": "Used explicit draft fields from task input.",
            }
        memory_context = await self._memory_context_for_thread(thread)
        plan = await invoke_gmail_draft_llm(
            cfg=self.config,
            http_client=self._http_client,
            request=request,
            thread=self._compact_thread(thread),
            context_brief=str(task.input.get("context_brief") or ""),
            memory_context=memory_context,
        )
        if not self._string_list(plan.get("to")) and thread:
            latest = (thread.get("messages") or [])[-1] if thread.get("messages") else {}
            sender = extract_email_address(str(latest.get("from") or ""))
            if sender:
                plan["to"] = [sender]
        if not str(plan.get("subject") or "").strip():
            plan["subject"] = self._reply_subject(thread) or str(task.input.get("subject") or "").strip()
        if not str(plan.get("body") or "").strip():
            raise ValueError("Gmail draft body could not be generated.")
        if not self._string_list(plan.get("to")):
            raise ValueError("Gmail draft recipients could not be determined.")
        return plan

    def _build_query(self, raw: dict[str, Any], *, default_query: str = "newer_than:30d -in:trash") -> str:
        query = str(raw.get("query") or raw.get("q") or "").strip()
        pieces = [query] if query else [default_query]
        sender = str(raw.get("from") or raw.get("sender") or "").strip()
        if sender and "from:" not in query:
            pieces.append(f"from:{sender}")
        subject = str(raw.get("subject") or "").strip()
        if subject and "subject:" not in query:
            pieces.append(f"subject:({subject})")
        if raw.get("unread") is True and "is:unread" not in query:
            pieces.append("is:unread")
        after = str(raw.get("after") or "").strip()
        if after:
            pieces.append(f"after:{after}")
        before = str(raw.get("before") or "").strip()
        if before:
            pieces.append(f"before:{before}")
        return " ".join(piece for piece in pieces if piece).strip()

    def _apply_prefilter(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        candidates: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for msg in messages:
            sender = str(msg.get("from") or "")
            match = self.prefilter.match(sender)
            if match:
                skipped.append(
                    {
                        "message_id": msg.get("message_id"),
                        "thread_id": msg.get("thread_id"),
                        "sender": sender,
                        "matched": match,
                    }
                )
            else:
                candidates.append(msg)
        return candidates, skipped

    def _normalize_decisions(
        self,
        raw_items: Any,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        by_id = {str(msg.get("message_id") or ""): msg for msg in messages}
        if not isinstance(raw_items, list):
            raw_items = []
        decisions: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            message_id = str(raw.get("message_id") or "").strip()
            msg = by_id.get(message_id)
            if not msg or message_id in seen:
                continue
            seen.add(message_id)
            category = str(raw.get("category") or "needs_review").strip()
            if category not in {"urgent", "needs_reply", "needs_review", "read_later", "notification", "spam_or_noise"}:
                category = "needs_review"
            confidence = self._float(raw.get("confidence"), 0.0)
            priority = self._bounded_int(raw.get("priority"), 0, 0, 100)
            surface = bool(raw.get("surface_to_user"))
            if category in {"urgent", "needs_reply", "needs_review"} and confidence >= 0.45:
                surface = True
            sender_email = extract_email_address(str(msg.get("from") or ""))
            decisions.append(
                {
                    "message_id": message_id,
                    "thread_id": msg.get("thread_id"),
                    "sender": msg.get("from"),
                    "sender_email": sender_email,
                    "sender_domain": extract_domain(sender_email),
                    "subject": msg.get("subject"),
                    "date": msg.get("date"),
                    "snippet": msg.get("snippet"),
                    "category": category,
                    "confidence": confidence,
                    "priority": priority,
                    "reason": str(raw.get("reason") or "").strip(),
                    "suggested_action": str(raw.get("suggested_action") or "").strip(),
                    "surface_to_user": surface,
                    "prefilter_sender": bool(raw.get("prefilter_sender")),
                    "prefilter_domain": bool(raw.get("prefilter_domain")),
                }
            )
        for msg in messages:
            message_id = str(msg.get("message_id") or "").strip()
            if message_id and message_id not in seen:
                sender_email = extract_email_address(str(msg.get("from") or ""))
                decisions.append(
                    {
                        "message_id": message_id,
                        "thread_id": msg.get("thread_id"),
                        "sender": msg.get("from"),
                        "sender_email": sender_email,
                        "sender_domain": extract_domain(sender_email),
                        "subject": msg.get("subject"),
                        "date": msg.get("date"),
                        "snippet": msg.get("snippet"),
                        "category": "needs_review",
                        "confidence": 0.0,
                        "priority": 0,
                        "reason": "No LLM decision was returned for this message.",
                        "suggested_action": "",
                        "surface_to_user": False,
                        "prefilter_sender": False,
                        "prefilter_domain": False,
                    }
                )
        return decisions

    def _fallback_triage(self, compact_messages: list[dict[str, Any]], *, reason: str) -> dict[str, Any]:
        return {
            "summary": f"Gmail internal LLM unavailable; returning messages without spam/noise classification. {reason[:160]}",
            "items": [
                {
                    "message_id": msg.get("message_id"),
                    "thread_id": msg.get("thread_id"),
                    "category": "needs_review",
                    "confidence": 0.0,
                    "priority": 0,
                    "reason": "LLM triage was unavailable.",
                    "surface_to_user": False,
                    "suggested_action": "Review manually if relevant.",
                    "prefilter_sender": False,
                    "prefilter_domain": False,
                }
                for msg in compact_messages
            ],
        }

    def _maybe_update_prefilter(
        self,
        decisions: list[dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not self.config.auto_prefilter_high_confidence_noise:
            return []
        added: list[dict[str, Any]] = []
        by_id = {str(msg.get("message_id") or ""): msg for msg in messages}
        for item in decisions:
            if item.get("category") != "spam_or_noise":
                continue
            if self._float(item.get("confidence"), 0.0) < self.config.prefilter_confidence_threshold:
                continue
            message = by_id.get(str(item.get("message_id") or ""))
            sender_email = item.get("sender_email") or extract_email_address(str((message or {}).get("from") or ""))
            sender_domain = item.get("sender_domain") or extract_domain(str(sender_email or ""))
            reason = str(item.get("reason") or "High-confidence recurring Gmail noise.").strip()
            try:
                if item.get("prefilter_sender") and sender_email:
                    added.append(self.prefilter.add_sender(str(sender_email), reason=reason, source="llm"))
                elif item.get("prefilter_domain") and sender_domain:
                    added.append(self.prefilter.add_domain(str(sender_domain), reason=reason, source="llm"))
            except ValueError:
                continue
        return added

    def _record_triage_decisions(
        self,
        decisions: list[dict[str, Any]],
        *,
        source: str = "triage",
    ) -> None:
        account = self._account_info()
        with connect_sync(self.session_db_path) as conn:
            for item in decisions:
                message_id = str(item.get("message_id") or "")
                if not message_id:
                    continue
                decision_id = f"{account.get('account_id') or 'acct'}:{message_id}:{utcnow().timestamp()}"
                conn.execute(
                    """
                    INSERT INTO gmail_triage_decisions
                    (decision_id, account_id, account_email, message_id, thread_id,
                     sender, sender_email, sender_domain, subject, message_date, snippet,
                     category, confidence, priority, surface_to_user, reason,
                     suggested_action, source, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        decision_id,
                        account.get("account_id"),
                        account.get("account_email"),
                        message_id,
                        item.get("thread_id"),
                        item.get("sender"),
                        item.get("sender_email"),
                        item.get("sender_domain"),
                        item.get("subject"),
                        item.get("date"),
                        item.get("snippet"),
                        item.get("category"),
                        self._float(item.get("confidence"), 0.0),
                        self._bounded_int(item.get("priority"), 0, 0, 100),
                        1 if item.get("surface_to_user") else 0,
                        item.get("reason"),
                        item.get("suggested_action"),
                        source,
                        utcnow().isoformat(),
                    ],
                )
            conn.commit()

    def _recent_triage_decisions(
        self,
        *,
        max_items: int,
        lookback_hours: int,
        surface_only: bool,
    ) -> list[dict[str, Any]]:
        account = self._account_info()
        account_id = str(account.get("account_id") or "").strip()
        account_email = str(account.get("account_email") or "").strip()
        cutoff = (utcnow() - timedelta(hours=max(1, lookback_hours))).isoformat()
        where = ["created_at >= ?"]
        params: list[Any] = [cutoff]
        if account_id or account_email:
            where.append("(account_id = ? OR account_email = ?)")
            params.extend([account_id, account_email])
        if surface_only:
            where.append("surface_to_user = 1")
            where.append("category != 'spam_or_noise'")
        params.append(max(10, max_items * 6))
        with connect_sync(self.session_db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT
                    decision_id, account_id, account_email, message_id, thread_id,
                    sender, sender_email, sender_domain, subject, message_date, snippet,
                    category, confidence, priority, surface_to_user, reason,
                    suggested_action, source, created_at
                FROM gmail_triage_decisions
                WHERE {' AND '.join(where)}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        seen: set[str] = set()
        items: list[dict[str, Any]] = []
        for row in rows:
            message_id = str(row[3] or "")
            if not message_id or message_id in seen:
                continue
            seen.add(message_id)
            item = {
                "decision_id": row[0],
                "account_id": row[1],
                "account_email": row[2],
                "message_id": message_id,
                "thread_id": row[4],
                "sender": row[5],
                "sender_email": row[6],
                "sender_domain": row[7],
                "subject": row[8],
                "date": row[9],
                "snippet": row[10],
                "category": row[11],
                "confidence": row[12],
                "priority": row[13] or 0,
                "surface_to_user": bool(row[14]),
                "reason": row[15],
                "suggested_action": row[16],
                "source": row[17] or "cached_triage_decision",
                "triaged_at": row[18],
            }
            items.append(self._attach_account(item))
            if len(items) >= max_items:
                break
        return self._actionable_items(items, max_items=max_items)

    def _actionable_items(self, raw_items: Any, *, max_items: int) -> list[dict[str, Any]]:
        items = [
            item
            for item in (raw_items or [])
            if isinstance(item, dict)
            and item.get("surface_to_user")
            and item.get("category") != "spam_or_noise"
        ]
        items.sort(
            key=lambda item: (
                int(item.get("priority") or 0),
                float(item.get("confidence") or 0.0),
                str(item.get("date") or item.get("triaged_at") or ""),
            ),
            reverse=True,
        )
        return items[:max(1, max_items)]

    def _bool_input(self, value: Any, *, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        return default

    async def _memory_context_for_messages(self, messages: list[dict[str, Any]]) -> str:
        senders = [
            str(item.get("from_email") or item.get("sender_email") or "").strip()
            for item in messages[:8]
            if str(item.get("from_email") or item.get("sender_email") or "").strip()
        ]
        query_terms: list[str] = []
        for item in messages[:8]:
            if not isinstance(item, dict):
                continue
            for key in (
                "subject",
                "snippet",
                "from",
                "sender",
                "from_email",
                "sender_email",
                "sender_domain",
            ):
                value = str(item.get(key) or "").strip()
                if value and value not in query_terms:
                    query_terms.append(value)
        if not query_terms and senders:
            query_terms.extend(senders[:6])
        if not query_terms or self.memory_read is None:
            return ""
        try:
            result = await self.memory_read.search(
                (
                    "Gmail event context, sender identity, active projects, prior "
                    "discussions, and user preferences for: "
                    + ", ".join(query_terms[:12])
                ),
                max_results=6,
            )
        except Exception:
            return ""
        return self._compact_memory_result(result)

    async def _memory_context_for_thread(self, thread: dict[str, Any] | None) -> str:
        messages = thread.get("messages") if isinstance(thread, dict) else []
        compact = [
            {"from_email": extract_email_address(str(item.get("from") or ""))}
            for item in (messages or [])[-6:]
            if isinstance(item, dict)
        ]
        return await self._memory_context_for_messages(compact)

    def _compact_memory_result(self, payload: dict[str, Any]) -> str:
        items = payload.get("results") or payload.get("memories") or []
        snippets: list[str] = []
        if isinstance(items, list):
            for item in items[:6]:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or "").strip()
                content = str(item.get("content") or item.get("text") or "").strip()
                if title or content:
                    snippets.append(f"{title}: {content[:240]}")
        return "\n".join(snippets)

    def _save_session(self, task: TaskEnvelope, output: dict[str, Any]) -> None:
        account = self._account_info()
        try:
            with connect_sync(self.session_db_path) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO gmail_session_runs
                    (task_id, session_id, intent, account_id, account_email, query, result_summary_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        task.task_id,
                        task.session_id,
                        task.intent,
                        account.get("account_id"),
                        account.get("account_email"),
                        str(task.input.get("query") or task.input.get("q") or ""),
                        json.dumps(self._summary_for_output(output), ensure_ascii=False),
                        utcnow().isoformat(),
                    ],
                )
                conn.commit()
        except Exception:
            logger.warning("gmail_agent.session_save_failed task_id=%s", task.task_id, exc_info=True)

    def _save_history_cursor(self, *, history_id: str, watch_expiration_ms: str = "") -> None:
        account = self._account_info()
        if not account.get("account_id") or not history_id:
            return
        with connect_sync(self.session_db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO gmail_history_cursors
                (account_id, account_email, history_id, watch_expiration_ms, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    account.get("account_id"),
                    account.get("account_email"),
                    history_id,
                    watch_expiration_ms,
                    utcnow().isoformat(),
                ],
            )
            conn.commit()

    def _history_cursor(self) -> dict[str, Any]:
        account = self._account_info()
        account_id = str(account.get("account_id") or "").strip()
        if not account_id:
            return {}
        with connect_sync(self.session_db_path) as conn:
            row = conn.execute(
                """
                SELECT account_id, account_email, history_id, watch_expiration_ms, updated_at
                FROM gmail_history_cursors
                WHERE account_id = ?
                """,
                [account_id],
            ).fetchone()
            return dict(row) if row else {}

    def _clear_history_cursor(self) -> None:
        account = self._account_info()
        account_id = str(account.get("account_id") or "").strip()
        if not account_id:
            return
        with connect_sync(self.session_db_path) as conn:
            conn.execute("DELETE FROM gmail_history_cursors WHERE account_id = ?", [account_id])
            conn.commit()

    def _summary_for_output(self, output: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": output.get("status"),
            "account": output.get("account"),
            "count": output.get("count") or len(output.get("items") or output.get("messages") or []),
            "reason": output.get("reason"),
            "draft_id": output.get("draft_id"),
        }

    async def _maybe_create_plan(self, task: TaskEnvelope, steps: list[str]) -> None:
        if self.step_plan is None or len(steps) < 3:
            return
        try:
            await self.step_plan.create(steps)
        except Exception:
            logger.debug("gmail_agent.step_plan_create_failed task_id=%s", task.task_id, exc_info=True)

    async def _maybe_step(self, step: int, status: str, message: str) -> None:
        if self.step_plan is None:
            return
        try:
            await self.step_plan.update(step, status, message)
        except Exception:
            logger.debug("gmail_agent.step_plan_update_failed", exc_info=True)

    def _account_info(self) -> dict[str, Any]:
        auth = self.auth if isinstance(self.auth, dict) else {}
        return {
            "account_id": auth.get("account_id"),
            "account_email": auth.get("account_email"),
            "account_label": auth.get("account_label") or auth.get("account_display_name") or auth.get("account_email"),
            "account_display_name": auth.get("account_display_name"),
            "account_is_primary": bool(auth.get("account_is_primary")),
        }

    def _attach_account(self, item: dict[str, Any], task: TaskEnvelope | None = None) -> dict[str, Any]:
        del task
        return {**item, **self._account_info()}

    def _compact_thread(self, thread: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(thread, dict):
            return {}
        messages = thread.get("messages") or []
        return {
            "thread_id": thread.get("thread_id"),
            "message_count": len(messages),
            "messages": [
                compact_message_for_llm(item, max_body_chars=1200)
                for item in messages[-8:]
                if isinstance(item, dict)
            ],
        }

    def _reply_subject(self, thread: dict[str, Any] | None) -> str:
        messages = thread.get("messages") if isinstance(thread, dict) else []
        latest = messages[-1] if messages else {}
        subject = str((latest or {}).get("subject") or "").strip()
        if subject and not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        return subject

    def _latest_header(self, thread: dict[str, Any] | None, key: str) -> str:
        messages = thread.get("messages") if isinstance(thread, dict) else []
        latest = messages[-1] if messages else {}
        return str((latest or {}).get(key) or "").strip()

    def _err(self, code: str, message: str, retryable: bool, next_action: str) -> AgentResult:
        return AgentResult(
            status="failed",
            output={},
            artifacts=[],
            error=AgentError(code=code, retryable=retryable, message=message, next_action=next_action),
        )

    def _bounded_int(self, value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    def _float(self, value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _string_list(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    def _json_loads(self, value: str, default: Any) -> Any:
        try:
            return json.loads(value)
        except Exception:
            return default
