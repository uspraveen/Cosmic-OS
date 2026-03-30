from __future__ import annotations

import hmac
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, Field

from ..runtime import GatewayRuntime
from .desktop import DesktopAdapter
from .mobile import MobileAdapter
from shared import is_supported_image_artifact

router = APIRouter(tags=["channels"])
logger = logging.getLogger(__name__)

DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _format_size_limit(size_bytes: int) -> str:
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.0f} MB"
    if size_bytes >= 1024:
        return f"{max(1, round(size_bytes / 1024))} KB"
    return f"{max(1, size_bytes)} B"


class WhatsAppPairingRequest(BaseModel):
    refresh: bool = True
    wait_timeout_ms: int = Field(default=15000, ge=1000, le=60000)


class WhatsAppConfigUpdateRequest(BaseModel):
    allowed_phone: str | None = Field(default=None, max_length=32)
    self_chat_only: bool | None = None


class WhatsAppSendRequest(BaseModel):
    number: str = Field(..., min_length=1, max_length=32)
    message: str = Field(..., min_length=1, max_length=8000)


class TelegramSendRequest(BaseModel):
    chat_id: int
    message: str = Field(..., min_length=1, max_length=8000)


class AgentEmailConfigRequest(BaseModel):
    base_url: str = Field(..., min_length=1, max_length=500)
    api_token: str = Field(..., min_length=1, max_length=1000)
    primary_mailbox_address: str | None = Field(default=None, max_length=320)


class AgentEmailTrustedSendersRequest(BaseModel):
    trusted_senders: list[str] = Field(default_factory=list)


class TaskInputReplyRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=8000)


class MobileDeviceAuthorizeRequest(BaseModel):
    device_id: str = Field(..., min_length=1, max_length=128)


class PauseRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


class SchedulerCreateRequest(BaseModel):
    cron_id: str | None = Field(default=None, max_length=80)
    label: str = Field(..., min_length=1, max_length=200)
    cron_expression: str = Field(..., min_length=1, max_length=64)
    prompt: str = Field(..., min_length=1, max_length=4000)
    one_shot: bool = True
    description: str | None = Field(default=None, max_length=500)
    timezone: str | None = Field(default=None, max_length=64)
    delivery_target: str | None = Field(default=None, max_length=64)
    delivery_channel: str | None = Field(default=None, max_length=128)
    context_summary: str | None = Field(default=None, max_length=800)
    source: str | None = Field(default=None, max_length=120)
    request_id: str | None = Field(default=None, max_length=120)
    session_id: str | None = Field(default=None, max_length=120)
    channel: str | None = Field(default=None, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChannelResolveRequest(BaseModel):
    delivery_target: str | None = Field(default=None, max_length=128)
    current_channel: str | None = Field(default=None, max_length=128)
    fallback_channel: str | None = Field(default=None, max_length=128)


def get_runtime(request: Request) -> GatewayRuntime:
    runtime = getattr(request.app.state, "gateway_runtime", None)
    if runtime is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gateway runtime is not initialized",
        )
    return runtime


def get_websocket_runtime(websocket: WebSocket) -> GatewayRuntime:
    runtime = getattr(websocket.app.state, "gateway_runtime", None)
    if runtime is None:
        raise RuntimeError("Gateway runtime is not initialized")
    return runtime


def _extract_request_token(request: Request) -> str:
    authorization = request.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()

    for header_name in ("X-API-Token", "X-Internal-Token"):
        token = request.headers.get(header_name, "").strip()
        if token:
            return token
    return ""


def _extract_websocket_token(websocket: WebSocket) -> str:
    authorization = websocket.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return websocket.query_params.get("token", "").strip()


def _extract_websocket_device_id(websocket: WebSocket) -> str:
    return (
        websocket.headers.get("x-device-id", "")
        or websocket.query_params.get("device_id", "")
    ).strip()


def _normalize_device_id(value: str) -> str | None:
    if not value:
        return None
    if not DEVICE_ID_PATTERN.fullmatch(value):
        return None
    return value


def _error_payload(request_id: str | None, code: str, message: str) -> dict[str, Any]:
    return {
        "type": "error",
        "request_id": request_id,
        "code": code,
        "message": message,
    }


async def _stage_realtime_uploads(
    *,
    request_id: str,
    device_id: str,
    session_id: str | None,
    files: list[UploadFile],
    platform: str,
    runtime: GatewayRuntime,
    require_live_session: bool = False,
) -> dict[str, Any]:
    normalized_request_id = str(request_id or "").strip()
    normalized_device_id = _normalize_device_id(str(device_id or "").strip())
    normalized_session_id = str(session_id or "").strip()
    if not normalized_request_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="request_id is required")
    if not normalized_device_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="device_id is required")
    if not normalized_session_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="session_id is required")
    if not files:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one attachment is required")

    channel = f"{platform}:{normalized_device_id}"
    if platform == "mobile" and runtime.is_mobile_device_revoked(normalized_device_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This mobile device was removed from desktop. Log in again on the phone to re-authorize it.",
        )
    if require_live_session:
        adapter = runtime.registry.adapters.get(platform)
        valid_adapter = (
            isinstance(adapter, DesktopAdapter)
            if platform == "desktop"
            else isinstance(adapter, MobileAdapter)
        )
        if not valid_adapter:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"{platform.title()} adapter unavailable",
            )
        tracked_session_id = str(await adapter.get_connection_session_id(channel) or "").strip()
        if not tracked_session_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"{platform.title()} session is not active for this device. Reconnect and retry.",
            )
        if tracked_session_id != normalized_session_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"session_id does not match the active {platform} session for this device",
            )

    image_count = sum(
        1
        for upload in files
        if is_supported_image_artifact(
            {
                "mime": str(upload.content_type or "").strip(),
                "filename": str(upload.filename or "").strip(),
            }
        )
    )
    if image_count > runtime.config.max_image_attachments_per_message:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Up to {runtime.config.max_image_attachments_per_message} images can be attached in one message. "
                f"You uploaded {image_count}."
            ),
        )

    uploads: list[dict[str, Any]] = []
    max_file_bytes = max(1024 * 1024, int(runtime.config.docs_upload_max_file_bytes))
    for index, upload in enumerate(files, start=1):
        filename = str(upload.filename or "").strip()
        if not filename:
            continue
        content = await upload.read()
        if not content:
            continue
        if len(content) > max_file_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=(
                    f"Attachment exceeds the {_format_size_limit(max_file_bytes)} upload limit: {filename}"
                ),
            )
        uploads.append(
            {
                "artifact_id": f"{platform}_att_{normalized_request_id}_{index}",
                "filename": filename,
                "mime_type": str(upload.content_type or "").strip() or None,
                "content": content,
            }
        )
    if not uploads:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No valid documents were uploaded")

    manifests = await runtime.stage_desktop_uploads(
        request_id=normalized_request_id,
        session_id=normalized_session_id,
        channel=channel,
        uploads=uploads,
        source_platform=platform,
    )
    if platform == "mobile":
        runtime.record_mobile_device_session(
            normalized_device_id,
            session_id=normalized_session_id,
            channel=channel,
        )
    if not manifests:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No supported document or image files were uploaded",
        )
    return {"attachments": manifests}


def require_local_api_token(
    request: Request,
    runtime: GatewayRuntime = Depends(get_runtime),
) -> None:
    expected = runtime.config.local_api_token
    if not expected:
        return
    if not hmac.compare_digest(_extract_request_token(request), expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


def require_internal_token(
    request: Request,
    runtime: GatewayRuntime = Depends(get_runtime),
) -> None:
    expected = runtime.config.internal_token
    if not expected:
        return
    if not hmac.compare_digest(_extract_request_token(request), expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


async def verify_websocket_auth(websocket: WebSocket, runtime: GatewayRuntime) -> bool:
    expected = runtime.config.local_api_token
    token = _extract_websocket_token(websocket)
    if expected and not hmac.compare_digest(token, expected):
        await websocket.close(code=4001, reason="Invalid token")
        return False

    device_id = _normalize_device_id(_extract_websocket_device_id(websocket))
    if not device_id:
        await websocket.close(code=4002, reason="Missing or invalid device_id")
        return False

    requested_platform = (
        websocket.headers.get("x-device-platform", "")
        or websocket.query_params.get("platform", "")
    ).strip().lower()
    if requested_platform not in {"", "desktop", "mobile"}:
        await websocket.close(code=4003, reason="Invalid platform")
        return False
    platform = requested_platform or ("mobile" if device_id.startswith("mob_") else "desktop")
    if platform == "mobile" and runtime.is_mobile_device_revoked(device_id):
        await websocket.close(code=4004, reason="This device was removed from desktop.")
        return False

    websocket.state.device_id = device_id
    websocket.state.platform = platform
    websocket.state.channel = f"{platform}:{device_id}"
    return True


async def _handle_realtime_websocket_message(
    payload: dict[str, Any],
    *,
    runtime: GatewayRuntime,
    adapter: DesktopAdapter | MobileAdapter,
    channel: str,
    platform: str,
) -> None:
    message_type = str(payload.get("type") or "").strip()
    request_id = str(payload.get("request_id") or "").strip() or None

    if message_type == "ping":
        await runtime.update_user_timezone(payload.get("timezone"), source=platform)
        await adapter.send(
            {
                "type": "pong",
                "ts_unix_ms": payload.get("ts_unix_ms"),
                "server_time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            },
            channel=channel,
        )
        return

    if message_type == "resume":
        known_task_ids = payload.get("known_task_ids")
        if not isinstance(known_task_ids, list):
            known_task_ids = []
        await runtime.update_user_timezone(payload.get("timezone"), source=platform)
        response = await runtime.build_resume_payload(
            channel=channel,
            request_id=request_id,
            requested_session_id=str(payload.get("session_id") or "").strip() or None,
            known_task_ids=[str(item) for item in known_task_ids if str(item).strip()],
        )
        # Track which session this desktop connection belongs to (for cross-channel sync)
        resolved_session_id = str(response.get("session_id") or "").strip()
        if resolved_session_id:
            await adapter.update_session_id(channel, resolved_session_id)
            if platform == "mobile":
                runtime.record_mobile_device_session(
                    channel.split(":", 1)[1],
                    session_id=resolved_session_id,
                    channel=channel,
                )
        await adapter.send(response, channel=channel)
        return

    if message_type == "query":
        try:
            result = await adapter.handle_incoming_message(payload, channel=channel)
        except (TypeError, ValueError) as exc:
            await adapter.send(
                _error_payload(request_id, "INVALID_QUERY", str(exc)),
                channel=channel,
            )
            return

        await adapter.send(
            {
                "type": "route_result",
                "request_id": result["request_id"],
                "session_id": result["session_id"],
                "channel": result["channel"],
                "route": result["route"],
                "classification": result["classification"],
            },
            channel=channel,
        )
        try:
            runtime.start_request_fulfillment(result)
        except Exception as exc:
            await adapter.send(
                _error_payload(
                    request_id,
                    "UPSTREAM_ERROR",
                    str(exc),
                ),
                channel=channel,
            )
        return

    if message_type == "task.input_reply":
        try:
            accepted = await runtime.submit_task_input_reply(
                input_request_id=str(payload.get("input_request_id") or "").strip(),
                task_id=str(payload.get("task_id") or "").strip(),
                content=str(payload.get("content") or "").strip(),
                channel=channel,
            )
        except ValueError as exc:
            await adapter.send(
                _error_payload(
                    request_id,
                    "INVALID_TASK_INPUT_REPLY",
                    str(exc),
                ),
                channel=channel,
            )
            return
        except RuntimeError as exc:
            await adapter.send(
                _error_payload(
                    request_id,
                    "TASK_INPUT_UNAVAILABLE",
                    str(exc),
                ),
                channel=channel,
            )
            return
        await adapter.send(
            {
                "type": "task.input_reply.accepted",
                "request_id": request_id,
                "input_request_id": accepted["input_request_id"],
                "task_id": accepted["task_id"],
                "channel": channel,
                "timestamp": accepted["timestamp"],
            },
            channel=channel,
        )
        return

    if message_type == "cancel":
        task_id = str(payload.get("task_id") or "").strip() or None
        target_request_id = str(payload.get("target_request_id") or "").strip() or None
        try:
            await runtime.cancel_active_fulfillment(
                channel=channel,
                request_id=request_id,
                target_request_id=target_request_id,
                task_id=task_id,
            )
        except ValueError as exc:
            await adapter.send(
                _error_payload(
                    request_id,
                    "INVALID_CANCEL",
                    str(exc),
                ),
                channel=channel,
            )
        return

    if message_type == "background":
        target = str(payload.get("request_id") or "").strip()
        if not target:
            await adapter.send(
                _error_payload(request_id, "INVALID_BACKGROUND", "request_id is required"),
                channel=channel,
            )
            return
        ok = await runtime.background_active_request(channel=channel, request_id=target)
        if not ok:
            await adapter.send(
                _error_payload(
                    request_id,
                    "BACKGROUND_FAILED",
                    "Cannot background this request. It may be completed, not found, or the background task limit has been reached.",
                ),
                channel=channel,
            )
        return

    if message_type == "foreground":
        target = str(payload.get("request_id") or "").strip()
        if not target:
            await adapter.send(
                _error_payload(request_id, "INVALID_FOREGROUND", "request_id is required"),
                channel=channel,
            )
            return
        ok = await runtime.foreground_background_request(channel=channel, request_id=target)
        if not ok:
            await adapter.send(
                _error_payload(
                    request_id,
                    "FOREGROUND_FAILED",
                    "Cannot foreground this request. A foreground task may already be active, or the request was not found.",
                ),
                channel=channel,
            )
        return

    await adapter.send(
        _error_payload(request_id, "UNKNOWN_MESSAGE_TYPE", f"Unsupported WebSocket message type: {message_type or '<missing>'}"),
        channel=channel,
    )


@router.websocket("/ws")
async def desktop_websocket(websocket: WebSocket) -> None:
    runtime = get_websocket_runtime(websocket)
    if not await verify_websocket_auth(websocket, runtime):
        return

    platform = str(getattr(websocket.state, "platform", "desktop") or "desktop")
    adapter = runtime.registry.adapters.get(platform)
    if platform == "desktop":
        valid_adapter = isinstance(adapter, DesktopAdapter)
    elif platform == "mobile":
        valid_adapter = isinstance(adapter, MobileAdapter)
    else:
        valid_adapter = False
    if not valid_adapter:
        await websocket.close(code=1011, reason=f"{platform.title()} adapter unavailable")
        return

    await websocket.accept()
    channel = websocket.state.channel
    device_id = websocket.state.device_id
    await adapter.register_connection(websocket, device_id=device_id, channel=channel)
    runtime.notify_channel_active(channel)
    if platform == "mobile":
        runtime.record_mobile_device_connection(device_id, channel=channel)

    try:
        while True:
            try:
                payload = await websocket.receive_json()
            except ValueError:
                await adapter.send(
                    _error_payload(None, "INVALID_JSON", "WebSocket payload must be valid JSON."),
                    channel=channel,
                )
                continue

            if not isinstance(payload, dict):
                await adapter.send(
                    _error_payload(None, "INVALID_PAYLOAD", "WebSocket payload must be a JSON object."),
                    channel=channel,
                )
                continue

            await _handle_realtime_websocket_message(
                payload,
                runtime=runtime,
                adapter=adapter,
                channel=channel,
                platform=platform,
            )
    except WebSocketDisconnect:
        pass
    finally:
        await adapter.unregister_connection(channel, websocket)
        if platform == "mobile":
            runtime.record_mobile_device_disconnected(device_id)


@router.post("/internal/channels/whatsapp/incoming")
async def whatsapp_incoming(
    payload: dict[str, Any],
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    started_at = time.perf_counter()
    adapter = runtime.registry.adapters.get("whatsapp")
    if adapter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="WhatsApp adapter is not registered")

    try:
        result = adapter.normalize_message(payload)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    request_id = str(result.get("request_id") or "").strip() or uuid4().hex
    result["request_id"] = request_id

    logger.info(
        "whatsapp.incoming normalized channel=%s message_id=%s elapsed_ms=%.1f",
        result.get("channel"),
        result.get("metadata", {}).get("message_id") if isinstance(result.get("metadata"), dict) else None,
        (time.perf_counter() - started_at) * 1000.0,
    )

    try:
        await adapter.send(
            {
                "type": "route_result",
                "request_id": request_id,
                "session_id": None,
                "channel": result["channel"],
                "route": "pending",
                "classification": None,
            },
            channel=result["channel"],
        )
        logger.info(
            "whatsapp.incoming immediate_ack_sent request_id=%s elapsed_ms=%.1f",
            request_id,
            (time.perf_counter() - started_at) * 1000.0,
        )
    except Exception as exc:
        logger.exception(
            "whatsapp.incoming immediate_ack_failed request_id=%s elapsed_ms=%.1f",
            request_id,
            (time.perf_counter() - started_at) * 1000.0,
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    processed = await runtime.process_incoming_user_message(result)
    runtime.notify_channel_active(processed["channel"])
    if processed.get("dispatch_target") == "redis":
        return processed
    logger.info(
        "whatsapp.incoming classified request_id=%s route=%s elapsed_ms=%.1f",
        processed.get("request_id"),
        processed.get("route"),
        (time.perf_counter() - started_at) * 1000.0,
    )

    try:
        runtime.start_request_fulfillment(processed)
    except Exception as exc:
        logger.exception(
            "whatsapp.incoming fulfillment_start_failed request_id=%s elapsed_ms=%.1f",
            processed.get("request_id"),
            (time.perf_counter() - started_at) * 1000.0,
        )
        try:
            await adapter.send(
                _error_payload(
                    processed.get("request_id"),
                    "UPSTREAM_ERROR",
                    str(exc),
                ),
                channel=processed["channel"],
            )
        except Exception:
            logger.exception(
                "whatsapp.incoming error_delivery_failed request_id=%s",
                processed.get("request_id"),
            )
    else:
        logger.info(
            "whatsapp.incoming fulfillment_started request_id=%s elapsed_ms=%.1f",
            processed.get("request_id"),
            (time.perf_counter() - started_at) * 1000.0,
        )
    return processed


@router.post("/internal/channels/agent-email/incoming")
async def agent_email_incoming(
    request: Request,
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    adapter = runtime.registry.adapters.get("agent-email")
    if adapter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent Email adapter is not registered")

    try:
        raw_body = await request.body()
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unable to read webhook body") from exc

    try:
        adapter.verify_webhook_signature(request.headers, raw_body)  # type: ignore[attr-defined]
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Agent Email JSON payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Agent Email webhook payload must be a JSON object")

    try:
        normalized = adapter.normalize_message(payload)  # type: ignore[attr-defined]
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    reserved_keys, duplicate = runtime.reserve_agent_email_inbound(normalized)
    if duplicate is not None:
        return {
            "status": duplicate.get("status") or "duplicate",
            "request_id": duplicate.get("request_id"),
            "session_id": duplicate.get("session_id") or normalized.get("session_id"),
            "channel": duplicate.get("channel") or normalized.get("channel"),
            "duplicate": True,
        }

    try:
        request_id = str(normalized.get("request_id") or "").strip() or uuid4().hex
        normalized["request_id"] = request_id
        processed = await runtime.process_incoming_user_message(normalized)
        runtime.notify_channel_active(processed["channel"])
        if processed.get("dispatch_target") == "redis":
            return {
                "status": "accepted",
                "request_id": processed["request_id"],
                "session_id": processed["session_id"],
                "channel": processed["channel"],
            }

        try:
            runtime.start_request_fulfillment(processed)
        except Exception as exc:
            logger.exception(
                "agent_email.webhook fulfillment_start_failed request_id=%s",
                processed.get("request_id"),
            )
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        return {
            "status": "accepted",
            "request_id": processed["request_id"],
            "session_id": processed["session_id"],
            "channel": processed["channel"],
            "route": processed["route"],
        }
    finally:
        runtime.release_agent_email_inbound(reserved_keys)


@router.post("/channels/telegram/webhook")
async def telegram_webhook(
    request: Request,
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    adapter = runtime.registry.adapters.get("telegram")
    if adapter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Telegram adapter is not registered")

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Telegram JSON payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Telegram update payload must be a JSON object")

    header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    try:
        adapter.verify_webhook_secret(header_secret)  # type: ignore[attr-defined]
        normalized = adapter.normalize_message(payload)  # type: ignore[attr-defined]
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    if normalized is None:
        return {"ok": True, "status": "ignored"}

    started_at = time.perf_counter()
    request_id = str(normalized.get("request_id") or "").strip() or uuid4().hex
    normalized["request_id"] = request_id

    try:
        await adapter.send(  # type: ignore[attr-defined]
            {
                "type": "route_result",
                "request_id": request_id,
                "session_id": None,
                "channel": normalized["channel"],
                "route": "pending",
                "classification": None,
            },
            channel=normalized["channel"],
        )
    except Exception:
        logger.exception(
            "telegram.webhook immediate_ack_failed request_id=%s elapsed_ms=%.1f",
            request_id,
            (time.perf_counter() - started_at) * 1000.0,
        )

    processed = await runtime.process_incoming_user_message(normalized)
    runtime.notify_channel_active(processed["channel"])
    if processed.get("dispatch_target") == "redis":
        return {"ok": True, "status": "accepted", "request_id": processed["request_id"]}
    logger.info(
        "telegram.webhook classified request_id=%s route=%s elapsed_ms=%.1f",
        processed.get("request_id"),
        processed.get("route"),
        (time.perf_counter() - started_at) * 1000.0,
    )
    try:
        runtime.start_request_fulfillment(processed)
    except Exception as exc:
        logger.exception(
            "telegram.webhook fulfillment_start_failed request_id=%s elapsed_ms=%.1f",
            processed.get("request_id"),
            (time.perf_counter() - started_at) * 1000.0,
        )
        try:
            await adapter.send(  # type: ignore[attr-defined]
                _error_payload(processed.get("request_id"), "UPSTREAM_ERROR", str(exc)),
                channel=processed["channel"],
            )
        except Exception:
            logger.exception(
                "telegram.webhook error_delivery_failed request_id=%s",
                processed.get("request_id"),
            )
    else:
        logger.info(
            "telegram.webhook fulfillment_started request_id=%s elapsed_ms=%.1f",
            processed.get("request_id"),
            (time.perf_counter() - started_at) * 1000.0,
        )
    return {"ok": True, "status": "accepted", "request_id": processed["request_id"]}


@router.get("/internal/channels/telegram/media/{file_id}")
async def download_telegram_media(
    file_id: str,
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> Response:
    try:
        content, media_type = await runtime.download_telegram_media(file_id)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Telegram adapter is not registered") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return Response(content=content, media_type=media_type or "application/octet-stream")


@router.get("/sessions")
async def list_sessions(
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return {"sessions": runtime.list_sessions()}


@router.get("/sessions/{session_id}")
async def get_session_history(
    session_id: str,
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return {"session_id": session_id, "messages": runtime.get_session_history(session_id)}


@router.get("/sessions/{session_id}/request-traces")
async def get_session_request_traces(
    session_id: str,
    limit: int = 40,
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "request_traces": runtime.list_session_request_traces(session_id, limit=limit),
    }


@router.get("/channels/mobile/devices")
async def list_mobile_devices(
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return {"devices": await runtime.list_mobile_devices()}


@router.post("/channels/mobile/devices/authorize")
async def authorize_mobile_device(
    payload: MobileDeviceAuthorizeRequest,
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        device = runtime.authorize_mobile_device(payload.device_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"device": device}


@router.post("/channels/mobile/devices/revoke-all")
async def revoke_all_mobile_devices(
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    result = await runtime.revoke_all_mobile_devices()
    return result


@router.delete("/channels/mobile/devices/{device_id}")
async def revoke_mobile_device(
    device_id: str,
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        device = await runtime.revoke_mobile_device(_normalize_device_id(device_id) or "")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"device": device}


@router.get("/artifacts/content/{artifact_id}")
async def get_signed_artifact_content(
    artifact_id: str,
    exp: int,
    sig: str,
    purpose: str = "llm_image_fetch",
    runtime: GatewayRuntime = Depends(get_runtime),
) -> Response:
    try:
        payload = runtime.get_signed_artifact_content(
            artifact_id=artifact_id,
            purpose=purpose,
            expires_at=exp,
            signature=sig,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    filename = str(payload.get("filename") or artifact_id).replace('"', "")
    return Response(
        content=payload["content"],
        media_type=str(payload.get("media_type") or "application/octet-stream"),
        headers={
            "Cache-Control": "private, max-age=300",
            "Content-Disposition": f'inline; filename="{filename}"',
        },
    )


@router.post("/channels/desktop/uploads")
async def upload_desktop_documents(
    request_id: str = Form(...),
    device_id: str = Form(...),
    session_id: str | None = Form(default=None),
    files: list[UploadFile] = File(...),
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return await _stage_realtime_uploads(
        request_id=request_id,
        device_id=device_id,
        session_id=session_id,
        files=files,
        platform="desktop",
        runtime=runtime,
    )


@router.post("/channels/mobile/uploads")
async def upload_mobile_documents(
    request_id: str = Form(...),
    device_id: str = Form(...),
    session_id: str | None = Form(default=None),
    files: list[UploadFile] = File(...),
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return await _stage_realtime_uploads(
        request_id=request_id,
        device_id=device_id,
        session_id=session_id,
        files=files,
        platform="mobile",
        runtime=runtime,
        require_live_session=True,
    )


@router.post("/tasks/{task_id}/input-reply/{input_request_id}")
async def submit_task_input_reply(
    task_id: str,
    input_request_id: str,
    payload: TaskInputReplyRequest,
    request: Request,
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    channel = (
        request.headers.get("X-Channel", "").strip()
        or str(request.query_params.get("channel") or "").strip()
    )
    if not channel:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="channel is required via X-Channel header or query parameter",
        )
    try:
        accepted = await runtime.submit_task_input_reply(
            input_request_id=input_request_id,
            task_id=task_id,
            content=payload.content,
            channel=channel,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return {
        "type": "task.input_reply.accepted",
        "input_request_id": accepted["input_request_id"],
        "task_id": accepted["task_id"],
        "channel": channel,
        "timestamp": accepted["timestamp"],
    }


@router.get("/routing-audit")
async def list_routing_audit(
    limit: int = 50,
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return {"entries": runtime.list_routing_audit(limit=max(1, min(limit, 500)))}


@router.get("/scheduler/overview")
async def scheduler_overview(
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return runtime.scheduler_overview()


@router.get("/scheduler/crons")
async def list_scheduler_crons(
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return {"crons": runtime.list_scheduler_crons()}


@router.post("/internal/scheduler/crons")
async def create_internal_scheduler_cron(
    body: SchedulerCreateRequest,
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        return await runtime.create_scheduler_cron(
            cron_id=body.cron_id,
            label=body.label,
            cron_expression=body.cron_expression,
            prompt=body.prompt,
            one_shot=body.one_shot,
            description=body.description,
            timezone_name=body.timezone,
            delivery_target=body.delivery_target,
            delivery_channel=body.delivery_channel,
            metadata=body.metadata,
            created_by=body.source,
            created_request_id=body.request_id,
            created_session_id=body.session_id,
            created_channel=body.channel,
            context_summary=body.context_summary,
        )
    except ValueError as exc:
        detail = str(exc)
        status_code = status.HTTP_409_CONFLICT if "already exists" in detail.lower() else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=status_code, detail=detail) from exc


@router.get("/internal/scheduler/crons")
async def list_internal_scheduler_crons(
    include_system: bool = False,
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return {"crons": runtime.list_scheduler_crons(include_system=include_system, active_only=True)}


@router.get("/internal/scheduler/crons/{cron_id}")
async def get_internal_scheduler_cron(
    cron_id: str,
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    payload = runtime.get_scheduler_cron(cron_id)
    if payload is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown cron")
    return payload


@router.delete("/internal/scheduler/crons/{cron_id}")
async def delete_internal_scheduler_cron(
    cron_id: str,
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        deleted = runtime.delete_scheduler_cron(cron_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown cron")
    return {"deleted": True, "cron_id": cron_id}


@router.get("/scheduler/crons/{cron_id}")
async def get_scheduler_cron(
    cron_id: str,
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    payload = runtime.get_scheduler_cron(cron_id)
    if payload is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown cron")
    return payload


@router.post("/scheduler/crons/{cron_id}/pause")
async def pause_scheduler_cron(
    cron_id: str,
    body: PauseRequest,
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    payload = runtime.pause_scheduler_cron(cron_id, reason=body.reason)
    if payload is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown cron")
    return payload


@router.post("/internal/scheduler/crons/{cron_id}/pause")
async def pause_internal_scheduler_cron(
    cron_id: str,
    body: PauseRequest,
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    payload = runtime.pause_scheduler_cron(cron_id, reason=body.reason)
    if payload is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown cron")
    return payload


@router.post("/scheduler/crons/{cron_id}/resume")
async def resume_scheduler_cron(
    cron_id: str,
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    payload = runtime.resume_scheduler_cron(cron_id)
    if payload is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown cron")
    return payload


@router.post("/internal/scheduler/crons/{cron_id}/resume")
async def resume_internal_scheduler_cron(
    cron_id: str,
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    payload = runtime.resume_scheduler_cron(cron_id)
    if payload is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown cron")
    return payload


@router.post("/internal/channels/resolve")
async def resolve_internal_channel(
    body: ChannelResolveRequest,
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        return runtime.resolve_channel_target(
            delivery_target=body.delivery_target,
            current_channel=body.current_channel,
            fallback_channel=body.fallback_channel,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/scheduler/heartbeat")
async def get_scheduler_heartbeat(
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return runtime.get_scheduler_heartbeat()


@router.post("/scheduler/heartbeat/pause")
async def pause_scheduler_heartbeat(
    body: PauseRequest,
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return runtime.pause_scheduler_heartbeat(reason=body.reason)


@router.post("/scheduler/heartbeat/resume")
async def resume_scheduler_heartbeat(
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return runtime.resume_scheduler_heartbeat()


@router.get("/channels")
async def list_channels(
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return {"channels": runtime.list_channels()}


@router.get("/desktop/system-metrics")
async def get_desktop_system_metrics(
    force_refresh: bool = False,
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return await runtime.get_desktop_system_metrics(force_refresh=force_refresh)


@router.get("/desktop/registry-agents")
async def get_desktop_registry_agents(
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return await runtime.get_desktop_registry_agents()


@router.get("/desktop/messages/{message_id}/artifacts/{artifact_id}/download")
async def download_desktop_output_artifact(
    message_id: str,
    artifact_id: str,
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> Response:
    try:
        payload = runtime.get_desktop_output_artifact_content(
            message_id=message_id,
            artifact_id=artifact_id,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    filename = str(payload.get("filename") or artifact_id).replace('"', "")
    return Response(
        content=payload["content"],
        media_type=str(payload.get("media_type") or "application/octet-stream"),
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.post("/channels/whatsapp/pairing/qr")
async def request_whatsapp_pairing_qr(
    body: WhatsAppPairingRequest,
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        payload = await runtime.request_whatsapp_pairing_qr(
            refresh=body.refresh,
            wait_timeout_ms=body.wait_timeout_ms,
        )
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="WhatsApp adapter is not registered") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return payload


@router.delete("/channels/whatsapp/session")
async def clear_whatsapp_session(
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        payload = await runtime.clear_whatsapp_session()
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="WhatsApp adapter is not registered") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return payload


@router.get("/channels/whatsapp/config")
async def get_whatsapp_config(
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        payload = await runtime.get_whatsapp_config()
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="WhatsApp adapter is not registered") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return payload


@router.post("/channels/whatsapp/config")
async def update_whatsapp_config(
    body: WhatsAppConfigUpdateRequest,
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        payload = await runtime.update_whatsapp_config(
            allowed_phone=body.allowed_phone,
            self_chat_only=body.self_chat_only,
        )
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="WhatsApp adapter is not registered") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return payload


@router.post("/channels/whatsapp/send")
async def send_whatsapp_message(
    body: WhatsAppSendRequest,
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        payload = await runtime.send_whatsapp_test(
            number=body.number,
            message=body.message,
        )
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="WhatsApp adapter is not registered") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return payload


@router.post("/channels/telegram/webhook/sync")
async def sync_telegram_webhook(
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        payload = await runtime.sync_telegram_webhook()
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Telegram adapter is not registered") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return payload


@router.delete("/channels/telegram/webhook")
async def clear_telegram_webhook(
    drop_pending_updates: bool = False,
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        payload = await runtime.clear_telegram_webhook(drop_pending_updates=drop_pending_updates)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Telegram adapter is not registered") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return payload


@router.post("/channels/telegram/send")
async def send_telegram_message(
    body: TelegramSendRequest,
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        payload = await runtime.send_telegram_test(chat_id=body.chat_id, message=body.message)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Telegram adapter is not registered") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return payload


@router.get("/channels/agent-email/status")
async def get_agent_email_status(
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return await runtime.get_agent_email_connection_status()


@router.post("/channels/agent-email/config")
async def save_agent_email_config(
    body: AgentEmailConfigRequest,
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        return await runtime.save_agent_email_connection(
            base_url=body.base_url,
            api_token=body.api_token,
            primary_mailbox_address=body.primary_mailbox_address,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.delete("/channels/agent-email/config")
async def clear_agent_email_config(
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return await runtime.clear_agent_email_connection()


@router.post("/channels/agent-email/trusted-senders")
async def save_agent_email_trusted_senders(
    body: AgentEmailTrustedSendersRequest,
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        return await runtime.save_agent_email_trusted_senders(body.trusted_senders)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.get("/channels/{platform}/status")
async def get_channel_status(
    platform: str,
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        return await runtime.get_channel_status(platform)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown channel platform: {platform}") from exc
