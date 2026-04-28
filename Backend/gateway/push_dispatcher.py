from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Awaitable

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PushNotification:
    device_id: str
    token: str
    title: str
    body: str
    data: dict[str, Any]
    priority: str = "default"


class ExpoPushDispatcher:
    """Small Expo push client.

    Gateway push must never block a user-facing response path. Runtime schedules
    this dispatcher in background tasks and treats failures as telemetry only.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        access_token: str,
        push_url: str,
        timeout_sec: float,
        unregister_token: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.enabled = enabled
        self.access_token = access_token.strip()
        self.push_url = push_url.strip()
        self.unregister_token = unregister_token
        timeout = httpx.Timeout(timeout_sec, connect=min(timeout_sec, 5.0))
        self._client = httpx.AsyncClient(timeout=timeout, http2=True)

    async def close(self) -> None:
        await self._client.aclose()

    async def send(self, notification: PushNotification) -> bool:
        if not self.enabled:
            return False
        if not notification.token or not self.push_url:
            return False

        payload = {
            "to": notification.token,
            "title": _truncate(notification.title, 80),
            "body": _truncate(notification.body, 180),
            "data": _json_safe_dict(notification.data, max_items=24),
            "sound": "default",
            "priority": "high" if notification.priority == "high" else "default",
        }
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        try:
            response = await self._client.post(
                self.push_url,
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            logger.warning(
                "gateway.push.expo_send_failed device_id=%s reason=%s",
                notification.device_id,
                exc,
            )
            return False

        error = _extract_expo_error(body)
        if error:
            logger.warning(
                "gateway.push.expo_rejected device_id=%s error=%s",
                notification.device_id,
                error,
            )
            if error == "DeviceNotRegistered" and self.unregister_token is not None:
                try:
                    await self.unregister_token(notification.device_id)
                except Exception:
                    logger.exception(
                        "gateway.push.unregister_failed device_id=%s",
                        notification.device_id,
                    )
            return False

        logger.info(
            "gateway.push.expo_sent device_id=%s type=%s",
            notification.device_id,
            notification.data.get("type"),
        )
        return True


def _truncate(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def _json_safe_dict(value: dict[str, Any], *, max_items: int) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, raw in list(value.items())[:max_items]:
        if not isinstance(key, str) or not key:
            continue
        if raw is None or isinstance(raw, (str, int, float, bool)):
            safe[key] = raw
        else:
            safe[key] = _truncate(raw, 500)
    return safe


def _extract_expo_error(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    data = body.get("data")
    if isinstance(data, list):
        for item in data:
            error = _extract_expo_error({"data": item})
            if error:
                return error
        return None
    if isinstance(data, dict):
        details = data.get("details")
        if isinstance(details, dict) and isinstance(details.get("error"), str):
            return details["error"]
        if isinstance(data.get("status"), str) and data["status"] == "error":
            return str(data.get("message") or "ExpoPushError")
    errors = body.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            return str(first.get("code") or first.get("message") or "ExpoPushError")
    return None
