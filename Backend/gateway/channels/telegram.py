from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .base import ChannelAdapter, MessageCallback, NormalizedMessage

logger = logging.getLogger(__name__)

DEFAULT_TELEGRAM_API_BASE_URL = "https://api.telegram.org"
DEFAULT_TELEGRAM_TEXT_CHUNK_LIMIT = 4000
DEFAULT_ALLOWED_UPDATES = ("message", "edited_message")
SUPPORTED_TELEGRAM_MESSAGE_TYPES = {
    "audio",
    "document",
    "image",
    "sticker",
    "text",
    "unknown",
    "video",
    "voice",
}
TABLE_SEPARATOR_PATTERN = re.compile(r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?")


@dataclass(slots=True)
class TelegramConfig:
    bot_token: str
    webhook_secret: str
    api_base_url: str = DEFAULT_TELEGRAM_API_BASE_URL
    public_base_url: str | None = None
    enabled: bool = False
    auto_configure_webhook: bool = True
    allowed_user_id: int | None = None
    allowed_chat_id: int | None = None
    text_chunk_limit: int = DEFAULT_TELEGRAM_TEXT_CHUNK_LIMIT
    send_delay_ms: int = 120
    chat_action_min_interval_sec: float = 4.0
    allowed_updates: tuple[str, ...] = field(default_factory=lambda: DEFAULT_ALLOWED_UPDATES)
    webhook_path: str = "/channels/telegram/webhook"
    api_read_timeout_sec: float = 20.0
    api_connect_timeout_sec: float = 5.0

    @classmethod
    def from_env(cls, *, gateway_public_host: str | None = None) -> "TelegramConfig":
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        webhook_secret = (
            os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
            or os.getenv("TELEGRAM_SECRET_TOKEN", "").strip()
        )
        api_base_url = (
            os.getenv("TELEGRAM_API_BASE_URL", DEFAULT_TELEGRAM_API_BASE_URL).strip().rstrip("/")
        ) or DEFAULT_TELEGRAM_API_BASE_URL
        public_base_url = os.getenv("TELEGRAM_WEBHOOK_BASE_URL", "").strip().rstrip("/")
        if not public_base_url and gateway_public_host:
            public_base_url = "https://{0}".format(gateway_public_host.strip())
        allowed_updates_raw = os.getenv("TELEGRAM_ALLOWED_UPDATES", "")
        allowed_updates = tuple(
            item.strip()
            for item in allowed_updates_raw.split(",")
            if item.strip()
        ) or DEFAULT_ALLOWED_UPDATES
        return cls(
            bot_token=bot_token,
            webhook_secret=webhook_secret,
            api_base_url=api_base_url,
            public_base_url=public_base_url or None,
            enabled=_env_bool("TELEGRAM_ENABLED", False),
            auto_configure_webhook=_env_bool("TELEGRAM_AUTO_CONFIGURE_WEBHOOK", True),
            allowed_user_id=_env_int("TELEGRAM_ALLOWED_USER_ID"),
            allowed_chat_id=_env_int("TELEGRAM_ALLOWED_CHAT_ID"),
            text_chunk_limit=max(500, _env_int("TELEGRAM_TEXT_CHUNK_LIMIT", DEFAULT_TELEGRAM_TEXT_CHUNK_LIMIT) or DEFAULT_TELEGRAM_TEXT_CHUNK_LIMIT),
            send_delay_ms=max(0, _env_int("TELEGRAM_SEND_DELAY_MS", 120) or 120),
            chat_action_min_interval_sec=max(
                0.0,
                _env_float("TELEGRAM_CHAT_ACTION_MIN_INTERVAL_SEC", 4.0) or 4.0,
            ),
            allowed_updates=allowed_updates,
        )

    @property
    def webhook_url(self) -> str | None:
        if not self.public_base_url:
            return None
        return "{0}{1}".format(self.public_base_url.rstrip("/"), self.webhook_path)

    @property
    def bot_api_base(self) -> str:
        return "{0}/bot{1}".format(self.api_base_url, self.bot_token)


class TelegramAdapter(ChannelAdapter):
    """Gateway-side Telegram adapter using the official Bot API."""

    platform = "telegram"

    def __init__(self, config: TelegramConfig, http_client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._callback: MessageCallback | None = None
        self._http = http_client
        self._owns_http_client = http_client is None
        self._stream_buffers: dict[str, list[str]] = {}
        self._completed_response_request_ids: set[str] = set()
        self._last_chat_action_sent: dict[int, float] = {}
        self._channel_locks: dict[int, asyncio.Lock] = {}
        self._bot_info: dict[str, Any] | None = None

    async def start(self) -> None:
        if not self.config.enabled:
            return
        if not self.config.bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required when TELEGRAM_ENABLED=true")
        if not self.config.webhook_secret:
            raise RuntimeError("TELEGRAM_WEBHOOK_SECRET is required when TELEGRAM_ENABLED=true")
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    self.config.api_read_timeout_sec,
                    connect=self.config.api_connect_timeout_sec,
                )
            )
        self._bot_info = await self._call_api("getMe")
        logger.info(
            "telegram.adapter started bot_id=%s username=%s webhook_url=%s",
            self._bot_info.get("id") if isinstance(self._bot_info, dict) else None,
            self._bot_info.get("username") if isinstance(self._bot_info, dict) else None,
            self.config.webhook_url,
        )
        if self.config.auto_configure_webhook and self.config.webhook_url:
            await self.sync_webhook()

    async def stop(self) -> None:
        self._stream_buffers.clear()
        self._completed_response_request_ids.clear()
        self._last_chat_action_sent.clear()
        self._bot_info = None
        if self._owns_http_client and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def on_message(self, callback: MessageCallback) -> None:
        self._callback = callback

    async def send(self, message: dict[str, Any], channel: str | None = None) -> None:
        if not self.config.enabled:
            return
        destination = channel or self._extract_channel(message)
        if not destination:
            raise ValueError("Telegram outbound message missing channel")
        chat_id = self._channel_to_chat_id(destination)
        event_type = self._coerce_str(message.get("type")) or ""
        request_id = self._coerce_str(message.get("request_id"))

        if event_type in {"route_result", "task.created", "response.thinking.chunk", "task.progress"}:
            await self._send_chat_action(chat_id)
            return

        if event_type == "response.chunk":
            content = self._coerce_str(message.get("content"))
            if request_id and content:
                self._stream_buffers.setdefault(request_id, []).append(content)
                await self._send_chat_action(chat_id)
            return

        rendered = self._render_gateway_event(message)
        if not rendered:
            if event_type in {"task.completed", "task.failed", "task.cancelled", "error"} and request_id:
                self._completed_response_request_ids.discard(request_id)
            return

        await self._send_text(chat_id, rendered)

        if event_type == "response.complete":
            if request_id:
                self._stream_buffers.pop(request_id, None)
                self._completed_response_request_ids.add(request_id)
        elif event_type in {"task.failed", "task.cancelled", "error", "task.completed"} and request_id:
            self._completed_response_request_ids.discard(request_id)

    async def get_status(self) -> dict[str, Any]:
        if not self.config.enabled:
            return {"status": "disabled"}
        bot = self._bot_info or await self._call_api("getMe")
        webhook = await self._call_api("getWebhookInfo")
        return {
            "status": "connected",
            "bot": bot,
            "webhook": webhook,
            "webhook_url": self.config.webhook_url,
            "allowed_user_id": self.config.allowed_user_id,
            "allowed_chat_id": self.config.allowed_chat_id,
        }

    async def sync_webhook(self) -> dict[str, Any]:
        webhook_url = self.config.webhook_url
        if not webhook_url:
            raise RuntimeError("Cannot sync Telegram webhook without GATEWAY_PUBLIC_HOST / TELEGRAM_WEBHOOK_BASE_URL.")
        started_at = time.perf_counter()
        await self._call_api(
            "setWebhook",
            json={
                "url": webhook_url,
                "secret_token": self.config.webhook_secret,
                "allowed_updates": list(self.config.allowed_updates),
                "drop_pending_updates": False,
            },
        )
        webhook_info = await self._call_api("getWebhookInfo")
        logger.info(
            "telegram.webhook synced url=%s elapsed_ms=%.1f",
            webhook_info.get("url") if isinstance(webhook_info, dict) else webhook_url,
            (time.perf_counter() - started_at) * 1000.0,
        )
        return webhook_info

    async def delete_webhook(self, *, drop_pending_updates: bool = False) -> dict[str, Any]:
        started_at = time.perf_counter()
        await self._call_api(
            "deleteWebhook",
            json={"drop_pending_updates": bool(drop_pending_updates)},
        )
        webhook_info = await self._call_api("getWebhookInfo")
        logger.info(
            "telegram.webhook deleted drop_pending_updates=%s elapsed_ms=%.1f",
            bool(drop_pending_updates),
            (time.perf_counter() - started_at) * 1000.0,
        )
        return webhook_info

    async def send_test_message(self, *, chat_id: int, message: str) -> dict[str, Any]:
        started_at = time.perf_counter()
        result = await self._call_api(
            "sendMessage",
            json={
                "chat_id": chat_id,
                "text": message,
                "disable_web_page_preview": True,
            },
        )
        logger.info(
            "telegram.send_test chat_id=%s chars=%s elapsed_ms=%.1f",
            chat_id,
            len(message or ""),
            (time.perf_counter() - started_at) * 1000.0,
        )
        return result

    async def download_file(self, file_id: str) -> tuple[bytes, str | None]:
        started_at = time.perf_counter()
        file_info = await self._call_api("getFile", json={"file_id": file_id})
        file_path = self._coerce_str(file_info.get("file_path"))
        if not file_path:
            raise RuntimeError("Telegram getFile returned no file_path")
        if self._http is None:
            raise RuntimeError("Telegram adapter HTTP client is not initialized")
        url = "{0}/file/bot{1}/{2}".format(self.config.api_base_url, self.config.bot_token, file_path.lstrip("/"))
        response = await self._http.get(url)
        response.raise_for_status()
        content = response.content
        media_type = self._coerce_str(response.headers.get("content-type"))
        logger.info(
            "telegram.media downloaded file_id=%s bytes=%s content_type=%s elapsed_ms=%.1f",
            file_id,
            len(content),
            media_type,
            (time.perf_counter() - started_at) * 1000.0,
        )
        return content, media_type

    async def handle_webhook_update(
        self,
        payload: dict[str, Any],
        *,
        secret_token: str | None,
    ) -> NormalizedMessage | None:
        self._verify_secret_token(secret_token)
        normalized = self.normalize_message(payload)
        if not normalized:
            return None
        if self._callback is None:
            raise RuntimeError("TelegramAdapter.on_message() must be registered before handling inbound traffic")
        processed = await self._callback(normalized)
        if isinstance(processed, dict):
            return processed
        return normalized

    def verify_webhook_secret(self, provided_secret: str | None) -> None:
        self._verify_secret_token(provided_secret)

    def normalize_message(self, raw_message: Any) -> NormalizedMessage | None:
        if not isinstance(raw_message, dict):
            raise TypeError("Telegram update payload must be a dict")

        message = None
        event = None
        for key in self.config.allowed_updates:
            candidate = raw_message.get(key)
            if isinstance(candidate, dict):
                message = candidate
                event = key
                break
        if message is None:
            return None

        chat = message.get("chat")
        sender = message.get("from")
        if not isinstance(chat, dict) or not isinstance(sender, dict):
            return None
        chat_type = self._coerce_str(chat.get("type")) or ""
        if chat_type != "private":
            return None

        chat_id = self._coerce_int(chat.get("id"))
        user_id = self._coerce_int(sender.get("id"))
        if chat_id is None or user_id is None:
            return None

        if self.config.allowed_chat_id is not None and chat_id != self.config.allowed_chat_id:
            logger.info("telegram.incoming ignored chat_id=%s reason=chat_allowlist", chat_id)
            return None
        if self.config.allowed_user_id is not None and user_id != self.config.allowed_user_id:
            logger.info("telegram.incoming ignored user_id=%s reason=user_allowlist", user_id)
            return None

        message_type, attachments = self._extract_attachments(message)
        text = self._first_non_empty(message.get("text"), message.get("caption"))
        content = self._select_content(text=text, message_type=message_type, attachments=attachments)

        metadata: dict[str, Any] = {
            "platform": self.platform,
            "event": event,
            "message_id": self._coerce_int(message.get("message_id")),
            "message_type": message_type,
            "timestamp_unix_ms": self._coerce_int(message.get("date"), multiplier=1000),
            "attachments": attachments,
            "chat_id": chat_id,
            "chat_type": chat_type,
            "user_id": user_id,
            "username": self._coerce_str(sender.get("username")),
            "first_name": self._coerce_str(sender.get("first_name")),
            "language_code": self._coerce_str(sender.get("language_code")),
        }
        logger.info(
            "telegram.incoming accepted chat_id=%s user_id=%s message_type=%s attachments=%s message_id=%s",
            chat_id,
            user_id,
            message_type,
            len(attachments),
            metadata.get("message_id"),
        )
        return {
            "content": content,
            "session_id": None,
            "channel": "telegram:chat_{0}".format(chat_id),
            "metadata": metadata,
        }

    async def _send_text(self, chat_id: int, text: str) -> None:
        chunks = self._chunk_text(text)
        if not chunks:
            return
        chunks = self._label_chunk_sequence(chunks)
        lock = self._channel_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            for index, chunk in enumerate(chunks):
                started_at = time.perf_counter()
                await self._call_api(
                    "sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": chunk,
                        "disable_web_page_preview": True,
                    },
                )
                logger.info(
                    "telegram.send_text chat_id=%s part=%s/%s chars=%s elapsed_ms=%.1f",
                    chat_id,
                    index + 1,
                    len(chunks),
                    len(chunk),
                    (time.perf_counter() - started_at) * 1000.0,
                )
                if index + 1 < len(chunks) and self.config.send_delay_ms > 0:
                    await asyncio.sleep(self.config.send_delay_ms / 1000.0)

    async def _send_chat_action(self, chat_id: int) -> None:
        now = time.monotonic()
        last_sent = self._last_chat_action_sent.get(chat_id)
        if last_sent is not None and (now - last_sent) < self.config.chat_action_min_interval_sec:
            return
        started_at = time.perf_counter()
        await self._call_api(
            "sendChatAction",
            json={"chat_id": chat_id, "action": "typing"},
        )
        self._last_chat_action_sent[chat_id] = now
        logger.info(
            "telegram.chat_action chat_id=%s action=typing elapsed_ms=%.1f",
            chat_id,
            (time.perf_counter() - started_at) * 1000.0,
        )

    async def _call_api(self, method: str, *, json: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._http is None:
            raise RuntimeError("Telegram adapter HTTP client is not initialized")
        response = await self._http.post("{0}/{1}".format(self.config.bot_api_base, method), json=json or {})
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Telegram Bot API returned a non-object payload")
        if not payload.get("ok", False):
            description = self._coerce_str(payload.get("description")) or "Telegram Bot API request failed"
            raise RuntimeError(description)
        result = payload.get("result")
        if isinstance(result, dict):
            return result
        if result is None:
            return {}
        return {"result": result}

    def _verify_secret_token(self, provided_secret: str | None) -> None:
        provided = (provided_secret or "").strip()
        if not hmac.compare_digest(provided, self.config.webhook_secret):
            raise PermissionError("Invalid Telegram webhook secret token")

    def _render_gateway_event(self, message: dict[str, Any]) -> str | None:
        event_type = self._coerce_str(message.get("type")) or ""

        if event_type == "response.complete":
            content = self._coerce_str(message.get("content"))
            if content:
                return self._render_telegram_text(content)
            request_id = self._coerce_str(message.get("request_id"))
            if request_id:
                buffered = "".join(self._stream_buffers.get(request_id, []))
                return self._render_telegram_text(buffered) if buffered else None
            return None

        if event_type == "task.input_required":
            question = self._coerce_str(message.get("question")) or "Input required."
            options = message.get("options")
            lines = [question]
            if isinstance(options, list) and options:
                lines.append("")
                for index, option in enumerate(options, start=1):
                    lines.append("{0}. {1}".format(index, self._coerce_str(option) or "Option"))
            return self._render_telegram_text("\n".join(lines))

        if event_type == "task.completed":
            request_id = self._coerce_str(message.get("request_id"))
            if request_id and request_id in self._completed_response_request_ids:
                return None
            result = message.get("result")
            if isinstance(result, dict):
                for key in ("content", "text", "summary", "message"):
                    value = self._coerce_str(result.get(key))
                    if value:
                        return self._render_telegram_text(value)
            if isinstance(result, str) and result.strip():
                return self._render_telegram_text(result.strip())
            fallback = self._coerce_str(message.get("content")) or "Task completed."
            return self._render_telegram_text(fallback)

        if event_type == "task.failed":
            error = message.get("error")
            if isinstance(error, dict):
                error_message = self._coerce_str(error.get("message"))
                if error_message:
                    return self._render_telegram_text("Task failed: {0}".format(error_message))
            return self._render_telegram_text(self._coerce_str(message.get("message")) or "Task failed.")

        if event_type == "task.cancelled":
            return self._render_telegram_text(self._coerce_str(message.get("message")) or "Stopped.")

        if event_type == "error":
            return self._render_telegram_text(self._coerce_str(message.get("message")) or "An error occurred.")

        content = self._coerce_str(message.get("content"))
        return self._render_telegram_text(content) if content else None

    def _render_telegram_text(self, text: str | None) -> str | None:
        normalized = self._coerce_str(text)
        if not normalized:
            return None
        normalized = normalized.replace("<awaiting_reply/>", "")
        normalized = re.sub(r"```[a-zA-Z0-9_+-]+\n", "```\n", normalized)
        normalized = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1: \2", normalized)
        normalized = re.sub(r"(?m)^#{1,6}[ \t]+(.+?)$", lambda match: match.group(1).strip(), normalized)
        normalized = re.sub(r"\*\*(.+?)\*\*", r"\1", normalized)
        normalized = re.sub(r"__(.+?)__", r"\1", normalized)
        normalized = re.sub(r"(?m)^-\s+", "• ", normalized)
        normalized = re.sub(r"(?m)^\*\s+", "• ", normalized)
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
                and TABLE_SEPARATOR_PATTERN.fullmatch(lines[index + 1].strip())
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

    def _format_markdown_table(self, table_lines: list[str]) -> str:
        rows = [self._parse_markdown_table_row(line) for line in table_lines]
        if len(rows) < 2 or not rows[0]:
            return "\n".join(table_lines)
        header = rows[0]
        data_rows = [row for row in rows[2:] if any(cell for cell in row)]
        column_count = max(len(header), *(len(row) for row in data_rows)) if data_rows else len(header)
        normalized_header = self._normalize_table_cells(header, column_count)
        normalized_rows = [self._normalize_table_cells(row, column_count) for row in data_rows]
        rendered_lines: list[str] = []
        for row in normalized_rows:
            parts: list[str] = []
            for index, cell in enumerate(row):
                if not cell:
                    continue
                column_name = normalized_header[index] or "Column {0}".format(index + 1)
                parts.append("{0}: {1}".format(column_name, cell))
            if parts:
                rendered_lines.append("• " + " | ".join(parts))
        return "\n".join(rendered_lines) if rendered_lines else "\n".join(table_lines)

    def _extract_attachments(self, message: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        attachments: list[dict[str, Any]] = []
        if isinstance(message.get("photo"), list) and message["photo"]:
            largest = self._select_largest_photo(message["photo"])
            if largest:
                attachments.append(
                    self._build_attachment(
                        largest,
                        kind="image",
                        caption=self._coerce_str(message.get("caption")),
                        mime_type="image/jpeg",
                    )
                )
                return "image", attachments
        for key, kind in (
            ("video", "video"),
            ("animation", "video"),
            ("audio", "audio"),
            ("voice", "voice"),
            ("document", "document"),
            ("sticker", "sticker"),
            ("video_note", "video"),
        ):
            candidate = message.get(key)
            if isinstance(candidate, dict):
                attachments.append(
                    self._build_attachment(
                        candidate,
                        kind=kind,
                        caption=self._coerce_str(message.get("caption")),
                    )
                )
                return kind if kind in SUPPORTED_TELEGRAM_MESSAGE_TYPES else "unknown", attachments
        text = self._coerce_str(message.get("text"))
        if text:
            return "text", []
        return "unknown", []

    def _build_attachment(
        self,
        payload: dict[str, Any],
        *,
        kind: str,
        caption: str | None = None,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        file_id = self._coerce_str(payload.get("file_id"))
        file_unique_id = self._coerce_str(payload.get("file_unique_id"))
        attachment_id = file_unique_id or file_id or "tg_{0}".format(int(time.time() * 1000))
        safe_file_id = file_id or attachment_id
        return {
            "artifact_id": "tg_{0}".format(attachment_id),
            "id": attachment_id,
            "kind": kind,
            "mime_type": mime_type or self._coerce_str(payload.get("mime_type")),
            "filename": self._coerce_str(payload.get("file_name")),
            "caption": caption,
            "size_bytes": self._coerce_int(payload.get("file_size")),
            "width": self._coerce_int(payload.get("width")),
            "height": self._coerce_int(payload.get("height")),
            "duration_ms": self._coerce_int(payload.get("duration"), multiplier=1000),
            "sha256": None,
            "bridge_media_ref": "telegram:file:{0}".format(safe_file_id),
            "download_url": "/internal/channels/telegram/media/{0}".format(safe_file_id),
            "telegram_file_id": safe_file_id,
            "telegram_file_unique_id": file_unique_id,
        }

    def _select_largest_photo(self, photos: list[Any]) -> dict[str, Any] | None:
        candidates = [item for item in photos if isinstance(item, dict)]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda item: (
                self._coerce_int(item.get("file_size")) or 0,
                self._coerce_int(item.get("width")) or 0,
                self._coerce_int(item.get("height")) or 0,
            ),
        )

    def _select_content(self, *, text: str | None, message_type: str, attachments: list[dict[str, Any]]) -> str:
        if text:
            return text
        if attachments:
            return "[{0}]".format(message_type)
        return "[telegram message]"

    def _extract_channel(self, message: dict[str, Any]) -> str | None:
        channel = self._coerce_str(message.get("channel"))
        if channel:
            return channel
        metadata = message.get("metadata")
        if isinstance(metadata, dict):
            chat_id = self._coerce_int(metadata.get("chat_id"))
            if chat_id is not None:
                return "telegram:chat_{0}".format(chat_id)
        return None

    def _channel_to_chat_id(self, channel: str) -> int:
        normalized = channel.strip()
        if normalized.startswith("telegram:chat_"):
            raw = normalized.split("telegram:chat_", 1)[1]
        elif normalized.startswith("telegram:"):
            raw = normalized.split("telegram:", 1)[1]
        else:
            raise ValueError("Invalid Telegram channel: {0!r}".format(channel))
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError("Telegram channel is missing a numeric chat id") from exc

    def _chunk_text(self, text: str) -> list[str]:
        normalized = (text or "").replace("\r\n", "\n").strip()
        if not normalized:
            return []
        limit = self.config.text_chunk_limit
        if len(normalized) <= limit:
            return [normalized]
        chunks: list[str] = []
        current = ""
        for piece in [part.strip() for part in re.split(r"\n{2,}", normalized) if part.strip()]:
            candidate = piece if not current else "{0}\n\n{1}".format(current, piece)
            if len(candidate) <= limit:
                current = candidate
                continue
            if current:
                chunks.append(current)
                current = ""
            if len(piece) <= limit:
                current = piece
            else:
                chunks.extend(self._hard_wrap(piece, limit))
        if current:
            chunks.append(current)
        return [chunk for chunk in chunks if chunk.strip()]

    def _label_chunk_sequence(self, chunks: list[str]) -> list[str]:
        if len(chunks) <= 1:
            return chunks
        total = len(chunks)
        return ["Part {0}/{1}\n\n{2}".format(index, total, chunk) for index, chunk in enumerate(chunks, start=1)]

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

    def _looks_like_table_row(self, line: str) -> bool:
        stripped = line.strip()
        return bool(stripped) and "|" in stripped

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

    def _first_non_empty(self, *values: Any) -> str | None:
        for value in values:
            text = self._coerce_str(value)
            if text:
                return text
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

    def _coerce_int(self, value: Any, *, multiplier: int = 1) -> int | None:
        try:
            if value is None:
                return None
            return int(value) * multiplier
        except (TypeError, ValueError):
            return None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int | None = None) -> int | None:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float | None = None) -> float | None:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default
