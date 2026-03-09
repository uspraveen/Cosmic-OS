from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .base import ChannelAdapter, MessageCallback, NormalizedMessage


WHATSAPP_DM_SUFFIX = "@s.whatsapp.net"
WHATSAPP_GROUP_SUFFIX = "@g.us"
DEFAULT_TEXT_CHUNK_LIMIT = 4000
SUPPORTED_CHUNK_MODES = {"newline", "length"}
SUPPORTED_ATTACHMENT_TYPES = {
    "audio",
    "button_reply",
    "contact",
    "document",
    "image",
    "list_reply",
    "location",
    "poll_reply",
    "reaction",
    "sticker",
    "text",
    "unknown",
    "video",
    "voice",
}


@dataclass(slots=True)
class WhatsAppConfig:
    """Runtime config for the Gateway-side WhatsApp adapter."""

    bridge_base_url: str
    bridge_token: str = ""
    gateway_internal_token: str = ""
    text_chunk_limit: int = DEFAULT_TEXT_CHUNK_LIMIT
    chunk_mode: str = "newline"
    send_delay_ms: int = 200
    ack_delay_sec: float = 0.0
    progress_min_interval_sec: float = 8.0
    health_path: str = "/health"
    status_path: str = "/status"
    send_path: str = "/send"
    pairing_qr_path: str = "/pairing/qr"
    session_path: str = "/session"
    config_path: str = "/config"

    @classmethod
    def from_env(cls) -> "WhatsAppConfig":
        chunk_mode = os.getenv("WHATSAPP_CHUNK_MODE", "newline").strip().lower()
        if chunk_mode not in SUPPORTED_CHUNK_MODES:
            chunk_mode = "newline"

        def env_int(name: str, default: int) -> int:
            raw = os.getenv(name)
            if raw is None:
                return default
            try:
                return int(raw)
            except ValueError:
                return default

        def env_float(name: str, default: float) -> float:
            raw = os.getenv(name)
            if raw is None:
                return default
            try:
                return float(raw)
            except ValueError:
                return default

        bridge_base_url = (
            os.getenv("WHATSAPP_BRIDGE_URL")
            or os.getenv("WHATSAPP_BRIDGE_BASE_URL")
            or "http://127.0.0.1:3000"
        )

        return cls(
            bridge_base_url=bridge_base_url.rstrip("/"),
            bridge_token=os.getenv("WHATSAPP_BRIDGE_TOKEN", ""),
            gateway_internal_token=os.getenv("GATEWAY_INTERNAL_TOKEN", ""),
            text_chunk_limit=max(500, env_int("WHATSAPP_TEXT_CHUNK_LIMIT", DEFAULT_TEXT_CHUNK_LIMIT)),
            chunk_mode=chunk_mode,
            send_delay_ms=max(0, env_int("WHATSAPP_SEND_DELAY_MS", 200)),
            ack_delay_sec=max(0.0, env_float("WHATSAPP_ACK_DELAY_SEC", 0.0)),
            progress_min_interval_sec=max(
                0.0,
                env_float("WHATSAPP_PROGRESS_MIN_INTERVAL_SEC", 8.0),
            ),
        )


class WhatsAppAdapter(ChannelAdapter):
    """Gateway-facing WhatsApp adapter backed by the Baileys bridge.

    Expected bridge -> Gateway payload shape:

    {
        "schema_version": 1,
        "event": "message.inbound",
        "sender": {"jid": "...", "phone": "+1555...", "push_name": "Alice"},
        "chat": {"jid": "...", "type": "dm|group"},
        "message": {
            "id": "...",
            "type": "text|image|video|audio|voice|document|...",
            "text": "...",
            "caption": "...",
            "timestamp_unix_ms": 1710000000000,
            "quoted_message_id": "...",
            "mentions": ["+1555..."],
            "attachments": [
                {
                    "id": "att_1",
                    "kind": "image",
                    "mime_type": "image/jpeg",
                    "filename": "photo.jpg",
                    "size_bytes": 12345,
                    "width": 1024,
                    "height": 768,
                    "duration_ms": null,
                    "sha256": "...",
                    "bridge_media_ref": "msg_123:att_1",
                    "download_url": "http://127.0.0.1:3000/media/msg_123/att_1"
                }
            ]
        }
    }

    The adapter also accepts the current prototype payload
    { sender, text, channel, platform } for backward compatibility.
    """

    platform = "whatsapp"

    def __init__(
        self,
        config: WhatsAppConfig,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._http = http_client
        self._owns_http_client = http_client is None
        self._callback: MessageCallback | None = None
        self._stream_buffers: dict[str, list[str]] = {}
        self._pending_ack_tasks: dict[str, asyncio.Task[None]] = {}
        self._sent_ack_request_ids: set[str] = set()
        self._completed_response_request_ids: set[str] = set()
        self._channel_locks: dict[str, asyncio.Lock] = {}
        self._last_progress_sent: dict[str, float] = {}

    async def start(self) -> None:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self.config.bridge_base_url,
                timeout=httpx.Timeout(15.0, connect=5.0),
            )

        response = await self._http.get(self.config.health_path)
        response.raise_for_status()

    async def ensure_ready(self) -> None:
        if self._http is None:
            await self.start()

    async def stop(self) -> None:
        self._stream_buffers.clear()
        for task in self._pending_ack_tasks.values():
            task.cancel()
        self._pending_ack_tasks.clear()
        self._sent_ack_request_ids.clear()
        self._completed_response_request_ids.clear()
        self._last_progress_sent.clear()

        if self._owns_http_client and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def on_message(self, callback: MessageCallback) -> None:
        self._callback = callback

    async def get_status(self) -> dict[str, Any]:
        await self.ensure_ready()
        if self._http is None:
            raise RuntimeError("WhatsApp bridge client is not initialized")

        return await self._request_bridge_json(
            "GET",
            self.config.status_path,
            headers=self._bridge_headers(),
        )

    async def request_pairing_qr(
        self,
        *,
        refresh: bool = True,
        wait_timeout_ms: int = 15000,
    ) -> dict[str, Any]:
        await self.ensure_ready()
        if self._http is None:
            raise RuntimeError("WhatsApp bridge client is not initialized")

        timeout_sec = max(15.0, (wait_timeout_ms / 1000.0) + 5.0)
        return await self._request_bridge_json(
            "POST",
            self.config.pairing_qr_path,
            json={
                "refresh": refresh,
                "wait_timeout_ms": wait_timeout_ms,
            },
            headers=self._bridge_headers(),
            timeout=httpx.Timeout(timeout_sec, connect=5.0),
        )

    async def clear_session(self) -> dict[str, Any]:
        await self.ensure_ready()
        if self._http is None:
            raise RuntimeError("WhatsApp bridge client is not initialized")

        return await self._request_bridge_json(
            "DELETE",
            self.config.session_path,
            headers=self._bridge_headers(),
        )

    async def get_config(self) -> dict[str, Any]:
        await self.ensure_ready()
        if self._http is None:
            raise RuntimeError("WhatsApp bridge client is not initialized")

        payload = await self._request_bridge_json(
            "GET",
            self.config.config_path,
            headers=self._bridge_headers(),
        )
        config_payload = payload.get("config")
        if isinstance(config_payload, dict):
            return config_payload
        return payload

    async def update_config(
        self,
        *,
        allowed_phone: str | None = None,
        self_chat_only: bool | None = None,
    ) -> dict[str, Any]:
        await self.ensure_ready()
        if self._http is None:
            raise RuntimeError("WhatsApp bridge client is not initialized")

        body: dict[str, Any] = {}
        if allowed_phone is not None:
            body["allowed_phone"] = allowed_phone
        if self_chat_only is not None:
            body["self_chat_only"] = self_chat_only

        payload = await self._request_bridge_json(
            "POST",
            self.config.config_path,
            json=body,
            headers=self._bridge_headers(),
        )
        config_payload = payload.get("config")
        if isinstance(config_payload, dict):
            return config_payload
        return payload

    async def handle_incoming(self, payload: dict[str, Any]) -> NormalizedMessage:
        normalized = self.normalize_message(payload)
        if self._callback is None:
            raise RuntimeError("WhatsAppAdapter.on_message() must be registered before handling inbound traffic")

        processed = await self._callback(normalized)
        if isinstance(processed, dict):
            return processed
        return normalized

    def normalize_message(self, raw_message: Any) -> NormalizedMessage:
        if not isinstance(raw_message, dict):
            raise TypeError("WhatsApp inbound payload must be a dict")

        message = raw_message.get("message")
        sender = raw_message.get("sender")
        chat = raw_message.get("chat")

        if isinstance(message, dict):
            message_type = self._normalize_message_type(message.get("type"))
            attachments = self._normalize_attachments(message.get("attachments"))
            text = self._first_non_empty(
                message.get("text"),
                message.get("caption"),
            )
            timestamp_unix_ms = self._coerce_int(message.get("timestamp_unix_ms"))
            message_id = self._coerce_str(message.get("id"))
            quoted_message_id = self._coerce_str(message.get("quoted_message_id"))
            mentions = self._normalize_string_list(message.get("mentions"))
        else:
            message_type = self._normalize_message_type(raw_message.get("message_type"))
            attachments = self._normalize_attachments(raw_message.get("attachments"))
            text = self._first_non_empty(raw_message.get("text"))
            timestamp_unix_ms = self._coerce_int(raw_message.get("timestamp_unix_ms"))
            message_id = self._coerce_str(raw_message.get("message_id"))
            quoted_message_id = self._coerce_str(raw_message.get("quoted_message_id"))
            mentions = self._normalize_string_list(raw_message.get("mentions"))

        sender_jid = self._extract_sender_jid(raw_message, sender, chat)
        channel = self._normalize_channel(raw_message, sender_jid, sender, chat)
        content = self._select_content(
            text=text,
            message_type=message_type,
            attachments=attachments,
        )

        metadata: dict[str, Any] = {
            "platform": self.platform,
            "schema_version": raw_message.get("schema_version", 1),
            "event": raw_message.get("event", "message.inbound"),
            "message_id": message_id,
            "message_type": message_type,
            "timestamp_unix_ms": timestamp_unix_ms,
            "quoted_message_id": quoted_message_id,
            "mentions": mentions,
            "attachments": attachments,
            "sender_jid": sender_jid,
            "chat_jid": self._extract_chat_jid(raw_message, chat, sender_jid),
            "chat_type": self._extract_chat_type(raw_message, chat),
            "push_name": self._extract_push_name(raw_message, sender),
            "bridge_event_id": self._coerce_str(raw_message.get("event_id")),
        }

        return {
            "content": content,
            "session_id": None,
            "channel": channel,
            "metadata": metadata,
        }

    async def send(self, message: dict[str, Any], channel: str | None = None) -> None:
        event_type = self._coerce_str(message.get("type")) or ""
        destination = channel or self._extract_channel(message)
        if not destination:
            raise ValueError("WhatsApp outbound message missing channel")
        request_id = self._coerce_str(message.get("request_id"))

        if event_type == "route_result" and request_id:
            self._schedule_delayed_ack(request_id, destination)
            return

        if event_type == "response.thinking.chunk":
            return

        if event_type == "task.created":
            if request_id:
                self._schedule_delayed_ack(request_id, destination)
            return

        if event_type == "response.chunk":
            content = self._coerce_str(message.get("content"))
            if request_id and content:
                self._cancel_delayed_ack(request_id)
                self._stream_buffers.setdefault(request_id, []).append(content)
            return

        if event_type == "task.progress":
            return

        if event_type in {
            "response.complete",
            "task.input_required",
            "task.completed",
            "task.failed",
            "task.cancelled",
            "error",
        } and request_id:
            self._cancel_delayed_ack(request_id)

        rendered = self._render_gateway_event(message)
        if not rendered:
            if event_type in {"task.completed", "task.failed", "task.cancelled", "error"} and request_id:
                self._completed_response_request_ids.discard(request_id)
            return

        await self._send_text(destination, rendered)

        if event_type == "response.complete":
            if request_id:
                self._stream_buffers.pop(request_id, None)
                self._sent_ack_request_ids.discard(request_id)
                self._completed_response_request_ids.add(request_id)
        elif event_type in {"task.failed", "task.cancelled", "error", "task.completed"} and request_id:
            self._sent_ack_request_ids.discard(request_id)
            self._completed_response_request_ids.discard(request_id)

    def _render_gateway_event(self, message: dict[str, Any]) -> str | None:
        event_type = self._coerce_str(message.get("type")) or ""

        if event_type == "response.complete":
            content = self._coerce_str(message.get("content"))
            if content:
                return self._render_whatsapp_text(content)

            request_id = self._coerce_str(message.get("request_id"))
            if request_id:
                buffered = "".join(self._stream_buffers.get(request_id, []))
                return self._render_whatsapp_text(buffered) if buffered else None
            return None

        if event_type == "task.input_required":
            question = self._coerce_str(message.get("question")) or "Input required."
            options = message.get("options")
            lines = [question]
            if isinstance(options, list) and options:
                lines.append("")
                for index, option in enumerate(options, start=1):
                    lines.append(f"{index}. {self._coerce_str(option) or 'Option'}")
            return self._render_whatsapp_text("\n".join(lines))

        if event_type == "task.completed":
            request_id = self._coerce_str(message.get("request_id"))
            if request_id and request_id in self._completed_response_request_ids:
                return None
            result = message.get("result")
            if isinstance(result, dict):
                for key in ("content", "text", "summary", "message"):
                    value = self._coerce_str(result.get(key))
                    if value:
                        return self._render_whatsapp_text(value)
            if isinstance(result, str) and result.strip():
                return self._render_whatsapp_text(result.strip())
            fallback = self._coerce_str(message.get("content")) or "Task completed."
            return self._render_whatsapp_text(fallback)

        if event_type == "task.failed":
            error = message.get("error")
            if isinstance(error, dict):
                error_message = self._coerce_str(error.get("message"))
                if error_message:
                    return self._render_whatsapp_text(f"Task failed: {error_message}")
            return self._render_whatsapp_text(self._coerce_str(message.get("message")) or "Task failed.")

        if event_type == "task.cancelled":
            return self._render_whatsapp_text(self._coerce_str(message.get("message")) or "Stopped.")

        if event_type == "error":
            return self._render_whatsapp_text(self._coerce_str(message.get("message")) or "An error occurred.")

        content = self._coerce_str(message.get("content"))
        return self._render_whatsapp_text(content) if content else None

    async def _send_text(self, channel: str, text: str) -> None:
        if self._http is None:
            raise RuntimeError("WhatsAppAdapter.start() must be called before send()")

        chunks = self._chunk_text(text)
        if not chunks:
            return
        chunks = self._label_chunk_sequence(chunks)

        lock = self._channel_locks.setdefault(channel, asyncio.Lock())
        recipient = self._channel_to_bridge_recipient(channel)
        headers = self._bridge_headers()

        async with lock:
            for index, chunk in enumerate(chunks):
                response = await self._http.post(
                    self.config.send_path,
                    json={"number": recipient, "message": chunk},
                    headers=headers,
                )
                response.raise_for_status()
                if index + 1 < len(chunks) and self.config.send_delay_ms > 0:
                    await asyncio.sleep(self.config.send_delay_ms / 1000.0)

    def _schedule_delayed_ack(self, request_id: str, channel: str) -> None:
        if not request_id or request_id in self._pending_ack_tasks or request_id in self._sent_ack_request_ids:
            return

        self._pending_ack_tasks[request_id] = asyncio.create_task(
            self._send_delayed_ack(request_id, channel),
            name=f"whatsapp-ack:{request_id}",
        )

    def _cancel_delayed_ack(self, request_id: str) -> None:
        task = self._pending_ack_tasks.pop(request_id, None)
        if task is not None:
            task.cancel()

    async def _send_delayed_ack(self, request_id: str, channel: str) -> None:
        current_task = asyncio.current_task()
        try:
            if self.config.ack_delay_sec > 0:
                await asyncio.sleep(self.config.ack_delay_sec)
            await self._send_text(channel, "Thinking...")
            self._sent_ack_request_ids.add(request_id)
        except asyncio.CancelledError:
            raise
        finally:
            current = self._pending_ack_tasks.get(request_id)
            if current is current_task:
                self._pending_ack_tasks.pop(request_id, None)

    def _chunk_text(self, text: str) -> list[str]:
        text = (text or "").replace("\r\n", "\n").strip()
        if not text:
            return []

        limit = self.config.text_chunk_limit
        if len(text) <= limit:
            return [text]

        if self.config.chunk_mode == "length":
            return self._hard_wrap(text, limit)

        chunks: list[str] = []
        current = ""
        for piece in self._preferred_pieces(text):
            if not piece:
                continue

            candidate = piece if not current else f"{current}\n\n{piece}"
            if len(candidate) <= limit:
                current = candidate
                continue

            if current:
                chunks.append(current)
                current = ""

            if len(piece) <= limit:
                current = piece
                continue

            chunks.extend(self._split_oversized_piece(piece, limit))

        if current:
            chunks.append(current)

        return [chunk for chunk in chunks if chunk.strip()]

    def _preferred_pieces(self, text: str) -> list[str]:
        pieces: list[str] = []
        cursor = 0
        for match in re.finditer(r"```[\s\S]*?```", text):
            prefix = text[cursor:match.start()].strip()
            if prefix:
                pieces.extend(self._split_paragraphs(prefix))
            fenced = match.group(0).strip()
            if fenced:
                pieces.append(fenced)
            cursor = match.end()

        suffix = text[cursor:].strip()
        if suffix:
            pieces.extend(self._split_paragraphs(suffix))
        return pieces

    def _split_paragraphs(self, text: str) -> list[str]:
        return [part.strip() for part in re.split(r"\n{2,}", text) if part.strip()]

    def _split_oversized_piece(self, piece: str, limit: int) -> list[str]:
        if piece.startswith("```") and piece.endswith("```"):
            return self._split_fenced_block(piece, limit)

        chunks: list[str] = []
        current = ""
        for line in piece.splitlines():
            line = line.rstrip()
            if not line:
                candidate = current + "\n" if current else ""
            else:
                candidate = line if not current else f"{current}\n{line}"

            if candidate and len(candidate) <= limit:
                current = candidate
                continue

            if current:
                chunks.append(current)
                current = ""

            if len(line) <= limit:
                current = line
                continue

            chunks.extend(self._split_sentences_or_wrap(line, limit))

        if current:
            chunks.append(current)
        return chunks

    def _split_fenced_block(self, fenced_block: str, limit: int) -> list[str]:
        lines = fenced_block.splitlines()
        if not lines:
            return self._hard_wrap(fenced_block, limit)

        fence_header = lines[0]
        body_lines = lines[1:]
        if body_lines and body_lines[-1].strip() == "```":
            body_lines = body_lines[:-1]

        wrapper_overhead = len(fence_header) + len("\n```")
        body_limit = max(100, limit - wrapper_overhead - 1)

        chunks: list[str] = []
        current_body = ""
        for line in body_lines:
            candidate = line if not current_body else f"{current_body}\n{line}"
            if len(candidate) <= body_limit:
                current_body = candidate
                continue

            if current_body:
                chunks.append(f"{fence_header}\n{current_body}\n```")
                current_body = ""

            if len(line) <= body_limit:
                current_body = line
                continue

            for wrapped in self._hard_wrap(line, body_limit):
                chunks.append(f"{fence_header}\n{wrapped}\n```")

        if current_body:
            chunks.append(f"{fence_header}\n{current_body}\n```")
        return chunks

    def _split_sentences_or_wrap(self, text: str, limit: int) -> list[str]:
        parts = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
        if len(parts) <= 1:
            return self._hard_wrap(text, limit)

        chunks: list[str] = []
        current = ""
        for part in parts:
            candidate = part if not current else f"{current} {part}"
            if len(candidate) <= limit:
                current = candidate
                continue

            if current:
                chunks.append(current)
                current = ""

            if len(part) <= limit:
                current = part
                continue

            chunks.extend(self._hard_wrap(part, limit))

        if current:
            chunks.append(current)
        return chunks

    def _hard_wrap(self, text: str, limit: int) -> list[str]:
        chunks: list[str] = []
        remaining = text.strip()
        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break

            split_at = remaining.rfind(" ", 0, limit + 1)
            if split_at < max(1, limit // 2):
                split_at = limit

            chunks.append(remaining[:split_at].strip())
            remaining = remaining[split_at:].strip()
        return [chunk for chunk in chunks if chunk]

    def _label_chunk_sequence(self, chunks: list[str]) -> list[str]:
        if len(chunks) <= 1:
            return chunks
        total = len(chunks)
        return [f"Part {index}/{total}\n\n{chunk}" for index, chunk in enumerate(chunks, start=1)]

    def _render_whatsapp_text(self, text: str | None) -> str | None:
        normalized = self._coerce_str(text)
        if not normalized:
            return None

        normalized = normalized.replace("<awaiting_reply/>", "")
        normalized = re.sub(r"```[a-zA-Z0-9_+-]+\n", "```\n", normalized)
        normalized = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1: \2", normalized)
        normalized = re.sub(r"(?m)^#{1,6}\s+(.+?)\s*$", lambda m: f"*{m.group(1).strip()}*", normalized)
        normalized = re.sub(r"\*\*(.+?)\*\*", r"*\1*", normalized)
        normalized = re.sub(r"__(.+?)__", r"*\1*", normalized)
        normalized = re.sub(r"(?m)^-\s+", "• ", normalized)
        normalized = re.sub(r"(?m)^\*\s+", "• ", normalized)
        normalized = re.sub(r"(?m)^---+$", "────────", normalized)
        normalized = self._render_markdown_tables(normalized)
        normalized = re.sub(r"\n{3,}", "\n\n", normalized)
        return normalized.strip() or None

    def _render_markdown_tables(self, text: str) -> str:
        lines = text.splitlines()
        rendered: list[str] = []
        index = 0
        in_fenced_block = False

        while index < len(lines):
            line = lines[index]
            stripped = line.strip()
            if stripped.startswith("```"):
                in_fenced_block = not in_fenced_block
                rendered.append(line)
                index += 1
                continue

            if (
                not in_fenced_block
                and index + 1 < len(lines)
                and self._looks_like_table_row(line)
                and self._is_markdown_table_separator(lines[index + 1])
            ):
                table_lines = [line, lines[index + 1]]
                index += 2
                while index < len(lines) and self._looks_like_table_row(lines[index]):
                    table_lines.append(lines[index])
                    index += 1
                rendered.append(self._format_markdown_table(table_lines))
                continue

            rendered.append(line)
            index += 1

        return "\n".join(rendered)

    def _looks_like_table_row(self, line: str) -> bool:
        stripped = line.strip()
        return bool(stripped) and "|" in stripped

    def _is_markdown_table_separator(self, line: str) -> bool:
        stripped = line.strip()
        return bool(
            re.fullmatch(r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?", stripped)
        )

    def _format_markdown_table(self, table_lines: list[str]) -> str:
        rows = [self._parse_markdown_table_row(line) for line in table_lines]
        if len(rows) < 2 or not rows[0]:
            return "\n".join(table_lines)

        header = rows[0]
        data_rows = [row for row in rows[2:] if any(cell for cell in row)]
        column_count = max(len(header), *(len(row) for row in data_rows)) if data_rows else len(header)
        if column_count == 0:
            return "\n".join(table_lines)

        normalized_rows = [self._normalize_table_cells(header, column_count)]
        normalized_rows.extend(self._normalize_table_cells(row, column_count) for row in data_rows)
        widths = [
            max(len(row[column_index]) for row in normalized_rows)
            for column_index in range(column_count)
        ]

        def render_row(row: list[str]) -> str:
            return " | ".join(cell.ljust(widths[idx]) for idx, cell in enumerate(row))

        separator = "-+-".join("-" * width for width in widths)
        rendered_lines = [render_row(normalized_rows[0]), separator]
        rendered_lines.extend(render_row(row) for row in normalized_rows[1:])
        return "```\n" + "\n".join(rendered_lines).rstrip() + "\n```"

    def _parse_markdown_table_row(self, line: str) -> list[str]:
        stripped = line.strip()
        if stripped.startswith("|"):
            stripped = stripped[1:]
        if stripped.endswith("|"):
            stripped = stripped[:-1]
        return [cell.strip() for cell in stripped.split("|")]

    def _normalize_table_cells(self, row: list[str], column_count: int) -> list[str]:
        if len(row) < column_count:
            row = row + [""] * (column_count - len(row))
        return row[:column_count]

    def _should_send_progress(self, message: dict[str, Any]) -> bool:
        task_id = self._coerce_str(message.get("task_id"))
        if not task_id:
            return True

        now = time.monotonic()
        last_sent = self._last_progress_sent.get(task_id)
        if last_sent is not None and (now - last_sent) < self.config.progress_min_interval_sec:
            return False

        self._last_progress_sent[task_id] = now
        return True

    def _bridge_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.config.bridge_token:
            headers["X-Bridge-Token"] = self.config.bridge_token
        return headers

    async def send_test_message(self, number: str, message: str) -> dict[str, Any]:
        await self.ensure_ready()
        if self._http is None:
            raise RuntimeError("WhatsApp bridge client is not initialized")

        return await self._request_bridge_json(
            "POST",
            self.config.send_path,
            json={"number": number, "message": message},
            headers=self._bridge_headers(),
        )

    async def _request_bridge_json(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if self._http is None:
            raise RuntimeError("WhatsApp bridge client is not initialized")

        try:
            response = await self._http.request(method, path, **kwargs)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise RuntimeError("WhatsApp bridge request timed out") from exc
        except httpx.HTTPStatusError as exc:
            detail = self._extract_bridge_error_detail(exc.response)
            raise RuntimeError(detail) from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"WhatsApp bridge request failed: {exc}") from exc

        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("WhatsApp bridge returned a non-object payload")
        return payload

    def _extract_bridge_error_detail(self, response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            payload = None

        if isinstance(payload, dict):
            for key in ("detail", "error", "message"):
                value = self._coerce_str(payload.get(key))
                if value:
                    return value

            bridge_status = payload.get("bridge_status")
            if isinstance(bridge_status, dict):
                for key in ("last_error",):
                    value = self._coerce_str(bridge_status.get(key))
                    if value:
                        return value
                disconnect_code = self._coerce_int(bridge_status.get("last_disconnect_code"))
                if disconnect_code is not None:
                    return f"WhatsApp bridge pairing failed (disconnect code {disconnect_code})"

        return f"WhatsApp bridge request failed ({response.status_code})"

    def _normalize_message_type(self, value: Any) -> str:
        text = (self._coerce_str(value) or "unknown").lower()
        return text if text in SUPPORTED_ATTACHMENT_TYPES else "unknown"

    def _normalize_attachments(self, attachments: Any) -> list[dict[str, Any]]:
        if not isinstance(attachments, list):
            return []

        normalized: list[dict[str, Any]] = []
        for index, attachment in enumerate(attachments, start=1):
            if not isinstance(attachment, dict):
                continue

            kind = self._normalize_message_type(attachment.get("kind"))
            metadata = {
                key: value
                for key, value in attachment.items()
                if key
                not in {
                    "id",
                    "kind",
                    "mime_type",
                    "filename",
                    "caption",
                    "size_bytes",
                    "width",
                    "height",
                    "duration_ms",
                    "sha256",
                    "bridge_media_ref",
                    "download_url",
                }
            }
            normalized.append(
                {
                    "id": self._coerce_str(attachment.get("id")) or f"att_{index}",
                    "kind": kind,
                    "mime_type": self._coerce_str(attachment.get("mime_type")),
                    "filename": self._coerce_str(attachment.get("filename")),
                    "caption": self._coerce_str(attachment.get("caption")),
                    "size_bytes": self._coerce_int(attachment.get("size_bytes")),
                    "width": self._coerce_int(attachment.get("width")),
                    "height": self._coerce_int(attachment.get("height")),
                    "duration_ms": self._coerce_int(attachment.get("duration_ms")),
                    "sha256": self._coerce_str(attachment.get("sha256")),
                    "bridge_media_ref": self._coerce_str(attachment.get("bridge_media_ref")),
                    "download_url": self._coerce_str(attachment.get("download_url")),
                    "metadata": metadata or None,
                }
            )
        return normalized

    def _normalize_channel(
        self,
        raw_message: dict[str, Any],
        sender_jid: str | None,
        sender: Any,
        chat: Any,
    ) -> str:
        explicit_channel = self._coerce_str(raw_message.get("channel"))
        if explicit_channel and explicit_channel.startswith("whatsapp:"):
            return explicit_channel

        sender_phone = None
        if isinstance(sender, dict):
            sender_phone = self._coerce_str(sender.get("phone"))

        if sender_phone:
            return self.channel_id({"id": sender_phone})

        chat_jid = self._extract_chat_jid(raw_message, chat, sender_jid)
        return self.channel_id({"id": self._canonical_whatsapp_id(chat_jid or sender_jid or "unknown")})

    def _extract_channel(self, message: dict[str, Any]) -> str | None:
        channel = self._coerce_str(message.get("channel"))
        if channel:
            return channel

        metadata = message.get("metadata")
        if isinstance(metadata, dict):
            return self._coerce_str(metadata.get("channel"))
        return None

    def _channel_to_bridge_recipient(self, channel: str) -> str:
        platform, _, destination = channel.partition(":")
        if platform != self.platform or not destination:
            raise ValueError(f"Invalid WhatsApp channel: {channel}")

        if destination.endswith((WHATSAPP_DM_SUFFIX, WHATSAPP_GROUP_SUFFIX)):
            return destination

        digits = re.sub(r"[^\d]", "", destination)
        if digits:
            return digits
        return destination

    def _extract_sender_jid(self, raw_message: dict[str, Any], sender: Any, chat: Any) -> str | None:
        if isinstance(sender, dict):
            jid = self._coerce_str(sender.get("jid"))
            if jid:
                return jid

        for key in ("sender_jid", "remote_jid", "remoteJid", "sender"):
            value = self._coerce_str(raw_message.get(key))
            if value and "@" in value:
                return value

        if isinstance(chat, dict):
            jid = self._coerce_str(chat.get("jid"))
            if jid:
                return jid

        return None

    def _extract_chat_jid(self, raw_message: dict[str, Any], chat: Any, sender_jid: str | None) -> str | None:
        if isinstance(chat, dict):
            jid = self._coerce_str(chat.get("jid"))
            if jid:
                return jid

        chat_jid = self._coerce_str(raw_message.get("chat_jid"))
        if chat_jid:
            return chat_jid

        return sender_jid

    def _extract_chat_type(self, raw_message: dict[str, Any], chat: Any) -> str:
        if isinstance(chat, dict):
            chat_type = self._coerce_str(chat.get("type"))
            if chat_type:
                return chat_type

        sender_jid = self._extract_sender_jid(raw_message, raw_message.get("sender"), chat)
        if sender_jid and sender_jid.endswith(WHATSAPP_GROUP_SUFFIX):
            return "group"
        return "dm"

    def _extract_push_name(self, raw_message: dict[str, Any], sender: Any) -> str | None:
        if isinstance(sender, dict):
            for key in ("push_name", "pushName", "name"):
                value = self._coerce_str(sender.get(key))
                if value:
                    return value
        return self._coerce_str(raw_message.get("push_name"))

    def _select_content(
        self,
        *,
        text: str | None,
        message_type: str,
        attachments: list[dict[str, Any]],
    ) -> str:
        if text:
            return text

        first_attachment = attachments[0] if attachments else {}
        filename = self._coerce_str(first_attachment.get("filename"))
        if message_type == "image":
            return "[image]"
        if message_type == "video":
            return "[video]"
        if message_type == "audio":
            return "[audio]"
        if message_type == "voice":
            return "[voice note]"
        if message_type == "document":
            return f"[document: {filename}]" if filename else "[document]"
        if message_type == "sticker":
            return "[sticker]"
        if message_type == "location":
            return "Location shared"
        if message_type == "contact":
            return "Contact shared"
        if message_type == "reaction":
            return "[reaction]"
        if message_type == "button_reply":
            return "[button reply]"
        if message_type == "list_reply":
            return "[list reply]"
        if message_type == "poll_reply":
            return "[poll reply]"
        return "[unsupported whatsapp message]"

    def _canonical_whatsapp_id(self, jid: str) -> str:
        if not jid:
            return "unknown"
        if jid.endswith(WHATSAPP_GROUP_SUFFIX):
            return jid

        local = jid.split("@", 1)[0]
        digits = re.sub(r"[^\d]", "", local)
        if digits and len(digits) >= 7:
            return f"+{digits}"
        return local or jid

    def _normalize_string_list(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [item for item in (self._coerce_str(entry) for entry in value) if item]

    def _first_non_empty(self, *values: Any) -> str | None:
        for value in values:
            text = self._coerce_str(value)
            if text:
                return text
        return None

    def _coerce_int(self, value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _coerce_str(self, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        if isinstance(value, (int, float)):
            return str(value)
        return None
