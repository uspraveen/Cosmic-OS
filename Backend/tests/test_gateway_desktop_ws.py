from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.adapters.response_processor import DirectRouteHandoff
from gateway.channels.base import ChannelUnavailableError, RetryableDeliveryError
from gateway.channels.desktop import DesktopAdapter
from gateway.channels.mobile import MobileAdapter
from gateway.channels.routes import router as channel_router
from gateway.config import GatewayConfig
from gateway.memory_client import MemoryClientHTTPError, MemoryPromptContext
from gateway.scheduler import CronExpressionError, compute_next_fire_at
from gateway.runtime import ActiveRequest, SYSTEM_CRON_DAILY_ROLLOVER, GatewayRuntime
from gateway.session_store import utcnow_iso
from shared import AgentEmailIntegrationStore


class FakeDirectAdapter:
    def __init__(self, route: str) -> None:
        self.route = route
        self.last_memory_context: str | None = None
        self.api_key = "test-anthropic-key"
        self.model = "claude-haiku-4-5"

    async def close(self) -> None:
        return

    async def stream(
        self,
        *,
        request_id: str,
        session_id: str,
        history,
        send,
        store_assistant_message,
        channel: str,
        memory_context: str | None = None,
        usage_recorder=None,
    ) -> None:
        self.last_memory_context = memory_context
        assert history[-1]["role"] == "user"
        await send(
            {
                "type": "response.chunk",
                "request_id": request_id,
                "session_id": session_id,
                "content": "Hello",
                "done": False,
            }
        )
        await send(
            {
                "type": "response.complete",
                "request_id": request_id,
                "session_id": session_id,
                "content": "Hello from fake adapter",
                "route": self.route,
                "awaiting_reply": False,
                "metrics": {"rtt_ms": 12},
            }
        )
        store_assistant_message(
            "Hello from fake adapter",
            awaiting_reply=False,
            metadata=None,
            channel=channel,
            route=self.route,
        )

    async def generate_text(
        self,
        *,
        system_prompt: str,
        messages,
        max_tokens: int,
    ) -> tuple[str, dict[str, object], str]:
        return (
            "## Summary\n- Daily summary generated for testing.\n",
            {"output_tokens": 64},
            "end_turn",
        )


class FakeHandoffDirectAdapter:
    def __init__(self, route: str, *, handoff_route: str = "opus") -> None:
        self.route = route
        self.handoff_route = handoff_route
        self.last_memory_context: str | None = None

    async def close(self) -> None:
        return

    async def stream(
        self,
        *,
        request_id: str,
        session_id: str,
        history,
        send,
        store_assistant_message,
        channel: str,
        memory_context: str | None = None,
        usage_recorder=None,
    ) -> None:
        self.last_memory_context = memory_context
        assert history[-1]["role"] == "user"
        raise DirectRouteHandoff(self.handoff_route)

    async def generate_text(
        self,
        *,
        system_prompt: str,
        messages,
        max_tokens: int,
    ) -> tuple[str, dict[str, object], str]:
        raise AssertionError("generate_text should not be used in handoff tests")


class FakeOrchestratorClient:
    def __init__(self) -> None:
        self.last_task = None

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def stream_task(self, task) -> object:
        self.last_task = task
        yield {
            "type": "task.created",
            "task_id": task.task_id,
            "session_id": task.session_id,
            "channel": task.channel,
            "route": "opus",
            "status": "running",
        }
        yield {
            "type": "response.thinking.chunk",
            "task_id": task.task_id,
            "request_id": task.input.get("request_id"),
            "session_id": task.session_id,
            "channel": task.channel,
            "content": "Let me think this through.",
            "done": False,
        }
        yield {
            "type": "response.chunk",
            "task_id": task.task_id,
            "request_id": task.input.get("request_id"),
            "session_id": task.session_id,
            "channel": task.channel,
            "content": "Thin Opus answer",
            "done": False,
        }
        yield {
            "type": "response.complete",
            "task_id": task.task_id,
            "request_id": task.input.get("request_id"),
            "session_id": task.session_id,
            "channel": task.channel,
            "content": "Thin Opus answer",
            "route": "opus",
            "awaiting_reply": True,
            "metrics": {"rtt_ms": 32},
        }
        yield {
            "type": "task.completed",
            "task_id": task.task_id,
            "session_id": task.session_id,
            "channel": task.channel,
            "route": "opus",
            "status": "completed",
        }

    async def list_active_tasks(self, *, session_id: str | None = None, channel: str | None = None) -> list[dict]:
        return []

    async def cancel_task(self, task_id: str) -> bool:
        return False


class FakeHeartbeatNoopOrchestratorClient(FakeOrchestratorClient):
    async def stream_task(self, task) -> object:
        self.last_task = task
        yield {
            "type": "task.created",
            "task_id": task.task_id,
            "session_id": task.session_id,
            "channel": task.channel,
            "route": "opus",
            "status": "running",
        }
        yield {
            "type": "response.chunk",
            "task_id": task.task_id,
            "request_id": task.input.get("request_id"),
            "session_id": task.session_id,
            "channel": task.channel,
            "content": "heartbeat_ok",
            "done": False,
        }
        yield {
            "type": "response.complete",
            "task_id": task.task_id,
            "request_id": task.input.get("request_id"),
            "session_id": task.session_id,
            "channel": task.channel,
            "content": "heartbeat_ok",
            "route": "opus",
            "awaiting_reply": False,
            "metrics": {"rtt_ms": 24},
        }
        yield {
            "type": "task.completed",
            "task_id": task.task_id,
            "session_id": task.session_id,
            "channel": task.channel,
            "route": "opus",
            "status": "completed",
        }


class FakeHeartbeatNoteOrchestratorClient(FakeOrchestratorClient):
    def __init__(self, note: str) -> None:
        super().__init__()
        self.note = note

    async def stream_task(self, task) -> object:
        self.last_task = task
        yield {
            "type": "task.created",
            "task_id": task.task_id,
            "session_id": task.session_id,
            "channel": task.channel,
            "route": "opus",
            "status": "running",
        }
        yield {
            "type": "task.progress",
            "task_id": task.task_id,
            "request_id": task.input.get("request_id"),
            "session_id": task.session_id,
            "channel": task.channel,
            "route": "opus",
            "status": "thinking",
            "message": "Checking heartbeat context.",
        }
        yield {
            "type": "response.complete",
            "task_id": task.task_id,
            "request_id": task.input.get("request_id"),
            "session_id": task.session_id,
            "channel": task.channel,
            "content": self.note,
            "route": "opus",
            "awaiting_reply": False,
            "metrics": {"rtt_ms": 24},
        }
        yield {
            "type": "task.completed",
            "task_id": task.task_id,
            "session_id": task.session_id,
            "channel": task.channel,
            "route": "opus",
            "status": "completed",
        }


class CapturingDesktopAdapter(DesktopAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def broadcast_to_session(self, session_id: str, event: dict[str, Any]) -> None:
        self.events.append((session_id, event))


class CapturingMobileAdapter(MobileAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def broadcast_to_session(self, session_id: str, event: dict[str, Any]) -> None:
        self.events.append((session_id, event))


class FakeResearchOrchestratorClient(FakeOrchestratorClient):
    async def stream_task(self, task) -> object:
        self.last_task = task
        yield {
            "type": "task.created",
            "task_id": task.task_id,
            "session_id": task.session_id,
            "channel": task.channel,
            "route": "opus",
            "status": "running",
        }
        yield {
            "type": "response.complete",
            "task_id": task.task_id,
            "request_id": task.input.get("request_id"),
            "session_id": task.session_id,
            "channel": task.channel,
            "content": "Here is the grounded answer.",
            "route": "opus",
            "awaiting_reply": False,
            "metrics": {"rtt_ms": 48},
            "research_provenance": {
                "paths": ["native_web_search"],
                "source_count": 2,
                "source_domains": ["cursor.com", "techcrunch.com"],
                "source_sample": [
                    {
                        "url": "https://cursor.com/blog/composer-2",
                        "title": "Cursor Composer 2",
                        "domain": "cursor.com",
                    },
                    {
                        "url": "https://techcrunch.com/cursor-composer-2",
                        "title": "TechCrunch coverage",
                        "domain": "techcrunch.com",
                    },
                ],
            },
            "sources": [
                {"url": "https://cursor.com/blog/composer-2", "title": "Cursor Composer 2", "domain": "cursor.com"},
                {"url": "https://techcrunch.com/cursor-composer-2", "title": "TechCrunch coverage", "domain": "techcrunch.com"},
            ],
            "specialist_receipts": [
                {
                    "tool_name": "x_search",
                    "intent": "x.search",
                    "agent_id": "cosmic/x-twitter-search-agent:1.0.0",
                    "agent_label": "x twitter search agent",
                    "activity": "delegated x.search to x twitter search agent and found recent source-backed X results",
                    "source_count": 2,
                    "source_domains": ["x.com"],
                    "source_sample": [
                        {
                            "url": "https://x.com/mntruell/status/123",
                            "title": "@mntruell on X",
                            "domain": "x.com",
                        }
                    ],
                }
            ],
        }
        yield {
            "type": "task.completed",
            "task_id": task.task_id,
            "session_id": task.session_id,
            "channel": task.channel,
            "route": "opus",
            "status": "completed",
        }


class FakeCancellableDirectAdapter:
    def __init__(self, route: str) -> None:
        self.route = route
        self.started = asyncio.Event()
        self.last_memory_context: str | None = None

    async def close(self) -> None:
        return

    async def stream(
        self,
        *,
        request_id: str,
        session_id: str,
        history,
        send,
        store_assistant_message,
        channel: str,
        memory_context: str | None = None,
        usage_recorder=None,
    ) -> None:
        self.last_memory_context = memory_context
        assert history[-1]["role"] == "user"
        self.started.set()
        await send(
            {
                "type": "response.chunk",
                "request_id": request_id,
                "session_id": session_id,
                "content": "Partial answer",
                "done": False,
            }
        )
        await asyncio.sleep(60)


class FakeCancellableOrchestratorClient:
    def __init__(self) -> None:
        self._cancellations: dict[str, asyncio.Event] = {}

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def stream_task(self, task) -> object:
        cancel_event = asyncio.Event()
        self._cancellations[task.task_id] = cancel_event
        try:
            yield {
                "type": "task.created",
                "task_id": task.task_id,
                "request_id": task.input.get("request_id"),
                "session_id": task.session_id,
                "channel": task.channel,
                "route": "opus",
                "status": "running",
            }
            await cancel_event.wait()
            yield {
                "type": "task.cancelled",
                "task_id": task.task_id,
                "request_id": task.input.get("request_id"),
                "session_id": task.session_id,
                "channel": task.channel,
                "route": "opus",
                "status": "cancelled",
                "message": "Response stopped.",
            }
        finally:
            self._cancellations.pop(task.task_id, None)

    async def list_active_tasks(self, *, session_id: str | None = None, channel: str | None = None) -> list[dict]:
        return []

    async def cancel_task(self, task_id: str) -> bool:
        cancel_event = self._cancellations.get(task_id)
        if cancel_event is None:
            return False
        cancel_event.set()
        return True


class FakeWhatsAppChannelAdapter:
    platform = "whatsapp"

    def __init__(self) -> None:
        self.sent_events: list[dict[str, object]] = []
        self.allowed_phone = "+12153079021"
        self.connected = True

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def on_message(self, callback) -> None:
        self._callback = callback

    def normalize_message(self, payload: dict[str, object]) -> dict[str, object]:
        sender = payload.get("sender")
        phone = None
        if isinstance(sender, dict):
            phone = sender.get("phone")
        return {
            "content": str(payload.get("text") or ""),
            "session_id": None,
            "channel": f"whatsapp:{phone or '+15551234567'}",
            "metadata": {
                "platform": "whatsapp",
                "message_id": "wamid_test_1",
            },
        }

    async def send(self, message: dict[str, object], channel: str | None = None) -> None:
        self.sent_events.append(
            {
                **message,
                "channel": channel or message.get("channel"),
            }
        )

    async def get_status(self) -> dict[str, object]:
        return {
            "connected": self.connected,
        }

    async def get_config(self) -> dict[str, object]:
        return {
            "allowed_phone": self.allowed_phone,
            "self_chat_only": False,
        }

    async def update_config(
        self,
        *,
        allowed_phone: str | None = None,
        self_chat_only: bool | None = None,
    ) -> dict[str, object]:
        if allowed_phone is not None:
            self.allowed_phone = allowed_phone
        return {
            "allowed_phone": self.allowed_phone,
            "self_chat_only": bool(self_chat_only),
        }

    async def download_media(self, bridge_media_ref: str) -> tuple[bytes, str | None]:
        return (f"whatsapp:{bridge_media_ref}".encode("utf-8"), "image/jpeg")


class FakeTelegramChannelAdapter:
    platform = "telegram"

    def __init__(self) -> None:
        self.sent_events: list[dict[str, object]] = []
        self.allowed_chat_id = 12345
        self.allowed_user_id = 12345

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def on_message(self, callback) -> None:
        self._callback = callback

    def verify_webhook_secret(self, provided_secret: str | None) -> None:
        if (provided_secret or "").strip() != "telegram-secret":
            raise PermissionError("Invalid Telegram webhook secret token")

    def normalize_message(self, payload: dict[str, object]) -> dict[str, object] | None:
        message = payload.get("message")
        if not isinstance(message, dict):
            return None
        chat = message.get("chat")
        sender = message.get("from")
        if not isinstance(chat, dict) or not isinstance(sender, dict):
            return None
        if chat.get("type") != "private":
            return None
        chat_id = int(chat.get("id") or 0)
        return {
            "content": str(message.get("text") or ""),
            "session_id": None,
            "channel": f"telegram:chat_{chat_id}",
            "metadata": {
                "platform": "telegram",
                "chat_id": chat_id,
                "user_id": int(sender.get("id") or 0),
                "message_id": int(message.get("message_id") or 0),
            },
        }

    async def send(self, message: dict[str, object], channel: str | None = None) -> None:
        self.sent_events.append(
            {
                **message,
                "channel": channel or message.get("channel"),
            }
        )

    async def get_status(self) -> dict[str, object]:
        return {
            "status": "connected",
            "allowed_chat_id": self.allowed_chat_id,
            "allowed_user_id": self.allowed_user_id,
        }

    async def sync_webhook(self) -> dict[str, object]:
        return {"url": "https://example.com/channels/telegram/webhook"}

    async def delete_webhook(self, *, drop_pending_updates: bool = False) -> dict[str, object]:
        return {"url": "", "drop_pending_updates": drop_pending_updates}

    async def send_test_message(self, *, chat_id: int, message: str) -> dict[str, object]:
        return {"chat_id": chat_id, "text": message}

    async def download_file(self, file_id: str) -> tuple[bytes, str | None]:
        return (b"telegram-media", "image/jpeg")


class FakeAgentEmailChannelAdapter:
    platform = "agent-email"

    def __init__(self) -> None:
        self.sent_events: list[dict[str, object]] = []
        self.primary_mailbox_address = "assistant@example.com"

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def on_message(self, callback) -> None:
        self._callback = callback

    async def send(self, message: dict[str, object], channel: str | None = None) -> None:
        self.sent_events.append(
            {
                **message,
                "channel": channel or message.get("channel"),
            }
        )

    async def get_status(self) -> dict[str, object]:
        return {
            "connected": True,
            "primary_mailbox_address": self.primary_mailbox_address,
        }


class FlakyWhatsAppChannelAdapter(FakeWhatsAppChannelAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.fail_response_complete = True

    async def send(self, message: dict[str, object], channel: str | None = None) -> None:
        if self.fail_response_complete and message.get("type") == "response.complete":
            raise ChannelUnavailableError("bridge temporarily unavailable")
        await super().send(message, channel=channel)


class FlakyDesktopChannelAdapter:
    platform = "desktop"

    def __init__(self) -> None:
        self.sent_events: list[dict[str, object]] = []
        self.available = False

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def on_message(self, callback) -> None:
        self._callback = callback

    async def send(self, message: dict[str, object], channel: str | None = None) -> None:
        if not self.available:
            raise ChannelUnavailableError("desktop socket offline")
        self.sent_events.append({**message, "channel": channel or message.get("channel")})


class RetryableDesktopChannelAdapter(FlakyDesktopChannelAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.available = True
        self.fail_once = True

    async def send(self, message: dict[str, object], channel: str | None = None) -> None:
        if self.fail_once:
            self.fail_once = False
            raise RetryableDeliveryError("desktop write timed out")
        await super().send(message, channel=channel)


class FakeMemoryClient:
    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = enabled
        self.started = False
        self.prompt_context_calls: list[dict[str, object]] = []
        self.search_calls: list[dict[str, object]] = []
        self.active_search_calls: list[dict[str, object]] = []
        self.memory_get_requests: list[str] = []
        self.write_calls: list[dict[str, object]] = []
        self.core_fact_write_calls: list[dict[str, object]] = []
        self.episode_calls: list[dict[str, object]] = []
        self.core_fact_requests: list[int] = []
        self.graph_sync_calls: list[dict[str, object]] = []
        self.graph_rebuild_calls: list[dict[str, object]] = []

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def health(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "status": "ok" if self.enabled else "disabled",
            "graph_enabled": self.enabled,
            "graph_backend": "memory" if self.enabled else None,
        }

    async def build_prompt_context(
        self,
        *,
        query: str,
        max_results: int,
        token_budget: int,
        core_fact_max_chars: int,
        kinds: tuple[str, ...],
        include_diagnostics: bool = False,
    ) -> MemoryPromptContext:
        self.prompt_context_calls.append(
            {
                "query": query,
                "max_results": max_results,
                "token_budget": token_budget,
                "core_fact_max_chars": core_fact_max_chars,
                "kinds": kinds,
                "include_diagnostics": include_diagnostics,
            }
        )
        if not self.enabled:
            return MemoryPromptContext()
        return MemoryPromptContext(
            core_fact_items=[
                {
                    "memory_id": "mem_core_pref",
                    "title": "Response style",
                    "content": "User prefers concise technical answers.",
                    "canonical_key": "preferences.response_style",
                    "priority": 100,
                    "always_include": True,
                    "tags": ["preferences"],
                }
            ],
            core_facts_rendered="- User prefers concise technical answers.",
            recall_items=[
                {
                    "memory_id": "mem_task_1",
                    "kind": "task_summary",
                    "title": "Memory integration work",
                    "content": "We are integrating cosmic-memory into Gateway and should keep the runtime HTTP boundary internal-only.",
                    "source_kind": "gateway",
                }
            ],
            total_token_count=42,
            rendered=(
                "Relevant long-term memory context for this request.\n"
                "Always-on core facts:\n"
                "- [key=preferences.response_style] Response style: User prefers concise technical answers.\n\n"
                "Retrieved long-term memories:\n"
                "1. [task_summary] Memory integration work (source=gateway)\n"
                "We are integrating cosmic-memory into Gateway and should keep the runtime HTTP boundary internal-only."
            ),
        )

    async def passive_search(self, payload: dict[str, object]) -> dict[str, object]:
        self.search_calls.append(payload)
        return {
            "items": [
                {
                    "memory_id": "mem_search_1",
                    "kind": "task_summary",
                    "title": "Search hit",
                    "content": "Gateway should use passive recall.",
                    "score": 0.91,
                }
            ],
            "total_token_count": 32,
        }

    async def active_search(self, payload: dict[str, object]) -> dict[str, object]:
        self.active_search_calls.append(payload)
        return {
            "items": [],
            "entities": [],
            "relations": [],
            "episodes": [],
            "search_plan": [],
        }

    async def get_memory(self, memory_id: str) -> dict[str, object]:
        self.memory_get_requests.append(memory_id)
        return {
            "memory_id": memory_id,
            "kind": "task_summary",
            "title": "Detailed memory block",
            "content": "This is the full canonical memory body.",
            "metadata": {"task_id": "tsk_1"},
            "provenance": {"source_kind": "gateway"},
            "status": "active",
            "version": 1,
            "created_at": "2026-03-15T00:00:00Z",
            "updated_at": "2026-03-15T00:00:00Z",
        }

    async def write_memory(self, payload: dict[str, object]) -> dict[str, object]:
        self.write_calls.append(payload)
        return {"memory_id": "mem_write_1"}

    async def write_core_fact(self, payload: dict[str, object]) -> dict[str, object]:
        self.core_fact_write_calls.append(payload)
        return {"memory_id": "mem_core_1"}

    async def ingest_episode(self, payload: dict[str, object]) -> dict[str, object]:
        self.episode_calls.append(payload)
        return {
            "record": {"memory_id": "mem_episode_1"},
            "observation_count": len(payload.get("observations", [])) if isinstance(payload, dict) else 0,
            "graph_episode_id": "ep_1",
        }

    async def get_core_fact_block(self, *, max_chars: int = 1500) -> dict[str, object]:
        self.core_fact_requests.append(max_chars)
        return {
            "items": [
                {
                    "memory_id": "mem_core_pref",
                    "title": "Response style",
                    "content": "User prefers concise technical answers.",
                    "canonical_key": "preferences.response_style",
                }
            ],
            "rendered": "- User prefers concise technical answers.",
        }

    def render_prompt_context(
        self,
        *,
        core_fact_items: list[dict[str, object]] | None = None,
        core_facts_rendered: str = "",
        recall_items: list[dict[str, object]] | None = None,
    ) -> str:
        rendered_lines = [
            "Relevant long-term memory context for this request.",
            "Always-on core facts:",
        ]
        if core_fact_items:
            for item in core_fact_items:
                key = str(item.get("canonical_key") or "").strip()
                prefix = f"- [key={key}] " if key else "- "
                title = str(item.get("title") or "").strip()
                content = str(item.get("content") or "").strip()
                if title and title.lower() not in content.lower():
                    rendered_lines.append(f"{prefix}{title}: {content}")
                else:
                    rendered_lines.append(f"{prefix}{content}")
        elif core_facts_rendered:
            rendered_lines.append(core_facts_rendered)
        recall_items = recall_items or []
        if recall_items:
            rendered_lines.extend(["", "Retrieved long-term memories:"])
            for index, item in enumerate(recall_items, start=1):
                heading = f"{index}. [{item.get('kind') or 'memory'}] {item.get('title') or ''}".rstrip()
                source_kind = str(item.get("source_kind") or "").strip()
                canonical_key = str(item.get("canonical_key") or "").strip()
                details = []
                if source_kind:
                    details.append(f"source={source_kind}")
                if canonical_key:
                    details.append(f"key={canonical_key}")
                if details:
                    heading += f" ({', '.join(details)})"
                rendered_lines.append(heading)
                content = str(item.get("content") or "").strip()
                if content:
                    rendered_lines.append(content)
        return "\n".join(rendered_lines)

    def render_core_fact_block(
        self,
        *,
        core_fact_items: list[dict[str, object]] | None = None,
        core_facts_rendered: str = "",
    ) -> str:
        if core_fact_items:
            lines: list[str] = []
            for item in core_fact_items:
                key = str(item.get("canonical_key") or "").strip()
                prefix = f"- [key={key}] " if key else "- "
                title = str(item.get("title") or "").strip()
                content = str(item.get("content") or "").strip()
                if title and title.lower() not in content.lower():
                    lines.append(f"{prefix}{title}: {content}")
                else:
                    lines.append(f"{prefix}{content}")
            return "\n".join(lines)
        return core_facts_rendered

    async def index_status(self) -> dict[str, object]:
        return {"enabled": self.enabled}

    async def index_sync(self) -> dict[str, object]:
        return {"enabled": self.enabled, "mode": "sync"}

    async def index_rebuild(self) -> dict[str, object]:
        return {"enabled": self.enabled, "mode": "rebuild"}

    async def graph_status(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "backend": "memory" if self.enabled else None,
            "ingested_memory_count": 1 if self.enabled else 0,
        }

    async def graph_sync(self, payload: dict[str, object] | None = None) -> dict[str, object]:
        self.graph_sync_calls.append(payload or {})
        return {
            "enabled": self.enabled,
            "backend": "memory" if self.enabled else None,
            "mode": "sync",
        }

    async def graph_rebuild(self, payload: dict[str, object] | None = None) -> dict[str, object]:
        self.graph_rebuild_calls.append(payload or {})
        return {
            "enabled": self.enabled,
            "backend": "memory" if self.enabled else None,
            "mode": "rebuild",
        }


class FakeRedis:
    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.groups: dict[tuple[str, str], dict[str, object]] = {}
        self.values: dict[str, object] = {}
        self._counter = 0

    async def xgroup_create(self, stream: str, group: str, id: str = "0", mkstream: bool = True) -> None:
        if mkstream:
            self.streams.setdefault(stream, [])
        self.groups.setdefault((stream, group), {"delivered": set(), "acked": set()})

    async def xadd(self, stream: str, fields: dict[str, str]) -> str:
        self._counter += 1
        message_id = f"{self._counter}-0"
        self.streams.setdefault(stream, []).append((message_id, dict(fields)))
        return message_id

    async def xreadgroup(
        self,
        *,
        groupname: str,
        consumername: str,
        streams: dict[str, str],
        count: int | None = None,
        block: int | None = None,
    ) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        del consumername
        results: list[tuple[str, list[tuple[str, dict[str, str]]]]] = []
        limit = count or 10**9
        for stream, start in streams.items():
            state = self.groups.setdefault((stream, groupname), {"delivered": set(), "acked": set()})
            delivered = state["delivered"]
            acked = state["acked"]
            messages: list[tuple[str, dict[str, str]]] = []
            for message_id, payload in self.streams.get(stream, []):
                if start == ">":
                    if message_id in delivered:
                        continue
                    delivered.add(message_id)
                    messages.append((message_id, payload))
                elif start == "0":
                    if message_id in delivered and message_id not in acked:
                        messages.append((message_id, payload))
                else:
                    raise AssertionError(f"Unsupported xreadgroup start value: {start}")
                if len(messages) >= limit:
                    break
            if messages:
                results.append((stream, messages))
        if not results and block:
            await asyncio.sleep(min(block / 1000.0, 0.02))
        return results

    async def xack(self, stream: str, group: str, message_id: str) -> int:
        state = self.groups.setdefault((stream, group), {"delivered": set(), "acked": set()})
        state["acked"].add(message_id)
        return 1

    async def get(self, key: str) -> object | None:
        return self.values.get(key)

    async def set(self, key: str, value: object, ex: int | None = None) -> bool:
        del ex
        self.values[key] = value
        return True

    async def incr(self, key: str) -> int:
        current = int(self.values.get(key) or 0)
        current += 1
        self.values[key] = current
        return current

    async def expire(self, key: str, seconds: int) -> bool:
        del key, seconds
        return True

    async def aclose(self) -> None:
        return


def build_runtime(tmp_path, *, route: str = "haiku") -> GatewayRuntime:
    runtime = GatewayRuntime(
        GatewayConfig(
            local_api_token="test-token",
            internal_token="internal-token",
            signing_secret="signing-secret",
            model_router_url="http://127.0.0.1:9999",
            orchestrator_url="http://127.0.0.1:8743",
            enable_whatsapp=False,
            preferences_db_path=tmp_path / "preferences.db",
            sessions_db_path=tmp_path / "sessions.db",
            mobile_devices_db_path=tmp_path / "mobile_devices.db",
            routing_audit_db_path=tmp_path / "routing_audit.db",
            artifacts_db_path=tmp_path / "artifacts.db",
            delivery_queue_db_path=tmp_path / "delivery_queue.db",
            scheduler_db_path=tmp_path / "scheduler.db",
            heartbeat_notes_path=tmp_path / "heartbeat_notes.md",
            memory_write_audit_db_path=tmp_path / "memory_write_audit.db",
        )
    )

    async def fake_start() -> None:
        return

    async def fake_stop() -> None:
        return

    async def fake_classify(
        *,
        query: str,
        conversation_context,
        memory_context: str | None = None,
        max_completion_tokens: int = 430,
    ) -> dict:
        return {
            "route": route,
            "needs_latest": False,
            "needs_citations": False,
            "is_task": route == "opus",
            "is_continuation": route == "opus",
            "confidence": 0.91,
            "signals": ["test"],
        }

    async def fake_classify_with_metadata(
        *,
        query: str,
        conversation_context,
        memory_context: str | None = None,
        max_completion_tokens: int = 430,
    ) -> dict:
        return {
            "classification": await fake_classify(
                query=query,
                conversation_context=conversation_context,
                memory_context=memory_context,
                max_completion_tokens=max_completion_tokens,
            ),
            "metrics": {"rtt_ms": 18.5},
            "classifier_model": "openai/gpt-oss-20b",
            "raw_classifier_output": '{"route":"%s"}' % route,
            "http2_enabled": True,
        }

    runtime.model_router.start = fake_start
    runtime.model_router.stop = fake_stop
    runtime.model_router.classify = fake_classify
    runtime.model_router.classify_with_metadata = fake_classify_with_metadata
    runtime.haiku_adapter = FakeDirectAdapter("haiku")
    runtime.perplexity_adapter = FakeDirectAdapter("perplexity")
    runtime.orchestrator = FakeOrchestratorClient()
    runtime.memory_client = FakeMemoryClient(enabled=False)
    runtime._artifact_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=b"mock-artifact-bytes",
                headers={"content-type": "image/jpeg"},
            )
        )
    )
    return runtime


def test_alpha_harness_completion_progress_keeps_activity_stage(tmp_path) -> None:
    runtime = build_runtime(tmp_path)

    message = runtime._specialist_progress_message(  # noqa: SLF001 - targeted status formatting
        event_type="task.progress",
        payload={"stage": "alpha.cursor.completed"},
        agent_label="alpha agent",
        intent="alpha.execute",
    )
    assert message == "Alpha agent completed alpha.execute."

    activity = runtime._build_task_activity_entry(  # noqa: SLF001 - targeted activity normalization
        {
            "type": "task.progress",
            "status": "specialist_progress",
            "message": message,
            "stage": "alpha.cursor.completed",
            "specialist": {
                "task_id": "tsk_alpha_123",
                "agent_id": "cosmic/alpha-agent:1.0.0",
                "agent_label": "alpha agent",
                "intent": "alpha.execute",
                "event_type": "task.progress",
            },
        }
    )

    assert activity is not None
    assert activity["stage"] == "alpha.cursor.completed"
    assert activity["specialist_task_id"] == "tsk_alpha_123"


def test_gateway_partial_stream_inserts_boundary_between_text_turns(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    state = ActiveRequest(
        request_id="req_stream_join",
        session_id="sess_stream_join",
        channel="desktop:desk_a",
        route="opus",
        task_id="tsk_stream_join",
        created_at=utcnow_iso(),
    )

    runtime._track_partial_stream(  # noqa: SLF001 - targeted stream cache behavior
        state,
        {"type": "response.chunk", "content": "Let me grab the artifact!"},
    )
    runtime._track_partial_stream(  # noqa: SLF001 - targeted stream cache behavior
        state,
        {"type": "response.chunk", "content": "Got it - firing Alpha now."},
    )

    assert state.partial_content == "Let me grab the artifact!\n\nGot it - firing Alpha now."


def test_backgrounded_request_context_gets_model_separator(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    session_id = runtime._current_session_id()
    runtime.session_store.append_message(
        session_id,
        role="user",
        content="The marquee was needed but in a better design I believe!?",
        channel="desktop:desk_a",
        metadata={"request_id": "req_background_marquee"},
    )
    runtime.active_requests["req_background_marquee"] = ActiveRequest(
        request_id="req_background_marquee",
        session_id=session_id,
        channel="desktop:desk_a",
        route="opus",
        task_id="tsk_background_marquee",
        foreground=False,
        user_query_excerpt="The marquee was needed but in a better design I believe!?",
    )

    context = runtime._build_conversation_context(session_id)  # noqa: SLF001 - targeted context shaping

    assert [item["role"] for item in context] == ["user", "assistant"]
    assert "already running as a background task" in context[1]["content"]
    assert "Do not resume" in context[1]["content"]


def test_backgrounded_task_notebook_is_not_rendered_as_active_workstream(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    session_id = runtime._current_session_id()
    runtime.session_store.upsert_task_notebook(
        "tsk_background_marquee",
        session_id,
        {
            "task_id": "tsk_background_marquee",
            "status": "active",
            "goal": "Improve the marquee design.",
            "current_state": "Alpha is editing the site.",
            "created_at": utcnow_iso(),
        },
    )
    runtime.active_requests["req_background_marquee"] = ActiveRequest(
        request_id="req_background_marquee",
        session_id=session_id,
        channel="desktop:desk_a",
        route="opus",
        task_id="tsk_background_marquee",
        foreground=False,
    )

    working_set = runtime._refresh_active_working_set(session_id)  # noqa: SLF001 - targeted working-set shaping

    assert "tsk_background_marquee" not in (working_set.get("active_task_refs") or [])
    assert "Improve the marquee design." not in (working_set.get("active_workstreams") or [])


@pytest.mark.asyncio
async def test_runtime_uses_shared_daily_session_across_channels(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    await runtime.start()
    try:
        desktop_result = await runtime.process_incoming_user_message(
            {
                "content": "first desktop message",
                "channel": "desktop:desk_a",
                "metadata": {"platform": "desktop"},
            }
        )
        whatsapp_result = await runtime.process_incoming_user_message(
            {
                "content": "follow up from whatsapp",
                "channel": "whatsapp:+15551234567",
                "metadata": {"platform": "whatsapp"},
            }
        )

        assert desktop_result["session_id"] == whatsapp_result["session_id"]
        assert desktop_result["session_id"].startswith("sess_")

        history_tail = runtime.session_store.get_history_tail(desktop_result["session_id"])
        assert [item["channel"] for item in history_tail] == [
            "desktop:desk_a",
            "whatsapp:+15551234567",
        ]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_broadcasts_cross_channel_attachment_metadata_to_desktop(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    await runtime.start()
    try:
        desktop_adapter = CapturingDesktopAdapter()
        runtime.registry.register(desktop_adapter)
        attachments = [
            {
                "id": "att_1",
                "kind": "document",
                "mime_type": "application/pdf",
                "filename": "newsletter.pdf",
                "size_bytes": 2048,
            }
        ]
        input_artifacts = [
            {
                "artifact_id": "art_1",
                "kind": "document",
                "mime": "application/pdf",
                "filename": "newsletter.pdf",
                "size_bytes": 2048,
                "path": "runs/artifacts/req_ingest_x/inputs/art_1/original/newsletter.pdf",
            }
        ]
        await runtime._broadcast_cross_channel_to_desktop(  # noqa: SLF001 - intentional unit seam
            "sess_test",
            role="user",
            content="[document: newsletter.pdf]",
            channel="whatsapp:+12153079021",
            attachments=attachments,
            input_artifacts=input_artifacts,
        )
        assert len(desktop_adapter.events) == 1
        session_id, event = desktop_adapter.events[0]
        assert session_id == "sess_test"
        assert event["attachments"] == attachments
        assert event["input_artifacts"] == input_artifacts
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_broadcasts_desktop_messages_to_mobile_clients(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    await runtime.start()
    try:
        mobile_adapter = CapturingMobileAdapter()
        runtime.registry.register(mobile_adapter)
        await runtime._broadcast_cross_channel_to_realtime_clients(  # noqa: SLF001 - intentional unit seam
            "sess_test",
            role="assistant",
            content="Desktop-originated assistant message",
            channel="desktop:desk_a",
            route="haiku",
        )
        assert len(mobile_adapter.events) == 1
        session_id, event = mobile_adapter.events[0]
        assert session_id == "sess_test"
        assert event["type"] == "crosschannel.message"
        assert event["channel"] == "desktop:desk_a"
        assert event["content"] == "Desktop-originated assistant message"
        assert event["route"] == "haiku"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_non_text_inbound_persists_artifacts_and_passes_them_to_opus(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="opus")
    runtime.config.public_base_url = "https://gateway.example.test"

    async def _fake_download(bridge_media_ref: str) -> tuple[bytes, str | None]:
        assert bridge_media_ref == "wamid_img_1:att_1"
        return (b"mock-whatsapp-image", "image/jpeg")

    runtime.download_whatsapp_media = _fake_download  # type: ignore[method-assign]
    await runtime.start()
    try:
        result = await runtime.process_incoming_user_message(
            {
                "content": "[image]",
                "channel": "whatsapp:+12153079021",
                "metadata": {
                    "platform": "whatsapp",
                    "message_id": "wamid_img_1",
                    "message_type": "image",
                    "attachments": [
                        {
                            "id": "att_1",
                            "kind": "image",
                            "mime_type": "image/jpeg",
                            "filename": "photo.jpg",
                            "caption": "look at this",
                            "size_bytes": 183920,
                            "width": 1280,
                            "height": 720,
                            "sha256": "abc123",
                            "bridge_media_ref": "wamid_img_1:att_1",
                        }
                    ],
                },
            }
        )

        assert result["route"] == "opus"
        assert len(result["input_artifacts"]) == 1
        assert result["input_artifacts"][0]["kind"] == "image"
        assert result["input_artifacts"][0]["bridge_media_ref"] == "wamid_img_1:att_1"
        assert result["input_artifacts"][0]["ingest_state"] == "staged"
        assert result["input_artifacts"][0]["path"].startswith("runs/artifacts/req_ingest_")

        stored_artifacts = runtime.artifact_store.list_for_request(result["request_id"])
        assert len(stored_artifacts) == 1
        assert stored_artifacts[0]["mime"] == "image/jpeg"
        assert stored_artifacts[0]["source_message_id"] == "wamid_img_1"

        task = runtime._build_orchestrator_task(  # noqa: SLF001 - direct unit seam
            request_record=result,
            session_id=result["session_id"],
            request_id=result["request_id"],
            channel=result["channel"],
        )
        assert len(task.input_artifacts) == 1
        assert task.input_artifacts[0]["caption"] == "look at this"
        assert task.input_artifacts[0]["provider_access"] == "signed_url"
        assert task.input_artifacts[0]["provider_url"].startswith("https://gateway.example.test/artifacts/content/")

        history_tail = runtime.session_store.get_history_tail(result["session_id"])
        assert history_tail[-1]["metadata"]["message_type"] == "image"
        assert history_tail[-1]["metadata"]["attachments"][0]["bridge_media_ref"] == "wamid_img_1:att_1"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_document_inbound_stages_supported_artifacts_into_runs_tree(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="opus")

    async def _fake_download(file_id: str) -> tuple[bytes, str | None]:
        assert file_id == "telegram_file_pdf_1"
        return (b"%PDF-1.7 fake pdf", "application/pdf")

    runtime.download_telegram_media = _fake_download  # type: ignore[method-assign]
    await runtime.start()
    try:
        result = await runtime.process_incoming_user_message(
            {
                "content": "[document]",
                "channel": "telegram:12345",
                "metadata": {
                    "platform": "telegram",
                    "message_id": "tg_doc_1",
                    "message_type": "document",
                    "attachments": [
                        {
                            "artifact_id": "tg_doc_art_1",
                            "kind": "document",
                            "mime_type": "application/pdf",
                            "filename": "strategy.pdf",
                            "download_url": "/internal/channels/telegram/media/telegram_file_pdf_1",
                            "telegram_file_id": "telegram_file_pdf_1",
                            "bridge_media_ref": "telegram:file:telegram_file_pdf_1",
                        }
                    ],
                },
            }
        )

        assert len(result["input_artifacts"]) == 1
        staged = result["input_artifacts"][0]
        assert staged["ingest_state"] == "staged"
        assert staged["mime"] == "application/pdf"
        assert staged["path"].startswith("runs/artifacts/req_ingest_")
        assert staged["sha256"]

        stored_artifacts = runtime.artifact_store.list_for_request(result["request_id"])
        assert stored_artifacts[0]["ingest_state"] == "staged"
        assert stored_artifacts[0]["path"] == staged["path"]
    finally:
        await runtime.stop()


def test_artifact_store_preserves_pre_staged_desktop_document_metadata(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="opus")
    runtime.artifact_store.initialize()

    manifests = runtime.artifact_store.persist_inbound_attachments(
        request_id="req_desktop_stage_1",
        session_id="sess_desktop_stage_1",
        source_channel="desktop:desk_a",
        source_platform="desktop",
        source_message_id=None,
        attachments=[
            {
                "artifact_id": "art_desktop_1",
                "kind": "document",
                "mime_type": "application/pdf",
                "filename": "plan.pdf",
                "sha256": "abc123",
                "path": "runs/artifacts/req_ingest_req_desktop_stage_1/inputs/art_desktop_1/original/plan.pdf",
                "ingest_state": "staged",
            }
        ],
    )

    assert manifests[0]["path"] == "runs/artifacts/req_ingest_req_desktop_stage_1/inputs/art_desktop_1/original/plan.pdf"
    assert manifests[0]["ingest_state"] == "staged"

    stored = runtime.artifact_store.list_for_request("req_desktop_stage_1")
    assert len(stored) == 1
    assert stored[0]["path"] == manifests[0]["path"]
    assert stored[0]["ingest_state"] == "staged"


def test_resolve_session_artifacts_falls_back_to_artifact_store(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="opus")
    runtime.config.artifacts_root = tmp_path / "runs" / "artifacts"
    runtime.session_store.initialize()
    runtime.artifact_store.initialize()
    artifact_path = runtime.config.artifacts_root / "tsk_pdf" / "orchestrator" / "doc.pdf"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(b"%PDF-1.4\n")

    runtime.artifact_store.persist_output_artifacts(
        request_id="req_original",
        session_id="sess_original",
        source_channel="desktop:desk_a",
        source_platform="desktop",
        source_message_id="msg_original",
        artifacts=[
            {
                "artifact_id": "anthropic_file_pdf_1",
                "task_id": "tsk_pdf",
                "kind": "output",
                "mime": "application/pdf",
                "filename": "doc.pdf",
                "path": "runs/artifacts/tsk_pdf/orchestrator/doc.pdf",
                "sha256": "abc123",
            }
        ],
    )

    result = runtime.resolve_session_artifacts(
        session_id="email-thread:followup",
        artifact_ids=["anthropic_file_pdf_1"],
        all_sessions=True,
    )

    assert result["count"] == 1
    assert result["artifacts"][0]["path"] == "runs/artifacts/tsk_pdf/orchestrator/doc.pdf"
    assert result["artifacts"][0]["filename"] == "doc.pdf"


@pytest.mark.asyncio
async def test_docs_autoparse_enriches_request_record_with_bundle_metadata(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="opus")
    await runtime.start()
    try:
        class _StubRedis:
            async def aclose(self) -> None:
                return

        runtime._redis = _StubRedis()  # type: ignore[assignment]
        request_record = {
            "route": "opus",
            "request_id": "req_docs_parse",
            "session_id": "sess_docs_parse",
            "channel": "desktop:desk_a",
            "source": "user",
            "source_id": "desktop",
            "input_artifacts": [
                {
                    "artifact_id": "art_doc_1",
                    "kind": "document",
                    "mime": "application/pdf",
                    "filename": "strategy.pdf",
                    "path": "runs/artifacts/req_ingest_req_docs_parse/inputs/art_doc_1/original/strategy.pdf",
                    "sha256": "abc123",
                    "ingest_state": "staged",
                }
            ],
        }
        runtime.artifact_store.persist_inbound_attachments(
            request_id="req_docs_parse",
            session_id="sess_docs_parse",
            source_channel="desktop:desk_a",
            source_platform="desktop",
            source_message_id=None,
            attachments=[
                {
                    "artifact_id": "art_doc_1",
                    "kind": "document",
                    "mime_type": "application/pdf",
                    "filename": "strategy.pdf",
                }
            ],
        )
        runtime.artifact_store.update_ingest_state(
            "art_doc_1",
            path=request_record["input_artifacts"][0]["path"],
            sha256="abc123",
            ingest_state="staged",
        )

        async def _fake_dispatch(*, request_record, input_artifacts):
            assert len(input_artifacts) == 1
            return {
                "status": "completed",
                "task_id": "tsk_docs_parse",
                "output": {
                    "bundle_id": "bundle_docs_001",
                    "documents": [
                        {
                            "artifact_id": "art_doc_1",
                            "doc_id": "doc_001",
                            "title": "Strategy",
                            "section_count": 3,
                            "chunk_count": 7,
                            "paths": {
                                "document_md": "runs/artifacts/tsk_docs_parse/docs_parser/art_doc_1/document.md",
                                "document_json": "runs/artifacts/tsk_docs_parse/docs_parser/art_doc_1/document.json",
                                "chunk_index": "runs/artifacts/tsk_docs_parse/docs_parser/art_doc_1/chunk_index.json",
                                "manifest": "runs/artifacts/tsk_docs_parse/docs_parser/art_doc_1/manifest.json",
                            },
                        }
                    ],
                },
            }

        runtime._dispatch_docs_parse_bundle = _fake_dispatch  # type: ignore[method-assign]

        await runtime._ensure_request_documents_parsed(request_record)  # noqa: SLF001

        enriched = request_record["input_artifacts"][0]
        assert enriched["ingest_state"] == "parsed"
        assert enriched["parse_bundle_id"] == "bundle_docs_001"
        assert enriched["parse_task_id"] == "tsk_docs_parse"
        assert enriched["parsed_summary"]["doc_id"] == "doc_001"

        stored = runtime.artifact_store.list_for_request("req_docs_parse")[0]
        assert stored["ingest_state"] == "parsed"
        assert stored["parse_bundle_id"] == "bundle_docs_001"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_email_inbound_preprocess_rewrites_effective_orchestrator_query(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="opus")
    runtime._redis = FakeRedis()
    await runtime.start()
    try:
        request_record = await runtime.process_incoming_user_message(
            {
                "content": "Email subject: Need help\n\nCan you help me with the latest invoice?",
                "channel": "agent-email:support@example.com",
                "metadata": {
                    "platform": "agent-email",
                    "message_id": "msg_email_1",
                    "thread_id": "thr_email_1",
                    "mailbox_address": "support@example.com",
                    "session_scope": "email_thread",
                    "rollover_exempt": True,
                },
            }
        )

        async def _fake_dispatch(*, request_record: dict[str, Any]) -> dict[str, Any]:
            assert request_record["session_id"].startswith("email-thread:")
            return {
                "status": "completed",
                "task_id": "tsk_email_process_1",
                "output": {
                    "summary": "Customer is asking for the latest invoice and wants quick confirmation.",
                    "subject": "Need help",
                    "from_address": "owner@example.com",
                    "trusted_sender": True,
                    "sender_role": "owner",
                    "attachments": [{"id": "att_1"}, {"id": "att_2"}],
                    "matched_instruction": {"label": "Auto reply invoices"},
                    "auto_reply": {"sent": True, "message_id": "msg_auto_1"},
                },
                "artifacts": [{"artifact_id": "art_email_process_1"}],
            }

        runtime._dispatch_email_process_inbound = _fake_dispatch  # type: ignore[method-assign]

        await runtime._ensure_request_email_processed(request_record)  # noqa: SLF001 - intentional unit seam

        assert request_record["email_process_inbound_state"] == "completed"
        assert request_record["message"]["content"].startswith("Email subject: Need help")
        assert "Customer is asking for the latest invoice" in request_record["orchestrator_query_override"]
        assert "Auto reply invoices" in request_record["orchestrator_query_override"]
        assert "2 attachment(s) were downloaded into the email specialist workflow" in request_record["orchestrator_query_override"]
        assert "Trusted sender: yes. Treat this as a direct owner query arriving over email." in request_record["orchestrator_query_override"]

        task = runtime._build_orchestrator_task(  # noqa: SLF001 - intentional unit seam
            request_record=request_record,
            session_id=request_record["session_id"],
            request_id=request_record["request_id"],
            channel=request_record["channel"],
        )
        assert task.input["query"] == request_record["orchestrator_query_override"]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_email_inbound_preprocess_falls_back_to_raw_summary_when_specialist_fails(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="opus")
    runtime._redis = FakeRedis()
    await runtime.start()
    try:
        request_record = await runtime.process_incoming_user_message(
            {
                "content": "Email subject: Need help\n\nCan you help me with the latest invoice?",
                "channel": "agent-email:support@example.com",
                "metadata": {
                    "platform": "agent-email",
                    "message_id": "msg_email_2",
                    "thread_id": "thr_email_2",
                    "mailbox_address": "support@example.com",
                    "session_scope": "email_thread",
                    "rollover_exempt": True,
                },
            }
        )

        async def _fake_dispatch(*, request_record: dict[str, Any]) -> dict[str, Any]:
            return {
                "status": "failed",
                "task_id": "tsk_email_process_2",
                "error_message": "email.process_inbound failed",
            }

        runtime._dispatch_email_process_inbound = _fake_dispatch  # type: ignore[method-assign]

        await runtime._ensure_request_email_processed(request_record)  # noqa: SLF001 - intentional unit seam

        assert request_record["email_process_inbound_state"] == "failed"
        assert "orchestrator_query_override" not in request_record

        task = runtime._build_orchestrator_task(  # noqa: SLF001 - intentional unit seam
            request_record=request_record,
            session_id=request_record["session_id"],
            request_id=request_record["request_id"],
            channel=request_record["channel"],
        )
        assert task.input["query"] == "Email subject: Need help\n\nCan you help me with the latest invoice?"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_email_thread_response_delivery_is_enriched_for_trusted_sender_reply(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="opus")
    runtime._redis = FakeRedis()
    await runtime.start()
    try:
        request_record = await runtime.process_incoming_user_message(
            {
                "content": "Email subject: Need help\n\nI replied to your email.",
                "channel": "agent-email:support@example.com",
                "metadata": {
                    "platform": "agent-email",
                    "message_id": "msg_email_trusted",
                    "thread_id": "thr_email_trusted",
                    "mailbox_id": "mbx_support",
                    "mailbox_address": "support@example.com",
                    "subject": "Need help",
                    "from_address": "owner@example.com",
                    "from_name": "Owner",
                    "session_scope": "email_thread",
                    "rollover_exempt": True,
                },
            }
        )
        request_record["email_process_inbound_output"] = {
            "subject": "Need help",
            "from_address": "owner@example.com",
            "trusted_sender": True,
            "sender_role": "owner",
            "auto_reply": {"sent": False},
        }

        prepared = runtime._prepare_channel_event_for_delivery(  # noqa: SLF001 - intentional unit seam
            {
                "type": "response.complete",
                "request_id": request_record["request_id"],
                "session_id": request_record["session_id"],
                "channel": request_record["channel"],
                "content": "I saw your reply.",
            },
            request_record=request_record,
        )

        assert prepared["thread_id"] == "thr_email_trusted"
        assert prepared["mailbox_id"] == "mbx_support"
        assert prepared["mailbox_address"] == "support@example.com"
        assert prepared["from_address"] == "owner@example.com"
        assert prepared["trusted_sender"] is True
        assert prepared["email_thread_reply"] is True
        assert prepared["email_thread_reply_eligible"] is True
        assert prepared["to_recipients"] == [{"email": "owner@example.com", "name": "Owner"}]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_docs_autoparse_emits_progress_for_desktop_documents(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="opus")
    await runtime.start()
    try:
        class _StubRedis:
            async def aclose(self) -> None:
                return

        runtime._redis = _StubRedis()  # type: ignore[assignment]
        request_record = {
            "route": "opus",
            "request_id": "req_docs_progress",
            "session_id": "sess_docs_progress",
            "channel": "desktop:desk_a",
            "source": "user",
            "source_id": "desktop",
            "input_artifacts": [
                {
                    "artifact_id": "art_doc_progress",
                    "kind": "document",
                    "mime": "application/pdf",
                    "filename": "roadmap.pdf",
                    "path": "runs/artifacts/req_ingest_req_docs_progress/inputs/art_doc_progress/original/roadmap.pdf",
                    "sha256": "abc123",
                    "ingest_state": "staged",
                }
            ],
        }

        async def _fake_dispatch(*, request_record, input_artifacts, progress_callback=None):
            assert len(input_artifacts) == 1
            if progress_callback is not None:
                await progress_callback(
                    {
                        "type": "task.progress",
                        "message": "Parsing document 1/1: roadmap.pdf.",
                    }
                )
            return {
                "status": "completed",
                "task_id": "tsk_docs_progress",
                "output": {
                    "bundle_id": "bundle_docs_progress",
                    "documents": [
                        {
                            "artifact_id": "art_doc_progress",
                            "doc_id": "doc_progress",
                            "title": "Roadmap",
                            "section_count": 2,
                            "chunk_count": 4,
                            "paths": {
                                "document_md": "runs/artifacts/tsk_docs_progress/docs_parser/art_doc_progress/document.md",
                                "document_json": "runs/artifacts/tsk_docs_progress/docs_parser/art_doc_progress/document.json",
                                "chunk_index": "runs/artifacts/tsk_docs_progress/docs_parser/art_doc_progress/chunk_index.json",
                                "manifest": "runs/artifacts/tsk_docs_progress/docs_parser/art_doc_progress/manifest.json",
                            },
                        }
                    ],
                },
            }

        events: list[dict[str, Any]] = []

        async def _capture(event: dict[str, Any]) -> None:
            events.append(event)

        runtime._dispatch_docs_parse_bundle = _fake_dispatch  # type: ignore[method-assign]

        await runtime._ensure_request_documents_parsed(request_record, send=_capture)  # noqa: SLF001

        progress_events = [event for event in events if event.get("type") == "task.progress"]
        assert len(progress_events) >= 3
        assert progress_events[0]["docs_progress"]["stage"] == "prepare"
        assert any(event["docs_progress"]["stage"] == "parse" for event in progress_events)
        assert progress_events[-1]["docs_progress"]["stage"] == "ready"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_docs_autoparse_timeout_marks_parse_pending_and_schedules_reconcile(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="opus")
    await runtime.start()
    try:
        class _StubRedis:
            async def aclose(self) -> None:
                return

        runtime._redis = _StubRedis()  # type: ignore[assignment]
        request_record = {
            "route": "opus",
            "request_id": "req_docs_pending",
            "session_id": "sess_docs_pending",
            "channel": "desktop:desk_a",
            "source": "user",
            "source_id": "desktop",
            "input_artifacts": [
                {
                    "artifact_id": "art_doc_pending",
                    "kind": "document",
                    "mime": "application/pdf",
                    "filename": "newsletter.pdf",
                    "path": "runs/artifacts/req_ingest_req_docs_pending/inputs/art_doc_pending/original/newsletter.pdf",
                    "sha256": "abc123",
                    "ingest_state": "staged",
                }
            ],
        }
        runtime.request_records["req_docs_pending"] = request_record
        runtime.artifact_store.persist_inbound_attachments(
            request_id="req_docs_pending",
            session_id="sess_docs_pending",
            source_channel="desktop:desk_a",
            source_platform="desktop",
            source_message_id=None,
            attachments=[
                {
                    "artifact_id": "art_doc_pending",
                    "kind": "document",
                    "mime_type": "application/pdf",
                    "filename": "newsletter.pdf",
                }
            ],
        )
        runtime.artifact_store.update_ingest_state(
            "art_doc_pending",
            path=request_record["input_artifacts"][0]["path"],
            sha256="abc123",
            ingest_state="staged",
        )

        async def _fake_dispatch(*, request_record, input_artifacts):
            assert len(input_artifacts) == 1
            return {
                "status": "pending",
                "task_id": "tsk_docs_pending",
                "error_message": "Timed out waiting for tsk_docs_pending.",
            }

        scheduled: list[Any] = []

        def _fake_track_background_task(coroutine) -> None:
            scheduled.append(coroutine)

        runtime._dispatch_docs_parse_bundle = _fake_dispatch  # type: ignore[method-assign]
        runtime._track_background_task = _fake_track_background_task  # type: ignore[method-assign]

        await runtime._ensure_request_documents_parsed(request_record)  # noqa: SLF001

        enriched = request_record["input_artifacts"][0]
        assert enriched["ingest_state"] == "parse_pending"
        assert enriched["parse_task_id"] == "tsk_docs_pending"
        assert enriched["parse_error"] == "Timed out waiting for tsk_docs_pending."

        stored = runtime.artifact_store.list_for_request("req_docs_pending")[0]
        assert stored["ingest_state"] == "parse_pending"
        assert stored["parse_task_id"] == "tsk_docs_pending"
        assert len(scheduled) == 1
        for coroutine in scheduled:
            coroutine.close()
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_docs_autoparse_reconcile_marks_late_completion_as_parsed(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="opus")
    await runtime.start()
    try:
        request_record = {
            "route": "opus",
            "request_id": "req_docs_reconcile",
            "session_id": "sess_docs_reconcile",
            "channel": "desktop:desk_a",
            "source": "user",
            "source_id": "desktop",
            "input_artifacts": [
                {
                    "artifact_id": "art_doc_reconcile",
                    "kind": "document",
                    "mime": "application/pdf",
                    "filename": "newsletter.pdf",
                    "path": "runs/artifacts/req_ingest_req_docs_reconcile/inputs/art_doc_reconcile/original/newsletter.pdf",
                    "sha256": "abc123",
                    "ingest_state": "parse_pending",
                    "parse_task_id": "tsk_docs_reconcile",
                    "parse_error": "Timed out waiting for tsk_docs_reconcile.",
                }
            ],
        }
        runtime.request_records["req_docs_reconcile"] = request_record
        runtime.artifact_store.persist_inbound_attachments(
            request_id="req_docs_reconcile",
            session_id="sess_docs_reconcile",
            source_channel="desktop:desk_a",
            source_platform="desktop",
            source_message_id=None,
            attachments=[
                {
                    "artifact_id": "art_doc_reconcile",
                    "kind": "document",
                    "mime_type": "application/pdf",
                    "filename": "newsletter.pdf",
                }
            ],
        )
        runtime.artifact_store.update_ingest_state(
            "art_doc_reconcile",
            path=request_record["input_artifacts"][0]["path"],
            sha256="abc123",
            ingest_state="parse_pending",
            parse_task_id="tsk_docs_reconcile",
        )

        async def _fake_wait(task_id: str, *, timeout_sec: float):
            assert task_id == "tsk_docs_reconcile"
            assert timeout_sec == runtime.config.docs_parse_reconcile_timeout_sec
            return {
                "status": "completed",
                "task_id": "tsk_docs_reconcile",
                "output": {
                    "bundle_id": "bundle_docs_reconciled",
                    "documents": [
                        {
                            "artifact_id": "art_doc_reconcile",
                            "doc_id": "doc_reconciled",
                            "title": "Newsletter",
                            "section_count": 4,
                            "chunk_count": 8,
                        }
                    ],
                },
            }

        runtime._wait_for_agent_terminal_result = _fake_wait  # type: ignore[method-assign]

        await runtime._reconcile_docs_parse_bundle(  # noqa: SLF001
            request_id="req_docs_reconcile",
            artifact_ids=["art_doc_reconcile"],
            task_id="tsk_docs_reconcile",
        )

        enriched = request_record["input_artifacts"][0]
        assert enriched["ingest_state"] == "parsed"
        assert enriched["parse_bundle_id"] == "bundle_docs_reconciled"
        assert enriched["parsed_summary"]["doc_id"] == "doc_reconciled"
        assert "parse_error" not in enriched

        stored = runtime.artifact_store.list_for_request("req_docs_reconcile")[0]
        assert stored["ingest_state"] == "parsed"
        assert stored["parse_bundle_id"] == "bundle_docs_reconciled"
        assert stored["parsed_summary"]["doc_id"] == "doc_reconciled"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_refresh_active_working_set_uses_latest_session_artifact_state(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="opus")
    await runtime.start()
    try:
        runtime._ensure_session_state_seeded("sess_artifacts")  # noqa: SLF001
        runtime.artifact_store.persist_inbound_attachments(
            request_id="req_artifact_old",
            session_id="sess_artifacts",
            source_channel="desktop:desk_a",
            source_platform="desktop",
            source_message_id=None,
            attachments=[
                {
                    "artifact_id": "art_newsletter",
                    "kind": "document",
                    "mime_type": "application/pdf",
                    "filename": "newsletter.pdf",
                    "ingest_state": "parsed",
                    "parse_task_id": "tsk_newsletter",
                    "parse_bundle_id": "bundle_newsletter",
                    "parsed_summary": {
                        "doc_id": "doc_newsletter",
                        "title": "Newsletter",
                        "chunk_count": 16,
                        "section_count": 16,
                    },
                }
            ],
        )

        working_set = runtime._refresh_active_working_set("sess_artifacts")  # noqa: SLF001

        assert "art_newsletter" not in working_set["pending_artifact_pointers"]
        assert working_set["recent_document_artifacts"][0]["artifact_id"] == "art_newsletter"
        assert working_set["recent_document_artifacts"][0]["parse_bundle_id"] == "bundle_newsletter"
        rendered = runtime._render_active_working_set_context(working_set)  # noqa: SLF001
        assert "Recent parsed documents" in (rendered or "")
        assert "bundle_id=bundle_newsletter" in (rendered or "")
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_refresh_active_working_set_surfaces_recent_memory_tool_receipts(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="opus")
    runtime.memory_client = FakeMemoryClient(enabled=True)
    await runtime.start()
    try:
        session_id = "sess_receipts"
        await runtime.memory_write_core_fact(
            {
                "fact": "Praveen's partner is Priya.",
                "title": "Partner name",
                "canonical_key": "relationships.partner.name",
                "metadata": {
                    "session_id": session_id,
                    "request_id": "req_receipt_1",
                    "task_id": "tsk_receipt_1",
                },
                "provenance": {
                    "created_by": "cosmic/orchestrator:1.0.0",
                    "session_id": session_id,
                    "request_id": "req_receipt_1",
                    "task_id": "tsk_receipt_1",
                    "source_kind": "orchestrator_tool",
                },
            }
        )

        working_set = runtime._refresh_active_working_set(session_id)  # noqa: SLF001

        assert working_set["recent_tool_receipts"][0]["operation"] == "memory_write_core_fact"
        assert working_set["recent_tool_receipts"][0]["canonical_key"] == "relationships.partner.name"
        rendered = runtime._render_active_working_set_context(working_set)  # noqa: SLF001
        assert "Recent tool receipts" in (rendered or "")
        assert "relationships.partner.name" in (rendered or "")
    finally:
        await runtime.stop()


def test_gateway_core_fact_normalization_sets_provenance_defaults(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="opus")
    normalized, _ = runtime._normalize_tool_core_fact_payload(  # noqa: SLF001 - intentional unit seam
        {
            "fact": "Praveen's partner is Priya.",
            "title": "Partner name",
            "canonical_key": "relationships.partner.name",
            "metadata": {
                "session_id": "sess_core_fact_1",
            },
            "provenance": {
                "source_kind": "orchestrator_tool",
                "source_id": "req_core_fact_1",
                "session_id": "sess_core_fact_1",
            },
        }
    )

    metadata = normalized["metadata"]
    assert metadata["confirmation_status"] == "confirmed"
    assert metadata["created_in_session_id"] == "sess_core_fact_1"
    assert metadata["created_by_tool"] == "memory_write_core_fact"
    assert metadata["derived_from_assistant_inference"] is False


def test_desktop_upload_route_stages_documents(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    runtime.config.artifacts_root = tmp_path / "runs" / "artifacts"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway_runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(channel_router)

    with TestClient(app) as client:
        response = client.post(
            "/channels/desktop/uploads",
            headers={"Authorization": "Bearer test-token"},
            data={
                "request_id": "req_upload_1",
                "session_id": "sess_upload_1",
                "device_id": "desk_upload_1",
            },
            files=[
                (
                    "files",
                    (
                        "brief.pdf",
                        b"%PDF-1.7 fake upload",
                        "application/pdf",
                    ),
                )
            ],
        )

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["attachments"]) == 1
    attachment = payload["attachments"][0]
    assert attachment["ingest_state"] == "staged"
    assert attachment["mime"] == "application/pdf"
    assert attachment["path"].startswith("runs/artifacts/req_ingest_req_upload_1/")
    staged_path = runtime.config.artifacts_root / "req_ingest_req_upload_1" / "inputs" / attachment["artifact_id"] / "original" / "brief.pdf"
    assert staged_path.exists()


def test_desktop_upload_route_rejects_oversized_documents(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    runtime.config.artifacts_root = tmp_path / "runs" / "artifacts"
    runtime.config.docs_upload_max_file_bytes = 1024 * 1024

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway_runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(channel_router)

    with TestClient(app) as client:
        response = client.post(
            "/channels/desktop/uploads",
            headers={"Authorization": "Bearer test-token"},
            data={
                "request_id": "req_upload_oversized",
                "session_id": "sess_upload_oversized",
                "device_id": "desk_upload_oversized",
            },
            files=[
                (
                    "files",
                    (
                        "oversized.pdf",
                        b"x" * (1024 * 1024 + 1),
                        "application/pdf",
                    ),
                )
            ],
        )

    assert response.status_code == 413
    assert response.json()["detail"] == "Attachment exceeds the 1 MB upload limit: oversized.pdf"


def test_desktop_upload_route_stages_images(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    runtime.config.artifacts_root = tmp_path / "runs" / "artifacts"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway_runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(channel_router)

    with TestClient(app) as client:
        response = client.post(
            "/channels/desktop/uploads",
            headers={"Authorization": "Bearer test-token"},
            data={
                "request_id": "req_upload_image_1",
                "session_id": "sess_upload_image_1",
                "device_id": "desk_upload_image_1",
            },
            files=[
                (
                    "files",
                    (
                        "photo.png",
                        b"\x89PNG\r\n\x1a\nfake",
                        "image/png",
                    ),
                )
            ],
        )

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["attachments"]) == 1
    attachment = payload["attachments"][0]
    assert attachment["kind"] == "image"
    assert attachment["mime"] == "image/png"
    assert attachment["ingest_state"] == "staged"
    staged_path = runtime.config.artifacts_root / "req_ingest_req_upload_image_1" / "inputs" / attachment["artifact_id"] / "original" / "photo.png"
    assert staged_path.exists()


def test_mobile_upload_route_stages_images_for_active_phone_session(test_client: TestClient) -> None:
    with test_client.websocket_connect("/ws?token=test-token&device_id=mob_upload_1") as websocket:
        websocket.send_json(
            {
                "type": "resume",
                "request_id": "resume_mobile_upload_1",
                "session_id": None,
                "known_task_ids": [],
            }
        )
        resume = websocket.receive_json()
        assert resume["type"] == "resume.ok"

        response = test_client.post(
            "/channels/mobile/uploads",
            headers={"Authorization": "Bearer test-token"},
            data={
                "request_id": "req_mobile_upload_1",
                "session_id": resume["session_id"],
                "device_id": "mob_upload_1",
            },
            files=[
                (
                    "files",
                    (
                        "photo.png",
                        b"\x89PNG\r\n\x1a\nmobile",
                        "image/png",
                    ),
                )
            ],
        )

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["attachments"]) == 1
    attachment = payload["attachments"][0]
    assert attachment["kind"] == "image"
    assert attachment["mime"] == "image/png"
    assert attachment["source_platform"] == "mobile"
    assert attachment["path"].startswith("runs/artifacts/req_ingest_req_mobile_upload_1/")


def test_mobile_device_routes_authorize_list_and_revoke(test_client: TestClient) -> None:
    listed_empty = test_client.get(
        "/channels/mobile/devices",
        headers={"Authorization": "Bearer test-token"},
    )
    assert listed_empty.status_code == 200
    assert listed_empty.json()["devices"] == []

    authorized = test_client.post(
        "/channels/mobile/devices/authorize",
        headers={"Authorization": "Bearer test-token"},
        json={
            "device_id": "mob_manage_1",
            "device_name": "Praveen's Pixel 8 Pro",
            "device_name_source": "user_assigned",
            "model_name": "Pixel 8 Pro",
            "brand": "Google",
            "manufacturer": "Google",
            "platform": "android",
            "os_name": "Android",
            "os_version": "14",
            "device_type": "phone",
            "is_physical_device": True,
            "app_version": "1.0.0",
            "app_build": "1",
        },
    )
    assert authorized.status_code == 200
    assert authorized.json()["device"]["device_id"] == "mob_manage_1"
    assert authorized.json()["device"]["device_name"] == "Praveen's Pixel 8 Pro"
    assert authorized.json()["device"]["device_name_source"] == "user_assigned"
    assert authorized.json()["device"]["model_name"] == "Pixel 8 Pro"
    assert authorized.json()["device"]["platform"] == "android"
    assert authorized.json()["device"]["revoked"] is False

    listed = test_client.get(
        "/channels/mobile/devices",
        headers={"Authorization": "Bearer test-token"},
    )
    assert listed.status_code == 200
    assert listed.json()["devices"][0]["device_id"] == "mob_manage_1"
    assert listed.json()["devices"][0]["device_name"] == "Praveen's Pixel 8 Pro"
    assert listed.json()["devices"][0]["device_name_source"] == "user_assigned"
    assert listed.json()["devices"][0]["app_version"] == "1.0.0"
    assert listed.json()["devices"][0]["revoked"] is False

    revoked = test_client.delete(
        "/channels/mobile/devices/mob_manage_1",
        headers={"Authorization": "Bearer test-token"},
    )
    assert revoked.status_code == 200
    assert revoked.json()["device"]["device_id"] == "mob_manage_1"
    assert revoked.json()["device"]["revoked"] is True

    reauthorized = test_client.post(
        "/channels/mobile/devices/authorize",
        headers={"Authorization": "Bearer test-token"},
        json={"device_id": "mob_manage_1"},
    )
    assert reauthorized.status_code == 200
    assert reauthorized.json()["device"]["device_id"] == "mob_manage_1"
    assert reauthorized.json()["device"]["revoked"] is False
    assert reauthorized.json()["device"]["device_name"] == "Praveen's Pixel 8 Pro"


def test_mobile_device_list_deduplicates_re_registered_phone(test_client: TestClient) -> None:
    metadata = {
        "device_name": "Praveen's Pixel 8 Pro",
        "device_name_source": "user_assigned",
        "model_name": "Pixel 8 Pro",
        "brand": "Google",
        "manufacturer": "Google",
        "platform": "android",
        "os_name": "Android",
        "os_version": "14",
        "device_type": "phone",
        "is_physical_device": True,
        "app_version": "1.0.0",
        "app_build": "1",
    }
    for device_id in ("mob_duplicate_old", "mob_duplicate_new"):
        authorized = test_client.post(
            "/channels/mobile/devices/authorize",
            headers={"Authorization": "Bearer test-token"},
            json={"device_id": device_id, **metadata},
        )
        assert authorized.status_code == 200
        registered = test_client.post(
            f"/channels/mobile/devices/{device_id}/push-token",
            headers={"Authorization": "Bearer test-token"},
            json={"push_token": "ExponentPushToken[duplicate-phone-token]"},
        )
        assert registered.status_code == 200

    listed = test_client.get(
        "/channels/mobile/devices",
        headers={"Authorization": "Bearer test-token"},
    )
    assert listed.status_code == 200
    devices = [
        device
        for device in listed.json()["devices"]
        if "mob_duplicate" in device["device_id"]
        or "mob_duplicate" in " ".join(device.get("duplicate_device_ids") or [])
    ]
    assert len(devices) == 1
    assert devices[0]["device_id"] == "mob_duplicate_new"
    assert devices[0]["duplicate_count"] == 2
    assert devices[0]["duplicate_device_ids"] == ["mob_duplicate_old"]
    assert devices[0]["deduped_device_ids"] == ["mob_duplicate_new", "mob_duplicate_old"]


def test_mobile_push_token_routes_register_update_and_delete(test_client: TestClient) -> None:
    authorized = test_client.post(
        "/channels/mobile/devices/authorize",
        headers={"Authorization": "Bearer test-token"},
        json={"device_id": "mob_push_1", "platform": "android"},
    )
    assert authorized.status_code == 200

    registered = test_client.post(
        "/channels/mobile/devices/mob_push_1/push-token",
        headers={"Authorization": "Bearer test-token"},
        json={
            "push_token": "ExponentPushToken[test-token-1]",
            "notifications_enabled": True,
            "preferences": {"chat": True, "tasks": False, "unknown": True},
        },
    )
    assert registered.status_code == 200
    device = registered.json()["device"]
    assert device["device_id"] == "mob_push_1"
    assert device["push_token"] == "ExponentPushToken[test-token-1]"
    assert device["notifications_enabled"] is True
    assert '"chat": true' in device["notification_preferences_json"]
    assert "unknown" not in device["notification_preferences_json"]

    updated = test_client.patch(
        "/channels/mobile/devices/mob_push_1/push-token",
        headers={"Authorization": "Bearer test-token"},
        json={"notifications_enabled": False},
    )
    assert updated.status_code == 200
    assert updated.json()["device"]["push_token"] == "ExponentPushToken[test-token-1]"
    assert updated.json()["device"]["notifications_enabled"] is False

    deleted = test_client.delete(
        "/channels/mobile/devices/mob_push_1/push-token",
        headers={"Authorization": "Bearer test-token"},
    )
    assert deleted.status_code == 200
    assert deleted.json()["device"]["push_token"] is None


def test_mobile_device_list_marks_active_connections(test_client: TestClient) -> None:
    with test_client.websocket_connect("/ws?token=test-token&device_id=mob_manage_active") as websocket:
        websocket.send_json(
            {
                "type": "resume",
                "request_id": "resume_mobile_manage_active",
                "session_id": None,
                "known_task_ids": [],
            }
        )
        resume = websocket.receive_json()
        assert resume["type"] == "resume.ok"

        listed = test_client.get(
            "/channels/mobile/devices",
            headers={"Authorization": "Bearer test-token"},
        )
        assert listed.status_code == 200
        device = next(item for item in listed.json()["devices"] if item["device_id"] == "mob_manage_active")
        assert device["active"] is True
        assert device["current_channel"] == "mobile:mob_manage_active"
        assert device["current_session_id"] == resume["session_id"]

        websocket.send_json(
            {
                "type": "device.presence",
                "state": "foreground",
                "visible_screen": "tasks",
                "ts_unix_ms": 1234567890,
            }
        )
        listed_after_presence = test_client.get(
            "/channels/mobile/devices",
            headers={"Authorization": "Bearer test-token"},
        )
        assert listed_after_presence.status_code == 200
        device_after_presence = next(
            item
            for item in listed_after_presence.json()["devices"]
            if item["device_id"] == "mob_manage_active"
        )
        assert device_after_presence["presence_state"] == "foreground"
        assert device_after_presence["visible_screen"] == "tasks"


def test_revoked_mobile_device_upload_route_is_blocked(test_client: TestClient) -> None:
    revoked = test_client.delete(
        "/channels/mobile/devices/mob_removed_upload",
        headers={"Authorization": "Bearer test-token"},
    )
    assert revoked.status_code == 200

    response = test_client.post(
        "/channels/mobile/uploads",
        headers={"Authorization": "Bearer test-token"},
        data={
            "request_id": "req_mobile_removed_upload",
            "session_id": "sess_removed",
            "device_id": "mob_removed_upload",
        },
        files=[
            (
                "files",
                (
                    "photo.png",
                    b"\x89PNG\r\n\x1a\nmobile",
                    "image/png",
                ),
            )
        ],
    )

    assert response.status_code == 403
    assert "re-authorize" in response.json()["detail"]


def test_mobile_upload_route_requires_active_phone_session(test_client: TestClient) -> None:
    response = test_client.post(
        "/channels/mobile/uploads",
        headers={"Authorization": "Bearer test-token"},
        data={
            "request_id": "req_mobile_upload_inactive",
            "session_id": "sess_20260329",
            "device_id": "mob_upload_inactive",
        },
        files=[
            (
                "files",
                (
                    "photo.png",
                    b"\x89PNG\r\n\x1a\nmobile",
                    "image/png",
                ),
            )
        ],
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Mobile session is not active for this device. Reconnect and retry."


def test_mobile_upload_route_stages_images_for_multiple_active_phones(test_client: TestClient) -> None:
    with test_client.websocket_connect("/ws?token=test-token&device_id=mob_upload_2a") as first_phone:
        first_phone.send_json(
            {
                "type": "resume",
                "request_id": "resume_mobile_upload_2a",
                "session_id": None,
                "known_task_ids": [],
            }
        )
        first_resume = first_phone.receive_json()
        assert first_resume["type"] == "resume.ok"

        with test_client.websocket_connect("/ws?token=test-token&device_id=mob_upload_2b") as second_phone:
            second_phone.send_json(
                {
                    "type": "resume",
                    "request_id": "resume_mobile_upload_2b",
                    "session_id": None,
                    "known_task_ids": [],
                }
            )
            second_resume = second_phone.receive_json()
            assert second_resume["type"] == "resume.ok"

            first_response = test_client.post(
                "/channels/mobile/uploads",
                headers={"Authorization": "Bearer test-token"},
                data={
                    "request_id": "req_mobile_upload_2a",
                    "session_id": first_resume["session_id"],
                    "device_id": "mob_upload_2a",
                },
                files=[
                    (
                        "files",
                        (
                            "photo-a.png",
                            b"\x89PNG\r\n\x1a\nphone-a",
                            "image/png",
                        ),
                    )
                ],
            )
            second_response = test_client.post(
                "/channels/mobile/uploads",
                headers={"Authorization": "Bearer test-token"},
                data={
                    "request_id": "req_mobile_upload_2b",
                    "session_id": second_resume["session_id"],
                    "device_id": "mob_upload_2b",
                },
                files=[
                    (
                        "files",
                        (
                            "photo-b.png",
                            b"\x89PNG\r\n\x1a\nphone-b",
                            "image/png",
                        ),
                    )
                ],
            )

    assert first_response.status_code == 200
    assert first_response.json()["attachments"][0]["source_channel"] == "mobile:mob_upload_2a"
    assert first_response.json()["attachments"][0]["source_platform"] == "mobile"
    assert second_response.status_code == 200
    assert second_response.json()["attachments"][0]["source_channel"] == "mobile:mob_upload_2b"
    assert second_response.json()["attachments"][0]["source_platform"] == "mobile"


def test_mobile_upload_route_rejects_session_mismatch_for_active_phone(test_client: TestClient) -> None:
    with test_client.websocket_connect("/ws?token=test-token&device_id=mob_upload_3") as websocket:
        websocket.send_json(
            {
                "type": "resume",
                "request_id": "resume_mobile_upload_3",
                "session_id": None,
                "known_task_ids": [],
            }
        )
        resume = websocket.receive_json()
        assert resume["type"] == "resume.ok"

        response = test_client.post(
            "/channels/mobile/uploads",
            headers={"Authorization": "Bearer test-token"},
            data={
                "request_id": "req_mobile_upload_mismatch",
                "session_id": "sess_wrong_phone",
                "device_id": "mob_upload_3",
            },
            files=[
                (
                    "files",
                    (
                        "photo.png",
                        b"\x89PNG\r\n\x1a\nmobile",
                        "image/png",
                    ),
                )
            ],
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "session_id does not match the active mobile session for this device"


@pytest.mark.asyncio
async def test_document_attachments_force_opus_even_with_text_content(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="haiku")
    await runtime.start()
    try:
        result = await runtime.process_incoming_user_message(
            {
                "content": "Use this deck to summarize pricing changes.",
                "channel": "desktop:desk_docs",
                "metadata": {
                    "platform": "desktop",
                    "message_type": "query",
                    "attachments": [
                        {
                            "artifact_id": "desktop_doc_1",
                            "kind": "document",
                            "mime_type": "application/pdf",
                            "filename": "pricing.pdf",
                            "ingest_state": "staged",
                            "path": "runs/artifacts/req_ingest_req_docs_force/inputs/desktop_doc_1/original/pricing.pdf",
                        }
                    ],
                },
            }
        )
    finally:
        await runtime.stop()

    assert result["route"] == "opus"
    assert result["classification"]["signals"] == ["media_attachments"]


@pytest.mark.asyncio
async def test_mobile_image_attachments_stage_into_input_artifacts_and_force_opus(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="haiku")
    await runtime.start()
    try:
        result = await runtime.process_incoming_user_message(
            {
                "content": "[image]",
                "channel": "mobile:mob_docs",
                "metadata": {
                    "platform": "mobile",
                    "message_type": "query",
                    "attachments": [
                        {
                            "artifact_id": "mobile_img_1",
                            "kind": "image",
                            "mime_type": "image/png",
                            "filename": "photo.png",
                            "ingest_state": "staged",
                            "path": "runs/artifacts/req_ingest_req_mobile_image_force/inputs/mobile_img_1/original/photo.png",
                            "sha256": "abc123",
                        }
                    ],
                },
            }
        )
    finally:
        await runtime.stop()

    assert result["route"] == "opus"
    assert result["classification"]["signals"] == ["media_attachments"]
    assert len(result["input_artifacts"]) == 1
    assert result["input_artifacts"][0]["kind"] == "image"
    assert result["input_artifacts"][0]["source_platform"] == "mobile"


@pytest.mark.asyncio
async def test_runtime_adds_memory_context_to_direct_and_orchestrator_paths(tmp_path) -> None:
    direct_runtime = build_runtime(tmp_path / "direct", route="haiku")
    direct_runtime.memory_client = FakeMemoryClient(enabled=True)
    await direct_runtime.start()
    try:
        direct_result = await direct_runtime.process_incoming_user_message(
            {
                "content": "What do you remember about the memory integration?",
                "channel": "desktop:desk_a",
                "metadata": {"platform": "desktop"},
            }
        )
        assert "Relevant long-term memory context" in direct_result["memory_context"]

        await direct_runtime.fulfill_processed_message(direct_result)
        assert "Memory integration work" in (
            direct_runtime.haiku_adapter.last_memory_context or ""
        )
    finally:
        await direct_runtime.stop()

    opus_runtime = build_runtime(tmp_path / "opus", route="opus")
    opus_runtime.memory_client = FakeMemoryClient(enabled=True)
    await opus_runtime.start()
    try:
        opus_result = await opus_runtime.process_incoming_user_message(
            {
                "content": "Continue the integration plan.",
                "channel": "desktop:desk_b",
                "metadata": {"platform": "desktop"},
            }
        )
        task = opus_runtime._build_orchestrator_task(  # noqa: SLF001 - intentional unit seam
            request_record=opus_result,
            session_id=opus_result["session_id"],
            request_id=opus_result["request_id"],
            channel=opus_result["channel"],
        )
        assert "Relevant long-term memory context" in str(task.input.get("memory_context") or "")
    finally:
        await opus_runtime.stop()


@pytest.mark.asyncio
async def test_runtime_suppresses_contested_core_facts_from_memory_context(tmp_path) -> None:
    class ContestedMemoryClient(FakeMemoryClient):
        async def build_prompt_context(
            self,
            *,
            query: str,
            max_results: int,
            token_budget: int,
            core_fact_max_chars: int,
            kinds: tuple[str, ...],
            include_diagnostics: bool = False,
        ) -> MemoryPromptContext:
            self.prompt_context_calls.append(
                {
                    "query": query,
                    "max_results": max_results,
                    "token_budget": token_budget,
                    "core_fact_max_chars": core_fact_max_chars,
                    "kinds": kinds,
                    "include_diagnostics": include_diagnostics,
                }
            )
            core_fact_items = [
                {
                    "memory_id": "mem_partner_name",
                    "title": "Partner name",
                    "content": "Praveen's partner is Priya.",
                    "canonical_key": "relationships.partner.name",
                    "priority": 100,
                    "always_include": True,
                    "tags": ["relationship"],
                }
            ]
            return MemoryPromptContext(
                core_fact_items=core_fact_items,
                core_facts_rendered=self.render_core_fact_block(core_fact_items=core_fact_items),
                recall_items=[],
                total_token_count=12,
                rendered=self.render_prompt_context(
                    core_fact_items=core_fact_items,
                    recall_items=[],
                ),
            )

    runtime = build_runtime(tmp_path, route="opus")
    runtime.memory_client = ContestedMemoryClient(enabled=True)
    await runtime.start()
    try:
        session_id = "sess_contested"
        await runtime.memory_write_core_fact(
            {
                "fact": "Praveen's partner is Priya.",
                "title": "Partner name",
                "canonical_key": "relationships.partner.name",
                "metadata": {
                    "session_id": session_id,
                    "request_id": "req_partner_write",
                    "task_id": "tsk_partner_write",
                },
                "provenance": {
                    "created_by": "cosmic/orchestrator:1.0.0",
                    "session_id": session_id,
                    "request_id": "req_partner_write",
                    "task_id": "tsk_partner_write",
                    "source_kind": "orchestrator_tool",
                },
            }
        )

        result = await runtime.process_incoming_user_message(
            {
                "content": "No. I didn't confirm that. That was your assumption.",
                "session_id": session_id,
                "channel": "desktop:desk_contested",
                "metadata": {"platform": "desktop"},
            }
        )

        memory_context = str(result.get("memory_context") or "")
        assert "Praveen's partner is Priya." not in memory_context
        active_working_set = result["active_working_set"]
        assert active_working_set["contested_memory_claims"][0]["canonical_key"] == "relationships.partner.name"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_ingests_completed_turn_as_episode(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="haiku")
    fake_memory = FakeMemoryClient(enabled=True)
    whatsapp_adapter = FakeWhatsAppChannelAdapter()
    runtime.memory_client = fake_memory
    runtime.registry.register(whatsapp_adapter)
    await whatsapp_adapter.start()
    await runtime.start()
    try:
        result = await runtime.process_incoming_user_message(
            {
                "content": "Remember this exchange.",
                "channel": "whatsapp:+12153079021",
                "metadata": {"platform": "whatsapp"},
            }
        )
        await runtime.fulfill_processed_message(result)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert len(fake_memory.episode_calls) == 1
        episode_payload = fake_memory.episode_calls[0]
        observations = episode_payload["observations"]
        assert observations[0]["role"] == "user"
        assert observations[0]["content"] == "Remember this exchange."
        assert observations[1]["role"] == "assistant"
        assert observations[1]["content"] == "Hello from fake adapter"
        assert episode_payload["kind"] == "transcript"
        assert episode_payload["episode_type"] == "conversation_turn"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_only_ingests_episode_after_delivered_response(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="haiku")
    fake_memory = FakeMemoryClient(enabled=True)
    flaky_channel = FlakyWhatsAppChannelAdapter()
    runtime.memory_client = fake_memory
    runtime.registry.register(flaky_channel)
    await flaky_channel.start()
    await runtime.start()
    try:
        result = await runtime.process_incoming_user_message(
            {
                "content": "Remember this only after delivery.",
                "channel": "whatsapp:+12153079021",
                "metadata": {"platform": "whatsapp"},
            }
        )
        await runtime.fulfill_processed_message(result)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert fake_memory.episode_calls == []

        flaky_channel.fail_response_complete = False
        runtime.notify_channel_active("whatsapp:+12153079021")

        deadline = time.time() + 3.0
        while time.time() < deadline:
            if fake_memory.episode_calls:
                break
            await asyncio.sleep(0.05)

        assert len(fake_memory.episode_calls) == 1
        assert fake_memory.episode_calls[0]["metadata"]["request_id"] == result["request_id"]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_rollover_writes_session_summary_to_memory(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="haiku")
    fake_memory = FakeMemoryClient(enabled=True)
    runtime.memory_client = fake_memory
    await runtime.start()
    try:
        old_session_id = "sess_20000101"
        runtime.session_store.append_message(
            old_session_id,
            role="user",
            content="We decided to ship the memory service.",
            channel="desktop:desk_a",
            metadata={"platform": "desktop"},
        )
        runtime.session_store.append_message(
            old_session_id,
            role="assistant",
            content="I will remember that and follow up tomorrow.",
            route="haiku",
            channel="desktop:desk_a",
            metadata={"platform": "desktop"},
        )

        await runtime._finalize_rollover_sessions(  # noqa: SLF001 - targeted rollover seam
            current_session_id=runtime._current_session_id()
        )

        transcript_path = runtime.config.session_transcript_dir / f"{old_session_id}.md"
        assert transcript_path.exists()
        assert len(fake_memory.write_calls) == 1
        summary_payload = fake_memory.write_calls[0]
        assert summary_payload["kind"] == "session_summary"
        assert summary_payload["metadata"]["session_id"] == old_session_id
        assert old_session_id not in {
            item["session_id"]
            for item in runtime.session_store.list_rollover_candidates(
                current_session_id=runtime._current_session_id()
            )
        }
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_active_working_set_only_keeps_unresolved_open_loops(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="haiku")
    await runtime.start()
    try:
        session_id = runtime._current_session_id()
        request_id = "req_open_loop_1"
        channel = "desktop:desk_a"
        assistant_text = "Can you clarify which repository you want me to use?"
        runtime.session_store.append_message(
            session_id,
            role="user",
            content="Please continue with the migration.",
            channel=channel,
            metadata={"platform": "desktop", "request_id": request_id},
        )
        assistant_message_id = runtime.session_store.append_message(
            session_id,
            role="assistant",
            content=assistant_text,
            route="haiku",
            awaiting_reply=True,
            channel=channel,
            metadata={"platform": "desktop", "request_id": request_id},
        )
        runtime.session_store.upsert_turn_ledger_entry(
            {
                "turn_id": "turn_req_open_loop_1",
                "request_id": request_id,
                "session_id": session_id,
                "channel": channel,
                "route": "haiku",
                "started_at": utcnow_iso(),
                "completed_at": utcnow_iso(),
                "user_goal": "Continue the migration.",
                "user_message_excerpt": "Please continue with the migration.",
                "assistant_outcome": assistant_text,
                "compact_line": "Continue the migration via haiku -> clarification requested",
                "open_loops": [assistant_text],
                "metadata": {"awaiting_reply": True},
            }
        )

        working_set = runtime._refresh_active_working_set(session_id)  # noqa: SLF001 - targeted unit seam
        assert assistant_text in working_set["open_loops"]

        runtime.session_store.clear_awaiting_reply(assistant_message_id)
        refreshed = runtime._refresh_active_working_set(session_id)  # noqa: SLF001 - targeted unit seam
        assert assistant_text not in refreshed["open_loops"]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_session_compaction_only_summarizes_newly_eligible_turns(tmp_path, monkeypatch) -> None:
    runtime = build_runtime(tmp_path, route="haiku")
    summary_prompts: list[str] = []

    async def fake_generate_text(*, system_prompt: str, messages, max_tokens: int) -> tuple[str, dict[str, object], str]:
        summary_prompts.append(str(messages[0]["content"]))
        return (
            "## Goal\n- Preserve the migration context.\n",
            {"output_tokens": 42},
            "end_turn",
        )

    runtime.haiku_adapter.generate_text = fake_generate_text
    monkeypatch.setattr("gateway.runtime.COMPACTION_RECENT_WINDOW_MESSAGES", 2)
    monkeypatch.setattr("gateway.runtime.COMPACTION_RAW_MESSAGE_CHAR_LIMIT", 2000)
    monkeypatch.setattr(runtime, "_compaction_trigger_threshold_tokens", lambda: 1)
    monkeypatch.setattr(runtime, "_conversation_context_budget_tokens", lambda: 100_000)

    def add_turn(index: int) -> None:
        request_id = f"req_compaction_{index}"
        channel = "desktop:desk_a"
        user_text = f"Question {index}: explain the migration plan in detail."
        assistant_text = f"Answer {index}: here is the migration guidance."
        runtime.session_store.append_message(
            session_id,
            role="user",
            content=user_text,
            channel=channel,
            metadata={"platform": "desktop", "request_id": request_id},
        )
        runtime.session_store.append_message(
            session_id,
            role="assistant",
            content=assistant_text,
            route="haiku",
            channel=channel,
            metadata={"platform": "desktop", "request_id": request_id},
        )
        runtime.session_store.upsert_turn_ledger_entry(
            {
                "turn_id": f"turn_{request_id}",
                "request_id": request_id,
                "session_id": session_id,
                "channel": channel,
                "route": "haiku",
                "started_at": utcnow_iso(),
                "completed_at": utcnow_iso(),
                "user_goal": user_text,
                "user_message_excerpt": user_text,
                "assistant_outcome": assistant_text,
                "compact_line": f"{user_text} via haiku -> {assistant_text}",
                "accomplished": [assistant_text],
                "metadata": {},
            }
        )
        time.sleep(0.01)

    await runtime.start()
    try:
        session_id = runtime._current_session_id()
        add_turn(1)
        add_turn(2)
        add_turn(3)

        await runtime._maybe_compact_session(session_id)  # noqa: SLF001 - targeted unit seam
        assert len(summary_prompts) == 1
        first_packet = runtime.get_session_state(session_id)["compaction_packet"]
        assert first_packet["compacted_until_completed_at"]

        await runtime._maybe_compact_session(session_id)  # noqa: SLF001 - targeted unit seam
        assert len(summary_prompts) == 1

        add_turn(4)
        await runtime._maybe_compact_session(session_id)  # noqa: SLF001 - targeted unit seam
        assert len(summary_prompts) == 2
        assert "Question 3" in summary_prompts[1]
        assert "Question 1" not in summary_prompts[1]
    finally:
        await runtime.stop()


@pytest.fixture
def test_client(tmp_path):
    runtime = build_runtime(tmp_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway_runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(channel_router)

    with TestClient(app) as client:
        yield client


def test_internal_memory_routes_proxy_to_memory_service(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    runtime.memory_client = FakeMemoryClient(enabled=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway_runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(channel_router)
    from gateway.memory_routes import router as memory_router

    app.include_router(memory_router)

    with TestClient(app) as client:
        search_response = client.post(
            "/internal/memory/search",
            headers={"X-Internal-Token": "internal-token"},
            json={
                "query": "memory integration",
                "kinds": ["task_summary"],
                "max_results": 4,
            },
        )
        assert search_response.status_code == 200
        payload = search_response.json()
        assert payload["items"][0]["memory_id"] == "mem_search_1"
        assert runtime.memory_client.search_calls[0]["query"] == "memory integration"

        memory_response = client.get(
            "/internal/memory/memories/mem_search_1",
            headers={"X-Internal-Token": "internal-token"},
        )
        assert memory_response.status_code == 200
        assert memory_response.json()["content"] == "This is the full canonical memory body."
        assert runtime.memory_client.memory_get_requests == ["mem_search_1"]

        core_fact_response = client.get(
            "/internal/memory/core-facts",
            headers={"X-Internal-Token": "internal-token"},
            params={"max_chars": 900},
        )
        assert core_fact_response.status_code == 200
        assert runtime.memory_client.core_fact_requests == [900]

        write_response = client.post(
            "/internal/memory/write",
            headers={"X-Internal-Token": "internal-token"},
            json={
                "content": "User prefers concise answers.",
                "kind": "preference",
                "metadata": {
                    "request_id": "req_memory_write_1",
                    "session_id": "sess_memory_write_1",
                    "task_id": "tsk_memory_write_1",
                    "stored_by": "cosmic/orchestrator:1.0.0",
                },
                "provenance": {
                    "created_by": "cosmic/orchestrator:1.0.0",
                    "request_id": "req_memory_write_1",
                    "session_id": "sess_memory_write_1",
                    "task_id": "tsk_memory_write_1",
                    "source_kind": "orchestrator_tool",
                },
            },
        )
        assert write_response.status_code == 201
        assert runtime.memory_client.write_calls[0]["kind"] == "user_data"

        core_fact_write_response = client.post(
            "/internal/memory/core-facts",
            headers={"X-Internal-Token": "internal-token"},
            json={
                "fact": "User prefers concise answers.",
                "canonical_key": "preferences.response_style",
                "metadata": {
                    "request_id": "req_core_fact_1",
                    "session_id": "sess_memory_write_1",
                    "task_id": "tsk_memory_write_1",
                    "stored_by": "cosmic/orchestrator:1.0.0",
                },
                "provenance": {
                    "created_by": "cosmic/orchestrator:1.0.0",
                    "request_id": "req_core_fact_1",
                    "session_id": "sess_memory_write_1",
                    "task_id": "tsk_memory_write_1",
                    "source_kind": "orchestrator_tool",
                },
            },
        )
        assert core_fact_write_response.status_code == 201
        assert runtime.memory_client.core_fact_write_calls[0]["canonical_key"] == "preferences.response_style"

        graph_status_response = client.get(
            "/internal/memory/graph-status",
            headers={"X-Internal-Token": "internal-token"},
        )
        assert graph_status_response.status_code == 200
        assert graph_status_response.json()["backend"] == "memory"

        graph_sync_response = client.post(
            "/internal/memory/graph-sync",
            headers={"X-Internal-Token": "internal-token"},
        )
        assert graph_sync_response.status_code == 200
        assert graph_sync_response.json()["mode"] == "sync"
        assert runtime.memory_client.graph_sync_calls[-1] == {}

        graph_rebuild_response = client.post(
            "/internal/memory/graph-rebuild",
            headers={"X-Internal-Token": "internal-token"},
        )
        assert graph_rebuild_response.status_code == 200
        assert graph_rebuild_response.json()["mode"] == "rebuild"
        assert runtime.memory_client.graph_rebuild_calls[-1] == {}

        graph_sync_with_payload = client.post(
            "/internal/memory/graph-sync",
            headers={"X-Internal-Token": "internal-token"},
            json={
                "allow_llm": True,
                "persist_graph_documents": True,
                "only_missing_graph_documents": True,
                "max_records": 10,
            },
        )
        assert graph_sync_with_payload.status_code == 200
        assert runtime.memory_client.graph_sync_calls[-1] == {
            "allow_llm": True,
            "persist_graph_documents": True,
            "only_missing_graph_documents": True,
            "max_records": 10,
        }

        audit_response = client.get(
            "/internal/memory/write-audit",
            headers={"X-Internal-Token": "internal-token"},
            params={"limit": 10, "session_id": "sess_memory_write_1"},
        )
        assert audit_response.status_code == 200
        entries = audit_response.json()["entries"]
        operations = {entry["operation"] for entry in entries}
        assert "memory_write" in operations
        assert "memory_write_core_fact" in operations
        assert any(entry["normalized_kind"] == "user_data" for entry in entries)
        assert any(entry["canonical_key"] == "preferences.response_style" for entry in entries)


@pytest.mark.asyncio
async def test_runtime_tool_memory_writes_are_deduplicated_and_audited(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="opus")
    fake_memory = FakeMemoryClient(enabled=True)
    runtime.memory_client = fake_memory
    runtime._redis = FakeRedis()
    await runtime.start()
    try:
        payload = {
            "content": "User prefers concise answers.",
            "kind": "preference",
            "metadata": {
                "request_id": "req_dedupe_1",
                "session_id": "sess_dedupe_1",
                "task_id": "tsk_dedupe_1",
                "channel": "desktop:desk_a",
                "stored_by": "cosmic/orchestrator:1.0.0",
            },
            "provenance": {
                "created_by": "cosmic/orchestrator:1.0.0",
                "request_id": "req_dedupe_1",
                "session_id": "sess_dedupe_1",
                "task_id": "tsk_dedupe_1",
                "channel": "desktop:desk_a",
                "source_kind": "orchestrator_tool",
            },
        }

        first = await runtime.memory_write(dict(payload))
        second = await runtime.memory_write(dict(payload))

        assert first["memory_id"] == "mem_write_1"
        assert first.get("deduplicated") is not True
        assert second["memory_id"] == "mem_write_1"
        assert second["deduplicated"] is True
        assert second["kind"] == "user_data"
        assert len(fake_memory.write_calls) == 1

        audit_entries = runtime.list_memory_write_audit(
            request_id="req_dedupe_1",
            writer_id="cosmic/orchestrator:1.0.0",
        )
        assert [entry["status"] for entry in audit_entries] == ["deduplicated", "saved"]
        assert audit_entries[0]["deduplicated"] is True
        assert audit_entries[1]["memory_id"] == "mem_write_1"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_tool_memory_writes_rate_limit_distinct_writes(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="opus")
    fake_memory = FakeMemoryClient(enabled=True)
    runtime.memory_client = fake_memory
    runtime._redis = FakeRedis()
    runtime.config.memory_write_max_per_hour = 1
    await runtime.start()
    try:
        base_metadata = {
            "session_id": "sess_rate_limit_1",
            "task_id": "tsk_rate_limit_1",
            "channel": "desktop:desk_a",
            "stored_by": "cosmic/orchestrator:1.0.0",
        }
        base_provenance = {
            "created_by": "cosmic/orchestrator:1.0.0",
            "session_id": "sess_rate_limit_1",
            "task_id": "tsk_rate_limit_1",
            "channel": "desktop:desk_a",
            "source_kind": "orchestrator_tool",
        }

        await runtime.memory_write(
            {
                "content": "First durable memory item.",
                "kind": "user_data",
                "metadata": {**base_metadata, "request_id": "req_rate_limit_1"},
                "provenance": {**base_provenance, "request_id": "req_rate_limit_1"},
            }
        )

        with pytest.raises(MemoryClientHTTPError) as exc_info:
            await runtime.memory_write(
                {
                    "content": "Second distinct durable memory item.",
                    "kind": "user_data",
                    "metadata": {**base_metadata, "request_id": "req_rate_limit_2"},
                    "provenance": {**base_provenance, "request_id": "req_rate_limit_2"},
                }
            )

        assert exc_info.value.status_code == 429
        assert len(fake_memory.write_calls) == 1
        audit_entries = runtime.list_memory_write_audit(
            writer_id="cosmic/orchestrator:1.0.0",
            session_id="sess_rate_limit_1",
            limit=5,
        )
        assert [entry["status"] for entry in audit_entries] == ["rate_limited", "saved"]
        assert audit_entries[0]["rate_limited"] is True
    finally:
        await runtime.stop()


def test_internal_session_routes_expose_state_turns_and_revisit(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="haiku")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway_runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(channel_router)
    from gateway.memory_routes import router as memory_router

    app.include_router(memory_router)

    with TestClient(app) as client:
        session_id = runtime._current_session_id()
        runtime.session_store.update_session_metadata(
            session_id,
            {
                "active_working_set": {
                    "session_id": session_id,
                    "goal": "Keep the migration plan moving.",
                    "active_workstreams": ["Finish memory integration"],
                    "recent_decisions": [],
                    "open_loops": ["Confirm rollout timing"],
                    "current_focus_entities": [],
                    "active_task_refs": ["task_123"],
                    "pending_artifact_pointers": [],
                    "user_preferences_in_play": ["Concise answers"],
                    "last_updated_at": utcnow_iso(),
                }
            },
        )
        runtime.session_store.append_message(
            session_id,
            role="user",
            content="Continue the migration.",
            channel="desktop:desk_a",
            metadata={"platform": "desktop", "request_id": "req_revisit_1"},
        )
        runtime.session_store.append_message(
            session_id,
            role="assistant",
            content="I will continue the migration and report back.",
            route="haiku",
            channel="desktop:desk_a",
            metadata={"platform": "desktop", "request_id": "req_revisit_1"},
        )
        runtime.session_store.upsert_turn_ledger_entry(
            {
                "turn_id": "turn_req_revisit_1",
                "request_id": "req_revisit_1",
                "session_id": session_id,
                "task_id": "task_123",
                "channel": "desktop:desk_a",
                "route": "haiku",
                "started_at": utcnow_iso(),
                "completed_at": utcnow_iso(),
                "user_goal": "Continue the migration.",
                "user_message_excerpt": "Continue the migration.",
                "assistant_outcome": "I will continue the migration and report back.",
                "compact_line": "Continue the migration via haiku -> I will continue the migration and report back.",
                "task_refs": ["task_123"],
                "metadata": {},
            }
        )
        runtime.session_store.upsert_task_notebook(
            "task_123",
            session_id,
            {
                "task_id": "task_123",
                "status": "active",
                "goal": "Finish the migration",
                "current_state": "Waiting on final verification",
                "open_questions": ["Confirm rollout timing"],
                "created_at": utcnow_iso(),
            },
        )

        state_response = client.get(
            f"/internal/session/state/{session_id}",
            headers={"X-Internal-Token": "internal-token"},
        )
        assert state_response.status_code == 200
        assert state_response.json()["active_working_set"]["goal"] == "Keep the migration plan moving."

        turns_response = client.get(
            f"/internal/session/turns/{session_id}",
            headers={"X-Internal-Token": "internal-token"},
            params={"limit": 5},
        )
        assert turns_response.status_code == 200
        assert turns_response.json()["turns"][0]["request_id"] == "req_revisit_1"

        history_response = client.get(
            f"/internal/session/history/{session_id}",
            headers={"X-Internal-Token": "internal-token"},
            params={"limit": 1, "offset": 1},
        )
        assert history_response.status_code == 200
        history_payload = history_response.json()
        assert history_payload["total_messages"] == 2
        assert history_payload["has_more"] is False
        assert history_payload["messages"][0]["content"] == "I will continue the migration and report back."

        notebook_response = client.get(
            "/internal/session/task-notebook/task_123",
            headers={"X-Internal-Token": "internal-token"},
        )
        assert notebook_response.status_code == 200
        assert notebook_response.json()["current_state"] == "Waiting on final verification"

        revisit_response = client.post(
            "/internal/session/revisit",
            headers={"X-Internal-Token": "internal-token"},
            json={
                "session_id": session_id,
                "task_id": "task_123",
                "request_id": "req_revisit_1",
                "turn_limit": 5,
                "raw_history_limit": 5,
            },
        )
        assert revisit_response.status_code == 200
        revisit_payload = revisit_response.json()
        assert revisit_payload["turn"]["request_id"] == "req_revisit_1"
        assert revisit_payload["task_notebook"]["task_id"] == "task_123"
        assert revisit_payload["raw_history"][-1]["content"] == "I will continue the migration and report back."


def test_internal_session_artifact_routes_search_and_resolve(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="haiku")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway_runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(channel_router)
    from gateway.memory_routes import router as memory_router

    app.include_router(memory_router)

    with TestClient(app) as client:
        session_id = runtime._current_session_id()
        artifact_path = runtime.config.artifacts_root / "tsk_export" / "out" / "example.csv"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("name,value\nAcme,1\n", encoding="utf-8")

        user_message_id = runtime.session_store.append_message(
            session_id,
            role="user",
            content="Make me a CSV file.",
            channel="desktop:desk_a",
            metadata={"platform": "desktop", "request_id": "req_artifact_1"},
        )
        assistant_message_id = runtime.session_store.append_message(
            session_id,
            role="assistant",
            content="Here is the CSV.",
            route="haiku",
            channel="desktop:desk_a",
            metadata={
                "platform": "desktop",
                "request_id": "req_artifact_1",
                "produced_artifacts": [
                    {
                        "artifact_id": "out_csv_1",
                        "task_id": "tsk_export",
                        "mime": "text/csv",
                        "path": "runs/artifacts/tsk_export/out/example.csv",
                        "filename": "example.csv",
                        "created_by_agent": "cosmic/tabular-agent:1.0.0",
                        "audience": "deliverable",
                    }
                ],
            },
        )
        runtime.session_store.upsert_turn_ledger_entry(
            {
                "turn_id": "turn_req_artifact_1",
                "request_id": "req_artifact_1",
                "session_id": session_id,
                "task_id": "tsk_parent",
                "channel": "desktop:desk_a",
                "route": "haiku",
                "started_at": utcnow_iso(),
                "completed_at": utcnow_iso(),
                "user_message_id": user_message_id,
                "assistant_message_id": assistant_message_id,
                "user_goal": "Create a CSV export",
                "user_message_excerpt": "Make me a CSV file.",
                "assistant_outcome": "Created example.csv",
                "compact_line": "Created example.csv",
                "task_refs": [],
                "artifact_refs": ["out_csv_1"],
                "metadata": {
                    "produced_artifacts": [
                        {
                            "artifact_id": "out_csv_1",
                            "task_id": "tsk_export",
                            "mime": "text/csv",
                            "path": "runs/artifacts/tsk_export/out/example.csv",
                            "filename": "example.csv",
                            "created_by_agent": "cosmic/tabular-agent:1.0.0",
                            "audience": "deliverable",
                        }
                    ]
                },
            }
        )

        search_response = client.post(
            "/internal/session/artifacts/search",
            headers={"X-Internal-Token": "internal-token"},
            json={"session_id": session_id, "query": "example", "limit": 5},
        )
        assert search_response.status_code == 200
        search_payload = search_response.json()
        assert search_payload["results"][0]["artifact_id"] == "out_csv_1"
        assert search_payload["results"][0]["downloadable"] is True
        assert search_payload["results"][0]["assistant_message_id"] == assistant_message_id

        resolve_response = client.post(
            "/internal/session/artifacts/resolve",
            headers={"X-Internal-Token": "internal-token"},
            json={"session_id": session_id, "artifact_ids": ["out_csv_1"]},
        )
        assert resolve_response.status_code == 200
        resolve_payload = resolve_response.json()
        assert resolve_payload["artifacts"][0]["artifact_id"] == "out_csv_1"
        assert resolve_payload["artifacts"][0]["path"] == "runs/artifacts/tsk_export/out/example.csv"


def test_desktop_websocket_supports_ping_query_and_resume(test_client: TestClient) -> None:
    with test_client.websocket_connect("/ws?token=test-token&device_id=desk_a1b2") as websocket:
        websocket.send_json({"type": "ping", "ts_unix_ms": 12345})
        pong = websocket.receive_json()
        assert pong["type"] == "pong"
        assert pong["ts_unix_ms"] == 12345

        websocket.send_json(
            {
                "type": "query",
                "request_id": "req_001",
                "content": "Hello from desktop",
                "conversation_context": [
                    {"role": "user", "content": "Prior context"},
                ],
            }
        )
        route_result = websocket.receive_json()
        assert route_result["type"] == "route_result"
        assert route_result["request_id"] == "req_001"
        assert route_result["route"] == "haiku"
        assert route_result["channel"] == "desktop:desk_a1b2"

        chunk = websocket.receive_json()
        assert chunk["type"] == "response.chunk"
        assert chunk["request_id"] == "req_001"
        assert chunk["session_id"] == route_result["session_id"]
        assert chunk["content"] == "Hello"
        assert chunk["done"] is False
        assert chunk["channel"] == "desktop:desk_a1b2"

        complete = websocket.receive_json()
        assert complete["type"] == "response.complete"
        assert complete["request_id"] == "req_001"
        assert complete["session_id"] == route_result["session_id"]
        assert complete["route"] == "haiku"
        assert complete["content"] == "Hello from fake adapter"

        websocket.send_json(
            {
                "type": "resume",
                "request_id": "resume_001",
                "session_id": route_result["session_id"],
                "known_task_ids": [],
            }
        )
        resume = websocket.receive_json()
        assert resume["type"] == "resume.ok"
        assert resume["request_id"] == "resume_001"
        assert resume["session_id"] == route_result["session_id"]
        assert resume["channel"] == "desktop:desk_a1b2"
        assert resume["user_timezone"] == "America/Chicago"
        assert resume["history_tail"][-1]["content"] == "Hello from fake adapter"


def test_desktop_resume_includes_foreground_active_request(test_client: TestClient) -> None:
    runtime = test_client.app.state.gateway_runtime
    session_id = runtime._current_session_id()
    runtime.active_requests["req_running_resume"] = ActiveRequest(
        request_id="req_running_resume",
        session_id=session_id,
        channel="desktop:desk_running_resume",
        route="opus",
        task_id="task_running_resume",
        partial_content="Partial streamed answer",
        partial_thinking="Partial streamed thinking",
        activity="",
    )
    runtime._track_forwarded_foreground_event(
        {
            "type": "task.progress",
            "request_id": "req_running_resume",
            "task_id": "task_running_resume",
            "message": "Slide agent accepted slide.edit.",
        }
    )

    with test_client.websocket_connect("/ws?token=test-token&device_id=desk_running_resume") as websocket:
        websocket.send_json(
            {
                "type": "resume",
                "request_id": "resume_running_001",
                "session_id": session_id,
                "known_task_ids": [],
            }
        )
        resume = websocket.receive_json()

    assert resume["type"] == "resume.ok"
    foreground_streams = resume["foreground_streams"]
    assert len(foreground_streams) == 1
    assert foreground_streams[0]["request_id"] == "req_running_resume"
    assert foreground_streams[0]["task_id"] == "task_running_resume"
    assert foreground_streams[0]["session_id"] == session_id
    assert foreground_streams[0]["route"] == "opus"
    assert foreground_streams[0]["content"] == "Partial streamed answer"
    assert foreground_streams[0]["thinking_text"] == "Partial streamed thinking"
    assert foreground_streams[0]["channel"] == "desktop:desk_running_resume"
    assert foreground_streams[0]["activity"] == "Slide agent accepted slide.edit."
    assert foreground_streams[0]["completed"] is False
    assert foreground_streams[0]["failed"] is False
    assert foreground_streams[0]["updated_at"]


def test_desktop_resume_includes_recent_failed_foreground_stream(test_client: TestClient) -> None:
    runtime = test_client.app.state.gateway_runtime
    session_id = runtime._current_session_id()
    runtime._append_session_message(
        session_id,
        role="user",
        content="Why did the stream fail?",
        route="opus",
        channel="desktop:desk_failed_resume",
        metadata={"request_id": "req_failed_resume"},
    )
    failed_state = ActiveRequest(
        request_id="req_failed_resume",
        session_id=session_id,
        channel="desktop:desk_failed_resume",
        route="opus",
        task_id="task_failed_resume",
        partial_content="Partial streamed answer",
        partial_thinking="Partial streamed thinking",
        response_blocks_snapshot=[{"type": "markdown", "text": "Partial streamed answer"}],
        completed=True,
        failed=True,
        activity="Still recovering...",
        error_message="Anthropic API error: upstream failed",
    )
    runtime._cache_recent_foreground_terminal_stream(failed_state)

    with test_client.websocket_connect("/ws?token=test-token&device_id=desk_failed_resume") as websocket:
        websocket.send_json(
            {
                "type": "resume",
                "request_id": "resume_failed_001",
                "session_id": session_id,
                "known_task_ids": [],
            }
        )
        resume = websocket.receive_json()

    assert resume["type"] == "resume.ok"
    foreground_streams = resume["foreground_streams"]
    assert len(foreground_streams) == 1
    assert foreground_streams[0]["request_id"] == "req_failed_resume"
    assert foreground_streams[0]["task_id"] == "task_failed_resume"
    assert foreground_streams[0]["session_id"] == session_id
    assert foreground_streams[0]["route"] == "opus"
    assert foreground_streams[0]["content"] == "Partial streamed answer"
    assert foreground_streams[0]["thinking_text"] == "Partial streamed thinking"
    assert foreground_streams[0]["channel"] == "desktop:desk_failed_resume"
    assert foreground_streams[0]["completed"] is True
    assert foreground_streams[0]["failed"] is True
    assert foreground_streams[0]["error"] == "Anthropic API error: upstream failed"
    assert foreground_streams[0]["updated_at"]


def test_failed_foreground_response_persists_to_history(test_client: TestClient) -> None:
    runtime = test_client.app.state.gateway_runtime
    session_id = runtime._current_session_id()
    runtime._append_session_message(
        session_id,
        role="user",
        content="Market cap, P/E ratio, profit margin, dividend yield (%) for Apple, Microsoft, Google",
        route="opus",
        channel="desktop:desk_failed_history",
        metadata={"request_id": "req_failed_history"},
    )
    failed_state = ActiveRequest(
        request_id="req_failed_history",
        session_id=session_id,
        channel="desktop:desk_failed_history",
        route="opus",
        task_id="task_failed_history",
        partial_content="Apple market cap is...",
        partial_thinking="Fetching the latest financial metrics.",
        response_blocks_snapshot=[{"type": "markdown", "text": "Apple market cap is..."}],
        completed=True,
        failed=True,
        error_message="Anthropic API error: upstream failed",
    )

    assert runtime._persist_failed_foreground_response(failed_state) is True

    with test_client.websocket_connect("/ws?token=test-token&device_id=desk_failed_history") as websocket:
        websocket.send_json(
            {
                "type": "resume",
                "request_id": "resume_failed_history_001",
                "session_id": session_id,
                "known_task_ids": [],
            }
        )
        resume = websocket.receive_json()

    assert resume["type"] == "resume.ok"
    assert resume["foreground_streams"] == []
    stored_message = next(
        item
        for item in resume["history_tail"]
        if item["role"] == "assistant"
        and item["metadata"]["request_id"] == "req_failed_history"
    )
    assert stored_message["content"] == "Apple market cap is..."
    assert stored_message["metadata"]["failed"] is True
    assert stored_message["metadata"]["partial_response"] is True
    assert stored_message["metadata"]["error"] == "Anthropic API error: upstream failed"
    assert stored_message["metadata"]["thinking_text"] == "Fetching the latest financial metrics."
    assert stored_message["metadata"]["response_blocks"][0]["text"] == "Apple market cap is..."


def test_desktop_resume_includes_artifact_only_assistant_message(test_client: TestClient) -> None:
    runtime = test_client.app.state.gateway_runtime
    session_id = runtime._current_session_id()
    artifact = {
        "artifact_id": "artifact_deck_resume",
        "filename": "deck.pptx",
        "mime_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "kind": "presentation",
    }
    message_id = runtime._append_session_message(
        session_id,
        role="assistant",
        content="",
        route="opus",
        channel="desktop:desk_artifact_resume",
        metadata={
            "request_id": "req_artifact_resume",
            "produced_artifacts": [artifact],
            "response_blocks": [
                {
                    "type": "file_artifact",
                    "artifact_id": "artifact_deck_resume",
                    "filename": "deck.pptx",
                }
            ],
        },
    )
    assert message_id

    with test_client.websocket_connect("/ws?token=test-token&device_id=desk_artifact_resume") as websocket:
        websocket.send_json(
            {
                "type": "resume",
                "request_id": "resume_artifact_001",
                "session_id": session_id,
                "known_task_ids": [],
            }
        )
        resume = websocket.receive_json()

    assert resume["type"] == "resume.ok"
    stored_message = next(
        item for item in resume["history_tail"] if item["message_id"] == message_id
    )
    assert stored_message["role"] == "assistant"
    assert stored_message["content"] == ""
    metadata = stored_message["metadata"]
    assert metadata["produced_artifacts"][0]["artifact_id"] == "artifact_deck_resume"
    assert metadata["response_blocks"][0]["type"] == "file_artifact"


def test_mobile_websocket_supports_ping_query_and_resume(test_client: TestClient) -> None:
    with test_client.websocket_connect("/ws?token=test-token&device_id=mob_a1b2") as websocket:
        websocket.send_json({"type": "ping", "ts_unix_ms": 54321})
        pong = websocket.receive_json()
        assert pong["type"] == "pong"
        assert pong["ts_unix_ms"] == 54321

        websocket.send_json(
            {
                "type": "query",
                "request_id": "req_mobile_001",
                "content": "Hello from mobile",
                "conversation_context": [
                    {"role": "user", "content": "Prior mobile context"},
                ],
            }
        )
        route_result = websocket.receive_json()
        assert route_result["type"] == "route_result"
        assert route_result["request_id"] == "req_mobile_001"
        assert route_result["route"] == "haiku"
        assert route_result["channel"] == "mobile:mob_a1b2"

        chunk = websocket.receive_json()
        assert chunk["type"] == "response.chunk"
        assert chunk["request_id"] == "req_mobile_001"
        assert chunk["session_id"] == route_result["session_id"]
        assert chunk["channel"] == "mobile:mob_a1b2"

        complete = websocket.receive_json()
        assert complete["type"] == "response.complete"
        assert complete["request_id"] == "req_mobile_001"
        assert complete["session_id"] == route_result["session_id"]
        assert complete["route"] == "haiku"
        assert complete["content"] == "Hello from fake adapter"
        assert complete["channel"] == "mobile:mob_a1b2"

        websocket.send_json(
            {
                "type": "resume",
                "request_id": "resume_mobile_001",
                "session_id": route_result["session_id"],
                "known_task_ids": [],
            }
        )
        resume = websocket.receive_json()
        assert resume["type"] == "resume.ok"
        assert resume["request_id"] == "resume_mobile_001"
        assert resume["session_id"] == route_result["session_id"]
        assert resume["channel"] == "mobile:mob_a1b2"
        assert resume["history_tail"][-1]["content"] == "Hello from fake adapter"


@pytest.mark.asyncio
async def test_runtime_uses_reported_desktop_timezone_for_session_rollover_and_cron(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    await runtime.start()
    try:
        await runtime.update_user_timezone("Asia/Kolkata", source="desktop")

        session_id = runtime._current_session_id(
            datetime(2026, 3, 12, 1, 0, tzinfo=timezone.utc)
        )
        assert session_id == "sess_20260312"

        profile = runtime.scheduler_store.get_profile()
        assert profile["user_timezone"] == "Asia/Kolkata"
        assert profile["timezone_source"] == "desktop"

        rollover_cron = runtime.scheduler_store.get_cron(SYSTEM_CRON_DAILY_ROLLOVER)
        assert rollover_cron is not None
        assert rollover_cron["timezone"] == "Asia/Kolkata"
        assert rollover_cron["cron_expr"] == "0 4 * * *"
    finally:
        await runtime.stop()


def test_desktop_resume_updates_scheduler_timezone_profile(test_client: TestClient) -> None:
    runtime = test_client.app.state.gateway_runtime

    with test_client.websocket_connect("/ws?token=test-token&device_id=desk_tz") as websocket:
        websocket.send_json(
            {
                "type": "resume",
                "request_id": "resume_tz_001",
                "timezone": "Asia/Kolkata",
                "session_id": "",
                "known_task_ids": [],
            }
        )
        resume = websocket.receive_json()

    assert resume["type"] == "resume.ok"
    assert resume["user_timezone"] == "Asia/Kolkata"
    assert runtime.scheduler_store.get_profile()["user_timezone"] == "Asia/Kolkata"


def test_channels_endpoint_lists_desktop(test_client: TestClient) -> None:
    response = test_client.get(
        "/channels",
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "channels": [
            {
                "platform": "desktop",
                "configured": True,
                "healthy": True,
                "last_error": None,
            }
        ]
    }


def test_desktop_system_metrics_endpoint_returns_cached_snapshot(test_client: TestClient) -> None:
    runtime = test_client.app.state.gateway_runtime
    calls: list[bool] = []

    async def fake_get_desktop_system_metrics(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(bool(kwargs.get("force_refresh")))
        return {
            "sourceEndpoint": "/desktop/system-metrics",
            "services": [{"name": "Gateway", "status": "active", "summary": "1/1 channels healthy"}],
            "providers": [],
            "usage_by_feature": [],
        }

    runtime.get_desktop_system_metrics = fake_get_desktop_system_metrics  # type: ignore[method-assign]

    response = test_client.get(
        "/desktop/system-metrics",
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200
    assert response.json()["sourceEndpoint"] == "/desktop/system-metrics"

    force_response = test_client.get(
        "/desktop/system-metrics?force_refresh=true",
        headers={"Authorization": "Bearer test-token"},
    )
    assert force_response.status_code == 200
    assert calls == [False, True]


def test_sessions_endpoints_return_vm_history(test_client: TestClient) -> None:
    with test_client.websocket_connect("/ws?token=test-token&device_id=desk_history") as websocket:
        websocket.send_json(
            {
                "type": "query",
                "request_id": "req_history",
                "content": "Save this to shared history",
            }
        )
        route_result = websocket.receive_json()
        assert route_result["type"] == "route_result"
        websocket.receive_json()
        websocket.receive_json()

    sessions_response = test_client.get(
        "/sessions",
        headers={"Authorization": "Bearer test-token"},
    )
    assert sessions_response.status_code == 200
    sessions = sessions_response.json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["id"] == route_result["session_id"]

    history_response = test_client.get(
        f"/sessions/{route_result['session_id']}",
        headers={"Authorization": "Bearer test-token"},
    )
    assert history_response.status_code == 200
    history_payload = history_response.json()
    assert history_payload["session_id"] == route_result["session_id"]
    assert [item["role"] for item in history_payload["messages"]] == ["user", "assistant"]


def test_routing_audit_endpoint_returns_effective_route_details(test_client: TestClient) -> None:
    with test_client.websocket_connect("/ws?token=test-token&device_id=desk_audit") as websocket:
        websocket.send_json(
            {
                "type": "query",
                "request_id": "req_audit",
                "content": "what is usd to inr today?",
            }
        )
        websocket.receive_json()
        websocket.receive_json()
        websocket.receive_json()

    response = test_client.get(
        "/routing-audit?limit=5",
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["entries"]) == 1
    entry = payload["entries"][0]
    assert entry["request_id"] == "req_audit"
    assert entry["decision_source"] == "model_router"
    assert entry["final_route"] == "haiku"
    assert entry["classifier_route"] == "haiku"
    assert entry["classifier_model"] == "openai/gpt-oss-20b"
    assert entry["classifier_metrics"] == {"rtt_ms": 18.5}
    assert entry["query_text"] == "what is usd to inr today?"


def test_scheduler_endpoints_list_and_pause_resume_system_cron(test_client: TestClient) -> None:
    overview = test_client.get(
        "/scheduler/overview",
        headers={"Authorization": "Bearer test-token"},
    )
    assert overview.status_code == 200
    overview_payload = overview.json()
    assert overview_payload["profile"]["user_timezone"] == "America/Chicago"
    assert any(item["cron_id"] == SYSTEM_CRON_DAILY_ROLLOVER for item in overview_payload["crons"])

    cron_response = test_client.get(
        f"/scheduler/crons/{SYSTEM_CRON_DAILY_ROLLOVER}",
        headers={"Authorization": "Bearer test-token"},
    )
    assert cron_response.status_code == 200
    assert cron_response.json()["cron_id"] == SYSTEM_CRON_DAILY_ROLLOVER

    paused = test_client.post(
        f"/scheduler/crons/{SYSTEM_CRON_DAILY_ROLLOVER}/pause",
        headers={"Authorization": "Bearer test-token"},
        json={"reason": "testing"},
    )
    assert paused.status_code == 200
    assert paused.json()["paused"] is True
    assert paused.json()["pause_reason"] == "testing"

    resumed = test_client.post(
        f"/scheduler/crons/{SYSTEM_CRON_DAILY_ROLLOVER}/resume",
        headers={"Authorization": "Bearer test-token"},
    )
    assert resumed.status_code == 200
    assert resumed.json()["paused"] is False

    heartbeat = test_client.get(
        "/scheduler/heartbeat",
        headers={"Authorization": "Bearer test-token"},
    )
    assert heartbeat.status_code == 200
    assert heartbeat.json()["timezone"] == "America/Chicago"


def test_scheduler_cron_helper_uses_user_local_timezone_and_validates_input() -> None:
    next_fire_at = compute_next_fire_at(
        "0 6 * * *",
        "America/Chicago",
        after=datetime(2026, 3, 16, 7, 30, tzinfo=timezone.utc),
    )
    assert next_fire_at == "2026-03-16T11:00:00Z"

    with pytest.raises(CronExpressionError):
        compute_next_fire_at("bad cron", "America/Chicago")

    with pytest.raises(CronExpressionError):
        compute_next_fire_at("0 6 * * *", "Mars/Phobos")


def test_internal_scheduler_crud_defaults_to_user_timezone_snapshot(test_client: TestClient) -> None:
    create_response = test_client.post(
        "/internal/scheduler/crons",
        headers={"X-Internal-Token": "internal-token"},
        json={
            "label": "Morning YC check",
            "cron_expression": "0 6 * * *",
            "prompt": "Check for newly added YC companies and report the diff.",
            "one_shot": True,
            "channel": "desktop:desk_sched",
            "request_id": "req_sched_create",
            "session_id": "sess_sched_create",
            "source": "orchestrator",
        },
    )
    assert create_response.status_code == 200
    created = create_response.json()
    cron_id = created["cron_id"]
    assert cron_id.startswith("cron_")
    assert created["timezone"] == "America/Chicago"
    assert created["delivery_channel"] == "desktop:desk_sched"
    assert created["prompt"] == "Check for newly added YC companies and report the diff."
    assert created["one_shot"] is True
    assert created["next_fire_at"]
    assert created["next_fire_local"]

    listed = test_client.get(
        "/internal/scheduler/crons",
        headers={"X-Internal-Token": "internal-token"},
    )
    assert listed.status_code == 200
    listed_ids = {item["cron_id"] for item in listed.json()["crons"]}
    assert cron_id in listed_ids
    assert SYSTEM_CRON_DAILY_ROLLOVER not in listed_ids

    fetched = test_client.get(
        f"/internal/scheduler/crons/{cron_id}",
        headers={"X-Internal-Token": "internal-token"},
    )
    assert fetched.status_code == 200
    assert fetched.json()["created_request_id"] == "req_sched_create"
    assert fetched.json()["created_session_id"] == "sess_sched_create"

    deleted = test_client.delete(
        f"/internal/scheduler/crons/{cron_id}",
        headers={"X-Internal-Token": "internal-token"},
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True, "cron_id": cron_id}

    missing = test_client.get(
        f"/internal/scheduler/crons/{cron_id}",
        headers={"X-Internal-Token": "internal-token"},
    )
    assert missing.status_code == 404


def test_internal_scheduler_create_rejects_bad_cron_or_timezone(test_client: TestClient) -> None:
    bad_cron = test_client.post(
        "/internal/scheduler/crons",
        headers={"X-Internal-Token": "internal-token"},
        json={
            "label": "Broken cron",
            "cron_expression": "not-a-cron",
            "prompt": "Do something later.",
        },
    )
    assert bad_cron.status_code == 400

    bad_timezone = test_client.post(
        "/internal/scheduler/crons",
        headers={"X-Internal-Token": "internal-token"},
        json={
            "label": "Wrong timezone",
            "cron_expression": "0 6 * * *",
            "prompt": "Do something later.",
            "timezone": "Mars/Phobos",
        },
    )
    assert bad_timezone.status_code == 400


def test_internal_channel_resolve_defaults_to_current_and_can_pick_linked_whatsapp(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    runtime.registry.register(FakeWhatsAppChannelAdapter())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway_runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(channel_router)

    with TestClient(app) as client:
        runtime.notify_channel_active("whatsapp:+12153079021")

        default_response = client.post(
            "/internal/channels/resolve",
            headers={"X-Internal-Token": "internal-token"},
            json={
                "current_channel": "desktop:desk_sched",
            },
        )
        assert default_response.status_code == 200
        assert default_response.json() == {
            "delivery_target": "desktop:desk_sched",
            "resolved_channel": "desktop:desk_sched",
            "platform": "desktop",
            "matched_by": "current_channel",
        }

        whatsapp_response = client.post(
            "/internal/channels/resolve",
            headers={"X-Internal-Token": "internal-token"},
            json={
                "delivery_target": "whatsapp",
                "current_channel": "desktop:desk_sched",
            },
        )
        assert whatsapp_response.status_code == 200
        assert whatsapp_response.json() == {
            "delivery_target": "whatsapp",
            "resolved_channel": "whatsapp:+12153079021",
            "platform": "whatsapp",
            "matched_by": "linked_channel",
        }


def test_internal_channel_resolve_supports_agent_email_aliases_and_explicit_channel(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    runtime.config.enable_agent_email = True
    runtime.config.cosmic_mail_primary_mailbox_address = "assistant@example.com"
    runtime.registry.register(FakeAgentEmailChannelAdapter())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway_runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(channel_router)

    with TestClient(app) as client:
        alias_response = client.post(
            "/internal/channels/resolve",
            headers={"X-Internal-Token": "internal-token"},
            json={
                "delivery_target": "email",
                "current_channel": "desktop:desk_sched",
            },
        )
        assert alias_response.status_code == 200
        assert alias_response.json() == {
            "delivery_target": "agent-email",
            "resolved_channel": "agent-email:assistant@example.com",
            "platform": "agent-email",
            "matched_by": "linked_channel",
        }

        explicit_response = client.post(
            "/internal/channels/resolve",
            headers={"X-Internal-Token": "internal-token"},
            json={
                "delivery_target": "agent-email:ops@example.com",
                "current_channel": "desktop:desk_sched",
            },
        )
        assert explicit_response.status_code == 200
        assert explicit_response.json() == {
            "delivery_target": "agent-email:ops@example.com",
            "resolved_channel": "agent-email:ops@example.com",
            "platform": "agent-email",
            "matched_by": "explicit_channel",
        }


@pytest.mark.asyncio
async def test_agent_email_status_respects_explicit_disconnect_over_env(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    runtime.config.enable_agent_email = True
    runtime.config.cosmic_mail_base_url = "https://env-mail.example.com"
    runtime.config.cosmic_mail_api_token = "env-token"
    runtime.config.cosmic_mail_primary_mailbox_address = "assistant@example.com"
    runtime.agent_email_integration_store = AgentEmailIntegrationStore(tmp_path / "agent_email_integrations.db")
    runtime.agent_email_integration_store.clear_primary()

    status = await runtime.get_agent_email_connection_status()

    assert status["configured"] is False
    assert status["explicitly_disconnected"] is True
    assert status["config_source"] == "integration_store_disabled"
    assert status["base_url"] == ""
    assert status["api_token"] == ""
    assert status["trusted_senders"] == []


@pytest.mark.asyncio
async def test_agent_email_trusted_senders_sync_to_status_even_without_connection(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    runtime.agent_email_integration_store = AgentEmailIntegrationStore(tmp_path / "agent_email_integrations.db")

    status = await runtime.save_agent_email_trusted_senders(["Owner@Example.com", "ops@example.com", "owner@example.com"])

    assert status["configured"] is False
    assert status["trusted_senders"] == ["owner@example.com", "ops@example.com"]
    persisted = runtime.agent_email_integration_store.get_primary()
    assert persisted is not None
    assert persisted.trusted_senders == ("owner@example.com", "ops@example.com")


@pytest.mark.asyncio
async def test_runtime_executes_due_custom_one_shot_cron_via_orchestrator(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="opus")
    await runtime.start()
    try:
        runtime.scheduler_store.upsert_cron(
            cron_id="cron_due_once",
            name="Morning YC diff",
            kind="reminder",
            description="Check the YC company list and report any changes.",
            cron_expr="0 6 * * *",
            timezone_name="America/Chicago",
            next_fire_at="2000-01-01T00:00:00Z",
            metadata={
                "prompt": "Check if any new YC companies were added and report the diff.",
                "one_shot": True,
                "delivery_channel": "desktop",
                "created_by": "orchestrator",
            },
        )

        await runtime._run_due_crons()  # noqa: SLF001 - targeted scheduler seam

        cron_record = runtime.get_scheduler_cron("cron_due_once")
        assert cron_record is not None
        assert cron_record["last_result_status"] == "completed"
        assert cron_record["next_fire_at"] is None

        task = runtime.orchestrator.last_task
        assert task is not None
        assert task.source == "cron"
        assert task.source_id == "cron_due_once"
        assert task.channel == "desktop"
        assert task.input["query"] == "Check if any new YC companies were added and report the diff."
        assert task.input["user_timezone"] == "America/Chicago"

        session_id = task.session_id
        assert session_id is not None
        history = runtime.get_session_history(session_id)
        assert [item["role"] for item in history] == ["assistant"]
        assert history[0]["content"] == "Thin Opus answer"
        assert history[0]["metadata"]["source"] == "cron"
        assert history[0]["metadata"]["source_id"] == "cron_due_once"

        notebook = runtime.get_task_notebook(task.task_id)
        assert notebook is not None
        assert notebook["goal"] == "Check if any new YC companies were added and report the diff."
        assert runtime.session_store.get_turn_ledger_entry(task.input["request_id"]) is None
        assert runtime.list_scheduler_crons(include_system=False, active_only=True) == []
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_due_heartbeat_suppresses_noop_and_does_not_append_history(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="opus")
    runtime.orchestrator = FakeHeartbeatNoopOrchestratorClient()
    await runtime.start()
    try:
        runtime.config.heartbeat_notes_path.write_text(
            "# COSMIC Heartbeat Notes\n\n- Watch current AI research/news when it matters.\n",
            encoding="utf-8",
        )
        runtime.scheduler_store.schedule_heartbeat(
            next_fire_at="2000-01-01T00:00:00Z"
        )

        await runtime._run_due_crons()  # noqa: SLF001 - targeted scheduler seam

        task = runtime.orchestrator.last_task
        assert task is not None
        assert task.source == "heartbeat"
        assert task.source_id == "default"
        assert task.priority == "low"
        assert task.channel == "desktop"
        assert task.input["conversation_context"] == []
        assert task.input["visual_response_enhancement_enabled"] is False
        assert "triggered automatically by COSMIC's scheduler" in task.input["query"]
        assert "Heartbeat Context" in str(task.input["memory_context"] or "")
        assert "automatic scheduler-triggered heartbeat" in str(task.input["memory_context"] or "")
        assert "Heartbeat Runtime State" in str(task.input["memory_context"] or "")
        assert "Projected next fire after this run" in str(task.input["memory_context"] or "")
        assert "Heartbeat Notes" in str(task.input["memory_context"] or "")
        assert "Watch current AI research/news" in str(task.input["memory_context"] or "")

        heartbeat = runtime.scheduler_store.get_heartbeat()
        assert heartbeat["last_result_status"] == "suppressed"
        assert heartbeat["next_fire_at"] is not None
        assert heartbeat["last_suppressed_at"] is not None

        assert runtime.get_session_history(task.session_id) == []
        assert runtime._heartbeat_activity_by_request_id == {}  # noqa: SLF001 - verifies suppressed beats stay ephemeral
    finally:
        await runtime.stop()


def test_scheduler_store_annotates_heartbeat_calendar_event_dedupe(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="opus")
    runtime.scheduler_store.initialize(default_timezone="America/Chicago")
    event = {
        "account_id": "acct_google_1",
        "account_label": "Work",
        "calendar_id": "primary",
        "calendar_name": "Primary",
        "event_id": "evt_pitch",
        "summary": "Pitch prep",
        "start": "2026-05-21T16:00:00Z",
        "end": "2026-05-21T16:30:00Z",
        "location": "Zoom",
    }

    first = runtime.scheduler_store.annotate_heartbeat_calendar_events(
        [event],
        included_at="2026-05-21T14:00:00Z",
    )
    assert first["new_event_count"] == 1
    assert first["seen_event_count"] == 0
    assert first["changed_event_count"] == 0
    assert first["events"][0]["heartbeat_new"] is True
    assert first["events"][0]["heartbeat_seen_before"] is False

    second = runtime.scheduler_store.annotate_heartbeat_calendar_events(
        [event],
        included_at="2026-05-21T14:30:00Z",
    )
    assert second["new_event_count"] == 0
    assert second["seen_event_count"] == 1
    assert second["changed_event_count"] == 0
    assert second["events"][0]["heartbeat_new"] is False
    assert second["events"][0]["heartbeat_seen_before"] is True
    assert second["events"][0]["heartbeat_previous_seen_at"] == "2026-05-21T14:00:00Z"

    changed = dict(event, summary="Pitch prep with Arun")
    third = runtime.scheduler_store.annotate_heartbeat_calendar_events(
        [changed],
        included_at="2026-05-21T15:00:00Z",
    )
    assert third["new_event_count"] == 0
    assert third["seen_event_count"] == 1
    assert third["changed_event_count"] == 1
    assert third["events"][0]["heartbeat_changed"] is True
    assert third["events"][0]["heartbeat_last_changed_at"] == "2026-05-21T15:00:00Z"


@pytest.mark.asyncio
async def test_runtime_heartbeat_calendar_digest_queries_multiple_accounts(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="opus")
    await runtime.start()
    try:
        runtime._redis = FakeRedis()  # noqa: SLF001 - avoids live Redis for this bounded dispatch seam
        accounts = [
            {
                "account_id": "acct_work",
                "account_label": "Work",
                "email": "work@example.com",
                "status": "active",
                "has_refresh_token": True,
                "is_primary": True,
            },
            {
                "account_id": "acct_personal",
                "account_label": "Personal",
                "email": "me@example.com",
                "status": "active",
                "has_refresh_token": True,
                "is_primary": False,
            },
        ]

        async def resolve_credential(**kwargs):
            return {"access_token": f"token-{kwargs['account_id']}"}

        async def dispatch_digest(**kwargs):
            account = kwargs["account_summary"]
            return {
                "account": {
                    **account,
                    "status": "ready",
                    "calendar_count": 1,
                    "event_count": 1,
                },
                "events": [
                    {
                        "event_id": f"evt_{account['account_id']}",
                        "summary": f"{account['account_label']} event",
                        "start": "2026-05-21T16:00:00Z",
                        "end": "2026-05-21T16:30:00Z",
                        "account_id": account["account_id"],
                        "account_label": account["account_label"],
                        "email": account["email"],
                        "calendar_id": "primary",
                        "calendar_name": "Primary",
                        "status": "confirmed",
                    }
                ],
            }

        with (
            patch.object(runtime.credential_manager, "list_accounts", return_value=accounts),
            patch.object(
                runtime.credential_manager,
                "resolve_credential",
                AsyncMock(side_effect=resolve_credential),
            ),
            patch.object(
                runtime,
                "_dispatch_calendar_heartbeat_digest",
                AsyncMock(side_effect=dispatch_digest),
            ),
        ):
            first = await runtime._build_heartbeat_calendar_digest(  # noqa: SLF001 - targeted heartbeat context seam
                session_id="sess_heartbeat_calendar",
                channel="desktop",
                scheduled_for="2026-05-21T14:00:00Z",
            )
            second = await runtime._build_heartbeat_calendar_digest(  # noqa: SLF001 - verifies dedupe across beats
                session_id="sess_heartbeat_calendar",
                channel="desktop",
                scheduled_for="2026-05-21T14:30:00Z",
            )

        assert first is not None
        assert first["queried_account_count"] == 2
        assert first["event_count"] == 2
        assert first["new_event_count"] == 2
        assert first["changed_event_count"] == 0
        assert {event["account_id"] for event in first["events"]} == {
            "acct_work",
            "acct_personal",
        }
        rendered = runtime._render_heartbeat_context_block(  # noqa: SLF001
            {"calendar_digest": first}
        )
        assert rendered is not None
        assert "Calendar Digest" in rendered
        assert "Work (work@example.com)" in rendered
        assert "Personal (me@example.com)" in rendered
        assert "Work event" in rendered
        assert "Personal event" in rendered

        assert second is not None
        assert second["new_event_count"] == 0
        assert second["seen_event_count"] == 2
        assert all(event["heartbeat_seen_before"] for event in second["events"])
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_due_heartbeat_suppresses_repeated_delivered_note(tmp_path) -> None:
    note = "Your delivery queue has 13 deadlettered items."
    runtime = build_runtime(tmp_path, route="opus")
    runtime.orchestrator = FakeHeartbeatNoteOrchestratorClient(note)
    await runtime.start()
    try:
        runtime.scheduler_store.schedule_heartbeat(
            next_fire_at="2000-01-01T00:00:00Z"
        )
        await runtime._run_due_crons()  # noqa: SLF001 - targeted scheduler seam

        first_task = runtime.orchestrator.last_task
        assert first_task is not None
        history = runtime.get_session_history(first_task.session_id)
        assert [item["content"] for item in history] == [note]
        activity_log = history[0]["metadata"]["activity_log"]
        assert any(item["label"] == "Checking heartbeat context." for item in activity_log)
        heartbeat = runtime.scheduler_store.get_heartbeat()
        assert heartbeat["last_result_status"] == "delivered"
        assert heartbeat["last_delivered_summary"] == note

        runtime.scheduler_store.schedule_heartbeat(
            next_fire_at="2000-01-01T00:30:00Z"
        )
        await runtime._run_due_crons()  # noqa: SLF001 - targeted scheduler seam

        second_task = runtime.orchestrator.last_task
        assert second_task is not None
        assert second_task.task_id != first_task.task_id
        assert runtime.get_session_history(first_task.session_id) == history
        heartbeat = runtime.scheduler_store.get_heartbeat()
        assert heartbeat["last_result_status"] == "suppressed"
        assert heartbeat["last_delivered_summary"] == note
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_heartbeat_preference_toggle_controls_due_execution(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="opus")
    runtime.orchestrator = FakeHeartbeatNoopOrchestratorClient()
    await runtime.start()
    try:
        snapshot = runtime.get_desktop_preferences_snapshot()
        assert snapshot["cosmic_heartbeat"]["enabled"] is True

        saved = await runtime.save_desktop_preferences(cosmic_heartbeat_enabled=False)
        assert saved["cosmic_heartbeat"]["enabled"] is False

        runtime.scheduler_store.schedule_heartbeat(
            next_fire_at="2000-01-01T00:00:00Z"
        )
        await runtime._run_due_crons()  # noqa: SLF001 - targeted scheduler seam
        assert runtime.orchestrator.last_task is None

        saved = await runtime.save_desktop_preferences(cosmic_heartbeat_enabled=True)
        assert saved["cosmic_heartbeat"]["enabled"] is True
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_due_cron_reuses_stored_context_and_resolves_explicit_whatsapp_target(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="opus")
    runtime.registry.register(FakeWhatsAppChannelAdapter())
    await runtime.start()
    try:
        runtime.notify_channel_active("whatsapp:+12153079021")
        runtime.request_records["req_sched_create"] = {
            "message": {
                "content": "At 6 AM, check the YC S26 company list against the saved baseline and send the result on WhatsApp.",
            },
            "assembled_conversation_context": [
                {"role": "user", "content": "Keep watching the YC S26 company list."},
                {"role": "assistant", "content": "I saved the baseline and can diff against it in the morning."},
            ],
            "active_working_set": {
                "goal": "Track changes to the YC S26 company list.",
                "active_workstreams": ["Diff the current list against the saved baseline."],
                "open_loops": ["Morning YC list check"],
                "active_task_refs": ["tsk_yc_watch"],
            },
            "memory_context": "## Passive Memory\n- Saved YC S26 baseline with 25 companies on March 16.\n",
        }

        created = await runtime.create_scheduler_cron(
            cron_id="cron_due_context",
            label="Morning YC WhatsApp diff",
            cron_expression="0 6 * * *",
            prompt="Check for new YC companies and report additions or no change.",
            one_shot=True,
            created_by="orchestrator",
            created_request_id="req_sched_create",
            created_session_id="sess_sched_create",
            created_channel="desktop:desk_sched",
            delivery_target="whatsapp",
            context_summary="Diff the saved YC S26 company baseline and explicitly report additions or no change.",
        )
        assert created["delivery_target"] == "whatsapp"
        assert created["delivery_channel"] == "whatsapp:+12153079021"
        assert created["context_summary"] == (
            "Diff the saved YC S26 company baseline and explicitly report additions or no change."
        )

        stored = runtime.scheduler_store.get_cron("cron_due_context")
        assert stored is not None
        metadata = stored["metadata"]
        runtime.scheduler_store.upsert_cron(
            cron_id="cron_due_context",
            name=stored["name"],
            kind=stored["kind"],
            description=stored["description"],
            cron_expr=stored["cron_expr"],
            timezone_name=stored["timezone"],
            next_fire_at="2000-01-01T00:00:00Z",
            metadata=metadata,
        )

        await runtime._run_due_crons()  # noqa: SLF001 - targeted scheduler seam

        task = runtime.orchestrator.last_task
        assert task is not None
        assert task.source == "cron"
        assert task.source_id == "cron_due_context"
        assert task.channel == "whatsapp:+12153079021"
        assert task.input["query"] == "Check for new YC companies and report additions or no change."
        assert task.input["conversation_context"] == []
        memory_context = str(task.input["memory_context"] or "")
        assert "## Stored Reminder Context" in memory_context
        assert "Why this exists: Diff the saved YC S26 company baseline and explicitly report additions or no change." in memory_context
        assert "Original request: At 6 AM, check the YC S26 company list against the saved baseline and send the result on WhatsApp." in memory_context
        assert "### Prior Conversation Snapshot" in memory_context
        assert "- user: Keep watching the YC S26 company list." in memory_context
        assert "- assistant: I saved the baseline and can diff against it in the morning." in memory_context
        assert "Saved YC S26 baseline with 25 companies on March 16." in memory_context

        cron_record = runtime.get_scheduler_cron("cron_due_context")
        assert cron_record is not None
        assert cron_record["last_result_status"] == "completed"
        assert cron_record["next_fire_at"] is None
    finally:
        await runtime.stop()


def test_whatsapp_incoming_emits_route_result_before_async_fulfillment(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="haiku")
    whatsapp_adapter = FakeWhatsAppChannelAdapter()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway_runtime = runtime
        await runtime.start()
        runtime.registry.register(whatsapp_adapter)
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(channel_router)

    with TestClient(app) as client:
        response = client.post(
            "/internal/channels/whatsapp/incoming",
            headers={"X-Internal-Token": "internal-token"},
            json={
                "sender": {"phone": "+12153079021"},
                "text": "hello from whatsapp",
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "accepted"
        assert payload["route"] == "haiku"

        deadline = time.time() + 2.0
        while len(whatsapp_adapter.sent_events) < 3 and time.time() < deadline:
            time.sleep(0.01)

    assert [event["type"] for event in whatsapp_adapter.sent_events[:3]] == [
        "route_result",
        "response.chunk",
        "response.complete",
    ]
    assert whatsapp_adapter.sent_events[0]["request_id"] == payload["request_id"]


def test_telegram_webhook_emits_route_result_before_async_fulfillment(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="haiku")
    telegram_adapter = FakeTelegramChannelAdapter()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway_runtime = runtime
        await runtime.start()
        runtime.registry.register(telegram_adapter)
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(channel_router)

    with TestClient(app) as client:
        response = client.post(
            "/channels/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
            json={
                "update_id": 1,
                "message": {
                    "message_id": 42,
                    "chat": {"id": 12345, "type": "private"},
                    "from": {"id": 12345},
                    "text": "hello from telegram",
                },
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "accepted"

        deadline = time.time() + 2.0
        while len(telegram_adapter.sent_events) < 3 and time.time() < deadline:
            time.sleep(0.01)

        assert [event["type"] for event in telegram_adapter.sent_events[:3]] == [
            "route_result",
            "response.chunk",
            "response.complete",
        ]
        assert telegram_adapter.sent_events[0]["request_id"] == payload["request_id"]


def test_whatsapp_activation_sends_welcome_once_on_runtime_start(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="haiku")
    whatsapp_adapter = FakeWhatsAppChannelAdapter()
    runtime.registry.register(whatsapp_adapter)
    asyncio.run(runtime.start())
    try:
        welcome_events = [event for event in whatsapp_adapter.sent_events if event.get("type") == "channel.welcome"]
        assert len(welcome_events) == 1
        assert welcome_events[0]["channel"] == "whatsapp:+12153079021"
        assert welcome_events[0]["content"] == "COSMIC is connected on WhatsApp. You can message me here anytime."
    finally:
        asyncio.run(runtime.stop())


def test_telegram_activation_sends_welcome_once_on_runtime_start(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="haiku")
    telegram_adapter = FakeTelegramChannelAdapter()
    runtime.registry.register(telegram_adapter)
    asyncio.run(runtime.start())
    try:
        welcome_events = [event for event in telegram_adapter.sent_events if event.get("type") == "channel.welcome"]
        assert len(welcome_events) == 1
        assert welcome_events[0]["channel"] == "telegram:chat_12345"
        assert welcome_events[0]["content"] == "COSMIC is connected on Telegram. You can message me here anytime."
    finally:
        asyncio.run(runtime.stop())


@pytest.mark.asyncio
async def test_whatsapp_final_response_is_queued_and_retried_when_channel_send_fails(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="haiku")
    flaky_adapter = FlakyWhatsAppChannelAdapter()
    runtime.registry.register(flaky_adapter)
    await runtime.start()
    try:
        processed = await runtime.process_incoming_user_message(
            {
                "content": "hello from whatsapp",
                "channel": "whatsapp:+12153079021",
                "metadata": {"platform": "whatsapp"},
            }
        )
        runtime.start_request_fulfillment(processed)

        deadline = time.time() + 2.0
        while time.time() < deadline:
            if any(event.get("type") == "response.chunk" for event in flaky_adapter.sent_events):
                break
            await asyncio.sleep(0.01)

        assert any(event.get("type") == "response.chunk" for event in flaky_adapter.sent_events)
        assert not any(event.get("type") == "response.complete" for event in flaky_adapter.sent_events)
        assert runtime.delivery_queue_store.summary()["pending_count"] == 1

        flaky_adapter.fail_response_complete = False
        runtime.notify_channel_active("whatsapp:+12153079021")

        deadline = time.time() + 3.0
        while time.time() < deadline:
            if any(event.get("type") == "response.complete" for event in flaky_adapter.sent_events):
                break
            await asyncio.sleep(0.05)

        assert any(event.get("type") == "response.complete" for event in flaky_adapter.sent_events)
        assert runtime.delivery_queue_store.summary()["pending_count"] == 0
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_offline_desktop_task_input_is_held_until_channel_returns(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="haiku")
    desktop_adapter = FlakyDesktopChannelAdapter()
    runtime.registry.register(desktop_adapter)
    await runtime.start()
    try:
        event = {
            "type": "task.input_required",
            "request_id": "req_input_hold",
            "task_id": "tsk_input_hold",
            "session_id": "sess_20260310",
            "channel": "desktop:desk_hold",
            "question": "Pick one option.",
            "options": ["A", "B"],
        }

        await runtime.deliver_channel_event(event)
        assert runtime.delivery_queue_store.summary()["pending_count"] == 1
        assert desktop_adapter.sent_events == []

        desktop_adapter.available = True
        runtime.notify_channel_active("desktop:desk_hold")

        deadline = time.time() + 3.0
        while time.time() < deadline:
            if desktop_adapter.sent_events:
                break
            await asyncio.sleep(0.05)

        assert desktop_adapter.sent_events[0]["type"] == "task.input_required"
        assert runtime.delivery_queue_store.summary()["pending_count"] == 0
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_task_input_stream_message_queues_and_acks_when_channel_unavailable(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="haiku")
    runtime._redis = FakeRedis()
    desktop_adapter = FlakyDesktopChannelAdapter()
    runtime.registry.register(desktop_adapter)
    await runtime.start()
    try:
        payload = {
            "input_request_id": "uir_stream_hold",
            "task_id": "tsk_stream_hold",
            "session_id": "sess_20260312",
            "channel": "desktop:desk_hold",
            "agent": "cosmic/orchestrator:1.0.0",
            "question": "Choose a deployment target.",
            "options": ["staging", "production"],
            "status": "pending",
            "timestamp": utcnow_iso(),
        }

        await runtime._handle_task_input_stream_message(  # noqa: SLF001 - targeted Redis seam
            "1-0",
            {"payload": json.dumps(payload)},
        )

        group_state = runtime._redis.groups[
            (runtime.config.task_input_requests_stream, runtime.config.task_input_gateway_group)
        ]
        assert "1-0" in group_state["acked"]
        assert runtime.delivery_queue_store.summary()["pending_count"] == 1
        stored = runtime.session_store.get_task_input_request("uir_stream_hold")
        assert stored is not None
        assert stored["status"] == "pending"

        desktop_adapter.available = True
        runtime.notify_channel_active("desktop:desk_hold")

        deadline = time.time() + 3.0
        while time.time() < deadline:
            if desktop_adapter.sent_events:
                break
            await asyncio.sleep(0.05)

        assert desktop_adapter.sent_events[0]["type"] == "task.input_required"
        assert runtime.delivery_queue_store.summary()["pending_count"] == 0
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_task_input_stream_message_queues_and_acks_on_retryable_delivery_error(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="haiku")
    runtime._redis = FakeRedis()
    desktop_adapter = RetryableDesktopChannelAdapter()
    runtime.registry.register(desktop_adapter)
    await runtime.start()
    try:
        payload = {
            "input_request_id": "uir_stream_retry",
            "task_id": "tsk_stream_retry",
            "session_id": "sess_20260312",
            "channel": "desktop:desk_retry",
            "agent": "cosmic/orchestrator:1.0.0",
            "question": "Choose a deployment target.",
            "options": ["staging", "production"],
            "status": "pending",
            "timestamp": utcnow_iso(),
        }

        await runtime._handle_task_input_stream_message(  # noqa: SLF001 - targeted Redis seam
            "1-0",
            {"payload": json.dumps(payload)},
        )

        group_state = runtime._redis.groups[
            (runtime.config.task_input_requests_stream, runtime.config.task_input_gateway_group)
        ]
        assert "1-0" in group_state["acked"]

        deadline = time.time() + 3.0
        while time.time() < deadline:
            if desktop_adapter.sent_events:
                break
            await asyncio.sleep(0.05)

        assert desktop_adapter.sent_events[0]["type"] == "task.input_required"
        assert runtime.delivery_queue_store.summary()["pending_count"] == 0
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_whatsapp_message_auto_submits_pending_task_input_reply(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="perplexity")
    runtime._redis = FakeRedis()
    await runtime.start()
    try:
        session_id = runtime._current_session_id()
        runtime.session_store.upsert_task_input_request(
            input_request_id="uir_whatsapp_1",
            task_id="tsk_waiting_whatsapp",
            session_id=session_id,
            channel="whatsapp:+12153079021",
            question="Which repository should I use?",
            options=["Cosmic-OS", "cosmic-memory"],
            agent="cosmic/orchestrator:1.0.0",
            metadata={},
            status="pending",
            created_at=utcnow_iso(),
        )

        result = await runtime.process_incoming_user_message(
            {
                "content": "Use Cosmic-OS.",
                "channel": "whatsapp:+12153079021",
                "metadata": {"platform": "whatsapp", "message_id": "wamid_reply_1"},
            }
        )

        assert result["dispatch_target"] == "redis"
        assert result["route"] == "task_input_reply"
        reply_stream = runtime._redis.streams[runtime.config.task_input_replies_stream]
        reply_payload = json.loads(reply_stream[0][1]["payload"])
        assert reply_payload["input_request_id"] == "uir_whatsapp_1"
        assert reply_payload["task_id"] == "tsk_waiting_whatsapp"
        assert reply_payload["content"] == "Use Cosmic-OS."

        stored = runtime.session_store.get_task_input_request("uir_whatsapp_1")
        assert stored is not None
        assert stored["status"] == "answered"
        assert stored["reply_content"] == "Use Cosmic-OS."
    finally:
        await runtime.stop()


def test_desktop_websocket_accepts_task_input_reply_messages(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="haiku")
    runtime._redis = FakeRedis()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway_runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(channel_router)

    with TestClient(app) as client:
        session_id = runtime._current_session_id()
        runtime.session_store.upsert_task_input_request(
            input_request_id="uir_desktop_1",
            task_id="tsk_waiting_desktop",
            session_id=session_id,
            channel="desktop:desk_input",
            question="Which environment should I target?",
            options=["staging", "production"],
            agent="cosmic/orchestrator:1.0.0",
            metadata={},
            status="pending",
            created_at=utcnow_iso(),
        )

        with client.websocket_connect("/ws?token=test-token&device_id=desk_input") as websocket:
            websocket.send_json(
                {
                    "type": "task.input_reply",
                    "request_id": "reply_req_1",
                    "input_request_id": "uir_desktop_1",
                    "task_id": "tsk_waiting_desktop",
                    "content": "Use staging.",
                }
            )
            accepted = websocket.receive_json()

        assert accepted["type"] == "task.input_reply.accepted"
        assert accepted["request_id"] == "reply_req_1"
        assert accepted["input_request_id"] == "uir_desktop_1"
        assert accepted["task_id"] == "tsk_waiting_desktop"

        reply_stream = runtime._redis.streams[runtime.config.task_input_replies_stream]
        reply_payload = json.loads(reply_stream[0][1]["payload"])
        assert reply_payload["channel"] == "desktop:desk_input"
        assert reply_payload["content"] == "Use staging."

        stored = runtime.session_store.get_task_input_request("uir_desktop_1")
        assert stored is not None
        assert stored["status"] == "answered"
        assert stored["reply_content"] == "Use staging."


def test_internal_telegram_media_route_uses_internal_token(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    telegram_adapter = FakeTelegramChannelAdapter()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway_runtime = runtime
        await runtime.start()
        runtime.registry.register(telegram_adapter)
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(channel_router)

    with TestClient(app) as client:
        response = client.get(
            "/internal/channels/telegram/media/file_123",
            headers={"X-Internal-Token": "internal-token"},
        )
        assert response.status_code == 200
        assert response.content == b"telegram-media"
        assert response.headers["content-type"].startswith("image/jpeg")


def test_signed_artifact_content_route_serves_staged_image(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    runtime.config.artifacts_root = tmp_path / "runs" / "artifacts"
    runtime.config.public_base_url = "https://gateway.example.test"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway_runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(channel_router)

    with TestClient(app) as client:
        upload_response = client.post(
            "/channels/desktop/uploads",
            headers={"Authorization": "Bearer test-token"},
            data={
                "request_id": "req_signed_image_1",
                "session_id": "sess_signed_image_1",
                "device_id": "desk_signed_image_1",
            },
            files=[
                (
                    "files",
                    (
                        "photo.png",
                        b"\x89PNG\r\n\x1a\nsigned",
                        "image/png",
                    ),
                )
            ],
        )
        assert upload_response.status_code == 200
        attachment = upload_response.json()["attachments"][0]
        runtime.artifact_store.persist_inbound_attachments(
            request_id="req_signed_image_1",
            session_id="sess_signed_image_1",
            source_channel="desktop:desk_signed_image_1",
            source_platform="desktop",
            source_message_id=None,
            attachments=[attachment],
        )
        signed_url = runtime.mint_artifact_access_url(attachment)
        assert signed_url is not None
        relative_url = signed_url.replace("https://gateway.example.test", "", 1)

        response = client.get(relative_url)

    assert response.status_code == 200
    assert response.content == b"\x89PNG\r\n\x1a\nsigned"
    assert response.headers["content-type"].startswith("image/png")


def test_desktop_upload_route_rejects_more_than_twenty_images(tmp_path) -> None:
    runtime = build_runtime(tmp_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway_runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(channel_router)

    files = [
        (
            "files",
            (f"image_{index}.png", b"fake-image", "image/png"),
        )
        for index in range(21)
    ]

    with TestClient(app) as client:
        response = client.post(
            "/channels/desktop/uploads",
            headers={"Authorization": "Bearer test-token"},
            data={
                "request_id": "req_image_cap_1",
                "session_id": "sess_image_cap_1",
                "device_id": "desk_image_cap_1",
            },
            files=files,
        )

    assert response.status_code == 400
    assert "Up to 20 images can be attached in one message" in response.json()["detail"]


def test_signed_artifact_content_route_resizes_large_image_for_llm_fetch(tmp_path) -> None:
    pytest.importorskip("PIL")
    from PIL import Image

    runtime = build_runtime(tmp_path)
    runtime.config.artifacts_root = tmp_path / "runs" / "artifacts"
    runtime.config.public_base_url = "https://gateway.example.test"

    image = Image.new("RGB", (4000, 3000), color=(25, 50, 75))
    image_buffer = io.BytesIO()
    image.save(image_buffer, format="PNG")
    source_bytes = image_buffer.getvalue()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway_runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(channel_router)

    with TestClient(app) as client:
        upload_response = client.post(
            "/channels/desktop/uploads",
            headers={"Authorization": "Bearer test-token"},
            data={
                "request_id": "req_signed_image_2",
                "session_id": "sess_signed_image_2",
                "device_id": "desk_signed_image_2",
            },
            files=[
                (
                    "files",
                    (
                        "hero.png",
                        source_bytes,
                        "image/png",
                    ),
                )
            ],
        )
        assert upload_response.status_code == 200
        attachment = upload_response.json()["attachments"][0]
        runtime.artifact_store.persist_inbound_attachments(
            request_id="req_signed_image_2",
            session_id="sess_signed_image_2",
            source_channel="desktop:desk_signed_image_2",
            source_platform="desktop",
            source_message_id=None,
            attachments=[attachment],
        )
        signed_url = runtime.mint_artifact_access_url(attachment)
        assert signed_url is not None
        relative_url = signed_url.replace("https://gateway.example.test", "", 1)

        response = client.get(relative_url)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")
    optimized = Image.open(io.BytesIO(response.content))
    assert max(optimized.size) <= runtime.config.llm_image_max_edge_px
    assert optimized.size[0] * optimized.size[1] <= runtime.config.llm_image_max_pixels


def test_runtime_builds_client_response_blocks_with_image_preview() -> None:
    local_temp_root = Path(__file__).resolve().parents[1] / ".codex_manual_tmp"
    local_temp_root.mkdir(parents=True, exist_ok=True)
    tmpdir = local_temp_root / f"gw-response-blocks-{int(time.time() * 1000)}"
    try:
        tmpdir.mkdir(parents=True, exist_ok=True)
        runtime = build_runtime(tmpdir)
        runtime.config.artifacts_root = tmpdir / "runs" / "artifacts"
        runtime.config.public_base_url = "https://gateway.example.test"

        artifact_dir = runtime.config.artifacts_root / "unit"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = artifact_dir / "chart.png"
        artifact_path.write_bytes(b"\x89PNG\r\n\x1a\nchart-preview")

        produced_artifacts = runtime._normalize_produced_artifact_list(
            [
                {
                    "artifact_id": "art_chart",
                    "path": "runs/artifacts/unit/chart.png",
                    "filename": "chart.png",
                    "mime_type": "image/png",
                    "downloadable": True,
                }
            ]
        )

        blocks = runtime._build_client_response_blocks(
            content="Chart ready.\n\n```python\nprint('ok')\n```",
            produced_artifacts=produced_artifacts,
        )

        assert [block["type"] for block in blocks] == ["markdown", "code", "image_artifact"]
        assert blocks[0]["text"] == "Chart ready.\n\n"
        assert blocks[1]["language"] == "python"
        assert blocks[2]["artifact_id"] == "art_chart"
        assert blocks[2]["preview_url"].startswith("https://gateway.example.test/artifacts/content/art_chart?")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.mark.asyncio
async def test_response_complete_caches_output_artifacts_for_signed_preview(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    runtime.config.artifacts_root = tmp_path / "runs" / "artifacts"
    runtime.config.public_base_url = "https://gateway.example.test"
    artifact_dir = runtime.config.artifacts_root / "unit"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / "chart.png"
    artifact_path.write_bytes(b"\x89PNG\r\n\x1a\ncached-output")

    await runtime.start()
    try:
        sent_events: list[dict[str, Any]] = []

        async def send(event: dict[str, Any]) -> None:
            sent_events.append(event)

        def store_assistant_message(
            content: str,
            *,
            awaiting_reply: bool,
            metadata: dict[str, Any] | None,
            channel: str | None,
            route: str | None,
        ) -> str:
            return runtime.session_store.append_message(
                "sess_output_preview_1",
                role="assistant",
                content=content,
                route=route,
                awaiting_reply=awaiting_reply,
                channel=channel,
                metadata=metadata,
            )

        await runtime._handle_orchestrator_event(
            {
                "type": "response.complete",
                "request_id": "req_output_preview_1",
                "task_id": "tsk_output_preview_1",
                "session_id": "sess_output_preview_1",
                "channel": "desktop:desk_output_preview_1",
                "content": "Here is the chart.",
                "produced_artifacts": [
                    {
                        "artifact_id": "art_output_chart",
                        "path": "runs/artifacts/unit/chart.png",
                        "filename": "chart.png",
                        "mime_type": "image/png",
                    }
                ],
            },
            send=send,
            store_assistant_message=store_assistant_message,
        )

        cached = runtime.artifact_store.get("art_output_chart")
        assert cached is not None
        assert cached["path"] == "runs/artifacts/unit/chart.png"
        assert cached["filename"] == "chart.png"
        assert cached["mime"] == "image/png"
        assert any(event.get("type") == "response.complete" for event in sent_events)
    finally:
        await runtime.stop()


def test_signed_artifact_content_route_serves_stored_output_artifact_preview_without_prior_cache(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    runtime.config.artifacts_root = tmp_path / "runs" / "artifacts"
    runtime.config.public_base_url = "https://gateway.example.test"
    runtime.session_store.initialize()
    runtime.artifact_store.initialize()
    artifact_dir = runtime.config.artifacts_root / "unit"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / "chart.png"
    artifact_path.write_bytes(b"\x89PNG\r\n\x1a\nstored-output")

    produced_artifacts = runtime._normalize_produced_artifact_list(
        [
            {
                "artifact_id": "art_stored_chart",
                "path": "runs/artifacts/unit/chart.png",
                "filename": "chart.png",
                "mime_type": "image/png",
            }
        ]
    )
    message_id = runtime.session_store.append_message(
        "sess_output_preview_2",
        role="assistant",
        content="Here it is.",
        route="opus",
        awaiting_reply=False,
        channel="desktop:desk_output_preview_2",
        metadata={
            "request_id": "req_output_preview_2",
            "produced_artifacts": produced_artifacts,
        },
    )
    assert message_id.startswith("msg_")
    assert runtime.artifact_store.get("art_stored_chart") is None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway_runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(channel_router)

    signed_url = runtime.mint_artifact_access_url(produced_artifacts[0], purpose="ui_preview")
    assert signed_url is not None
    relative_url = signed_url.replace("https://gateway.example.test", "", 1)

    with TestClient(app) as client:
        response = client.get(relative_url)

    assert response.status_code == 200
    assert response.content == b"\x89PNG\r\n\x1a\nstored-output"
    cached = runtime.artifact_store.get("art_stored_chart")
    assert cached is not None
    assert cached["source_message_id"] == message_id


def test_desktop_websocket_streams_thin_opus_route(test_client: TestClient, tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="opus")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway_runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(channel_router)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?token=test-token&device_id=desk_opus") as websocket:
            websocket.send_json(
                {
                    "type": "query",
                    "request_id": "req_opus",
                    "content": "Plan this for me",
                }
            )
            route_result = websocket.receive_json()
            assert route_result["type"] == "route_result"
            assert route_result["route"] == "opus"

            created = websocket.receive_json()
            assert created["type"] == "task.created"
            assert created["route"] == "opus"

            thinking = websocket.receive_json()
            assert thinking["type"] == "response.thinking.chunk"
            assert thinking["content"] == "Let me think this through."

            chunk = websocket.receive_json()
            assert chunk["type"] == "response.chunk"
            assert chunk["content"] == "Thin Opus answer"

            complete = websocket.receive_json()
            assert complete["type"] == "response.complete"
            assert complete["route"] == "opus"
            assert complete["awaiting_reply"] is True

            completed = websocket.receive_json()
            assert completed["type"] == "task.completed"


def test_desktop_websocket_hands_off_direct_route_to_opus_with_escalation_activity(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="perplexity")
    runtime.perplexity_adapter = FakeHandoffDirectAdapter("perplexity")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway_runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(channel_router)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?token=test-token&device_id=desk_handoff") as websocket:
            websocket.send_json(
                {
                    "type": "query",
                    "request_id": "req_handoff",
                    "content": "Make a plan and then do it.",
                }
            )
            route_result = websocket.receive_json()
            assert route_result["type"] == "route_result"
            assert route_result["route"] == "perplexity"

            progress = websocket.receive_json()
            assert progress["type"] == "task.progress"
            assert progress["request_id"] == "req_handoff"
            assert progress["route"] == "opus"
            assert progress["status"] == "escalating"
            assert progress["message"] == "Escalating to Opus for deeper handling."
            assert progress["escalated_from"] == "perplexity"

            created = websocket.receive_json()
            assert created["type"] == "task.created"
            assert created["route"] == "opus"

            thinking = websocket.receive_json()
            assert thinking["type"] == "response.thinking.chunk"
            assert thinking["content"] == "Let me think this through."

            chunk = websocket.receive_json()
            assert chunk["type"] == "response.chunk"
            assert chunk["content"] == "Thin Opus answer"

            complete = websocket.receive_json()
            assert complete["type"] == "response.complete"
            assert complete["route"] == "opus"
            assert complete["content"] == "Thin Opus answer"

            completed = websocket.receive_json()
            assert completed["type"] == "task.completed"

        history = runtime.session_store.get_history(route_result["session_id"])
        assert history[-1]["role"] == "assistant"
        assert history[-1]["route"] == "opus"
        assert history[-1]["content"] == "Thin Opus answer"

        response = client.get(
            "/routing-audit?limit=5",
            headers={"Authorization": "Bearer test-token"},
        )
        assert response.status_code == 200
        entries = response.json()["entries"]
        assert len(entries) == 2
        assert entries[0]["request_id"] == "req_handoff"
        assert entries[0]["decision_source"] == "direct_model_handoff"
        assert entries[0]["classifier_route"] == "perplexity"
        assert entries[0]["final_route"] == "opus"
        assert entries[0]["dispatch_target"] == "orchestrator"
        assert "direct_model_handoff:perplexity->opus" in entries[0]["signals"]
        assert entries[1]["decision_source"] == "model_router"
        assert entries[1]["final_route"] == "perplexity"


def test_opus_research_provenance_persists_into_history_turn_ledger_and_working_set(tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="opus")
    runtime.orchestrator = FakeResearchOrchestratorClient()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway_runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(channel_router)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?token=test-token&device_id=desk_research") as websocket:
            websocket.send_json(
                {
                    "type": "query",
                    "request_id": "req_research",
                    "content": "How was Cursor Composer 2 made?",
                }
            )
            route_result = websocket.receive_json()
            assert route_result["type"] == "route_result"
            assert route_result["route"] == "opus"
            session_id = route_result["session_id"]

            created = websocket.receive_json()
            assert created["type"] == "task.created"

            complete = websocket.receive_json()
            assert complete["type"] == "response.complete"
            assert complete["research_provenance"]["paths"] == ["native_web_search"]

            completed = websocket.receive_json()
            assert completed["type"] == "task.completed"

        deadline = time.time() + 2.0
        turn_entry = None
        while time.time() < deadline:
            turn_entry = runtime.session_store.get_turn_ledger_entry("req_research")
            if turn_entry is not None:
                break
            time.sleep(0.01)

        assert turn_entry is not None
        history = runtime.session_store.get_history(session_id)
        assistant_message = history[-1]
        assert assistant_message["role"] == "assistant"
        assert assistant_message["metadata"]["research_provenance"] == {
            "paths": ["native_web_search"],
            "source_count": 2,
            "source_domains": ["cursor.com", "techcrunch.com"],
            "source_sample": [
                {
                    "url": "https://cursor.com/blog/composer-2",
                    "title": "Cursor Composer 2",
                    "domain": "cursor.com",
                },
                {
                    "url": "https://techcrunch.com/cursor-composer-2",
                    "title": "TechCrunch coverage",
                    "domain": "techcrunch.com",
                },
            ],
        }
        assert assistant_message["metadata"]["sources"] == [
            {
                "url": "https://cursor.com/blog/composer-2",
                "title": "Cursor Composer 2",
                "domain": "cursor.com",
            },
            {
                "url": "https://techcrunch.com/cursor-composer-2",
                "title": "TechCrunch coverage",
                "domain": "techcrunch.com",
            },
        ]
        assert assistant_message["metadata"]["specialist_receipts"] == [
            {
                "tool_name": "x_search",
                "intent": "x.search",
                "agent_id": "cosmic/x-twitter-search-agent:1.0.0",
                "agent_label": "x twitter search agent",
                "activity": "delegated x.search to x twitter search agent and found recent source-backed X results",
                "source_count": 2,
                "source_domains": ["x.com"],
                "source_sample": [
                    {
                        "url": "https://x.com/mntruell/status/123",
                        "title": "@mntruell on X",
                        "domain": "x.com",
                    }
                ],
            }
        ]
        assert turn_entry["tool_summary"] == ["opus", "native_web_search"]
        assert turn_entry["metadata"]["research_provenance"]["source_domains"] == ["cursor.com", "techcrunch.com"]
        assert turn_entry["metadata"]["specialist_receipts"][0]["intent"] == "x.search"

        active_working_set = runtime._refresh_active_working_set(session_id)  # noqa: SLF001 - targeted unit seam
        assert active_working_set["recent_research_receipts"] == [
            {
                "request_id": "req_research",
                "route": "opus",
                "question": "How was Cursor Composer 2 made?",
                "completed_at": turn_entry["completed_at"],
                "paths": ["native_web_search"],
                "source_count": 2,
                "source_domains": ["cursor.com", "techcrunch.com"],
                "source_sample": [
                    {
                        "url": "https://cursor.com/blog/composer-2",
                        "title": "Cursor Composer 2",
                        "domain": "cursor.com",
                    },
                    {
                        "url": "https://techcrunch.com/cursor-composer-2",
                        "title": "TechCrunch coverage",
                        "domain": "techcrunch.com",
                    },
                ],
            }
        ]
        assert active_working_set["recent_specialist_receipts"] == [
            {
                "tool_name": "x_search",
                "intent": "x.search",
                "agent_id": "cosmic/x-twitter-search-agent:1.0.0",
                "agent_label": "x twitter search agent",
                "activity": "delegated x.search to x twitter search agent and found recent source-backed X results",
                "source_count": 2,
                "source_domains": ["x.com"],
                "source_sample": [
                    {
                        "url": "https://x.com/mntruell/status/123",
                        "title": "@mntruell on X",
                        "domain": "x.com",
                    }
                ],
                "request_id": "req_research",
                "route": "opus",
                "question": "How was Cursor Composer 2 made?",
                "completed_at": turn_entry["completed_at"],
            }
        ]
        rendered = runtime._render_active_working_set_context(active_working_set)  # noqa: SLF001 - targeted unit seam
        assert rendered is not None
        assert "Recent research receipts" in rendered
        assert "research=native web_search" in rendered
        assert "domains=cursor.com, techcrunch.com" in rendered


def test_desktop_websocket_route_override_bypasses_classifier(test_client: TestClient, tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="opus")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway_runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(channel_router)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?token=test-token&device_id=desk_override") as websocket:
            websocket.send_json(
                {
                    "type": "query",
                    "request_id": "req_override",
                    "content": "Force Perplexity",
                    "route_override": "perplexity",
                }
            )
            route_result = websocket.receive_json()
            assert route_result["type"] == "route_result"
            assert route_result["route"] == "perplexity"
            assert route_result["classification"]["signals"] == ["manual_route_override"]

            chunk = websocket.receive_json()
            assert chunk["type"] == "response.chunk"

            complete = websocket.receive_json()
            assert complete["type"] == "response.complete"
            assert complete["route"] == "perplexity"

        response = client.get(
            "/routing-audit?limit=5",
            headers={"Authorization": "Bearer test-token"},
        )
        assert response.status_code == 200
        entry = response.json()["entries"][0]
        assert entry["request_id"] == "req_override"
        assert entry["decision_source"] == "manual_override"
        assert entry["route_override"] == "perplexity"
        assert entry["final_route"] == "perplexity"
        assert entry["sticky_hit"] is False


def test_desktop_websocket_cancel_stops_direct_stream(test_client: TestClient, tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="haiku")
    runtime.haiku_adapter = FakeCancellableDirectAdapter("haiku")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway_runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(channel_router)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?token=test-token&device_id=desk_cancel_direct") as websocket:
            websocket.send_json(
                {
                    "type": "query",
                    "request_id": "req_cancel_direct",
                    "content": "Start a long answer",
                }
            )
            route_result = websocket.receive_json()
            assert route_result["type"] == "route_result"
            chunk = websocket.receive_json()
            assert chunk["type"] == "response.chunk"
            assert chunk["content"] == "Partial answer"

            websocket.send_json(
                {
                    "type": "cancel",
                    "request_id": "cancel_direct_001",
                    "target_request_id": "req_cancel_direct",
                }
            )
            cancelled = websocket.receive_json()
            assert cancelled["type"] == "task.cancelled"
            assert cancelled["request_id"] == "req_cancel_direct"
            assert cancelled["task_id"] is None
            assert cancelled["route"] == "haiku"


def test_desktop_websocket_cancel_stops_opus_stream(test_client: TestClient, tmp_path) -> None:
    runtime = build_runtime(tmp_path, route="opus")
    runtime.orchestrator = FakeCancellableOrchestratorClient()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway_runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(channel_router)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?token=test-token&device_id=desk_cancel_opus") as websocket:
            websocket.send_json(
                {
                    "type": "query",
                    "request_id": "req_cancel_opus",
                    "content": "Start an opus task",
                }
            )
            route_result = websocket.receive_json()
            assert route_result["type"] == "route_result"
            created = websocket.receive_json()
            assert created["type"] == "task.created"

            websocket.send_json(
                {
                    "type": "cancel",
                    "request_id": "cancel_opus_001",
                    "task_id": created["task_id"],
                    "target_request_id": "req_cancel_opus",
                }
            )
            cancelled = websocket.receive_json()
            assert cancelled["type"] == "task.cancelled"
            assert cancelled["task_id"] == created["task_id"]
            assert cancelled["request_id"] == "req_cancel_opus"
            assert cancelled["route"] == "opus"
