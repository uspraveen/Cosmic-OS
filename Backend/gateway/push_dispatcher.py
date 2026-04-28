from __future__ import annotations

import logging
import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Awaitable

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PushNotification:
    device_id: str
    token: str
    title: str
    body: str
    data: dict[str, Any]
    priority: str = "default"
    fcm_token: str = ""
    platform: str = ""


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
        fcm_project_id: str = "",
        fcm_service_account_file: Path | None = None,
        fcm_service_account_json: str = "",
        unregister_token: Callable[[str], Awaitable[None]] | None = None,
        unregister_fcm_token: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.enabled = enabled
        self.access_token = access_token.strip()
        self.push_url = push_url.strip()
        self.fcm_project_id = fcm_project_id.strip()
        self._fcm_service_account = _load_service_account(
            fcm_service_account_file,
            fcm_service_account_json,
        )
        if not self.fcm_project_id and self._fcm_service_account:
            self.fcm_project_id = str(self._fcm_service_account.get("project_id") or "").strip()
        self._fcm_access_token = ""
        self._fcm_access_token_expires_at = 0.0
        self.unregister_token = unregister_token
        self.unregister_fcm_token = unregister_fcm_token
        timeout = httpx.Timeout(timeout_sec, connect=min(timeout_sec, 5.0))
        self._client = httpx.AsyncClient(timeout=timeout, http2=True)

    async def close(self) -> None:
        await self._client.aclose()

    async def send(self, notification: PushNotification) -> bool:
        if not self.enabled:
            return False
        if await self._send_fcm_if_available(notification):
            return True
        if not notification.token or not self.push_url:
            return False

        payload = {
            "to": notification.token,
            "title": _truncate(notification.title, 80),
            "body": _truncate(notification.body, 180),
            "data": _json_safe_dict(notification.data, max_items=24),
            "sound": "default",
            "priority": "high" if notification.priority == "high" else "default",
            "channelId": _notification_channel(notification),
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

    async def _send_fcm_if_available(self, notification: PushNotification) -> bool:
        if not _is_android(notification.platform):
            return False
        if not notification.fcm_token or not self.fcm_project_id or not self._fcm_service_account:
            return False

        access_token = await self._get_fcm_access_token()
        if not access_token:
            return False

        channel_id = _notification_channel(notification)
        payload = {
            "message": {
                "token": notification.fcm_token,
                "notification": {
                    "title": _truncate(notification.title, 80),
                    "body": _truncate(notification.body, 180),
                },
                "data": _string_dict(
                    {
                        **_json_safe_dict(notification.data, max_items=24),
                        "screen": notification.data.get("screen"),
                    }
                ),
                "android": {
                    "priority": "HIGH" if notification.priority == "high" else "NORMAL",
                    "ttl": "3600s",
                    "notification": {
                        "channel_id": channel_id,
                        "color": "#79c9ff",
                        "sound": "default",
                        "visibility": "PUBLIC",
                        "notification_priority": (
                            "PRIORITY_MAX"
                            if notification.priority == "high"
                            else "PRIORITY_HIGH"
                        ),
                    },
                },
            }
        }
        url = f"https://fcm.googleapis.com/v1/projects/{self.fcm_project_id}/messages:send"
        try:
            response = await self._client.post(
                url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if response.status_code >= 400:
                error = _extract_fcm_error(response)
                logger.warning(
                    "gateway.push.fcm_rejected device_id=%s status=%s error=%s",
                    notification.device_id,
                    response.status_code,
                    error,
                )
                if error == "UNREGISTERED" and self.unregister_fcm_token is not None:
                    try:
                        await self.unregister_fcm_token(notification.device_id)
                    except Exception:
                        logger.exception(
                            "gateway.push.unregister_fcm_failed device_id=%s",
                            notification.device_id,
                        )
                return False
            logger.info(
                "gateway.push.fcm_sent device_id=%s type=%s",
                notification.device_id,
                notification.data.get("type"),
            )
            return True
        except Exception as exc:
            logger.warning(
                "gateway.push.fcm_send_failed device_id=%s reason=%s",
                notification.device_id,
                exc,
            )
            return False

    async def _get_fcm_access_token(self) -> str:
        now = time.time()
        if self._fcm_access_token and self._fcm_access_token_expires_at > now + 60:
            return self._fcm_access_token
        assertion = _build_service_account_assertion(self._fcm_service_account)
        if not assertion:
            return ""
        try:
            response = await self._client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            logger.warning("gateway.push.fcm_token_failed reason=%s", exc)
            return ""
        access_token = str(body.get("access_token") or "").strip()
        expires_in = int(body.get("expires_in") or 3600)
        if not access_token:
            return ""
        self._fcm_access_token = access_token
        self._fcm_access_token_expires_at = now + max(60, expires_in)
        return access_token


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


def _string_dict(value: dict[str, Any]) -> dict[str, str]:
    return {
        str(key): str(raw)
        for key, raw in value.items()
        if key and raw is not None and str(raw) != ""
    }


def _is_android(platform: str) -> bool:
    return str(platform or "").strip().lower() == "android"


def _notification_channel(notification: PushNotification) -> str:
    override = str(notification.data.get("channel_id") or "").strip()
    if override:
        return override
    event_type = str(notification.data.get("type") or "").strip()
    if event_type.startswith("agent_email.") or event_type.startswith("email."):
        return "cosmic-email"
    if event_type == "task.input_required" or event_type in {
        "task.failed",
        "task.cancelled",
    }:
        return "cosmic-actions"
    if event_type == "response.complete":
        return "cosmic-chat"
    return "default"


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


def _load_service_account(path: Path | None, raw_json: str) -> dict[str, Any] | None:
    try:
        if raw_json.strip():
            parsed = json.loads(raw_json)
            return parsed if isinstance(parsed, dict) else None
        if path and path.exists():
            parsed = json.loads(path.read_text(encoding="utf-8"))
            return parsed if isinstance(parsed, dict) else None
    except Exception:
        logger.exception("gateway.push.fcm_service_account_load_failed")
    return None


def _build_service_account_assertion(service_account: dict[str, Any] | None) -> str:
    if not service_account:
        return ""
    client_email = str(service_account.get("client_email") or "").strip()
    private_key_pem = str(service_account.get("private_key") or "").strip()
    if not client_email or not private_key_pem:
        return ""
    issued_at = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {
        "iss": client_email,
        "scope": "https://www.googleapis.com/auth/firebase.messaging",
        "aud": "https://oauth2.googleapis.com/token",
        "iat": issued_at,
        "exp": issued_at + 3600,
    }
    signing_input = ".".join(
        [
            _b64url_json(header),
            _b64url_json(claims),
        ]
    )
    try:
        private_key = serialization.load_pem_private_key(
            private_key_pem.encode("utf-8"),
            password=None,
        )
        signature = private_key.sign(
            signing_input.encode("ascii"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except Exception:
        logger.exception("gateway.push.fcm_assertion_sign_failed")
        return ""
    return f"{signing_input}.{_b64url(signature)}"


def _b64url_json(value: dict[str, Any]) -> str:
    return _b64url(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _extract_fcm_error(response: httpx.Response) -> str:
    try:
        body = response.json()
    except Exception:
        return response.text[:300]
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return str(body)[:300]
    details = error.get("details")
    if isinstance(details, list):
        for detail in details:
            if isinstance(detail, dict):
                code = detail.get("errorCode") or detail.get("reason")
                if code:
                    return str(code)
    return str(error.get("status") or error.get("message") or body)[:300]
