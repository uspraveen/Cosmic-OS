from __future__ import annotations

import hmac
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, Field

from ..runtime import GatewayRuntime
from .desktop import DesktopAdapter

router = APIRouter(tags=["channels"])

DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class WhatsAppPairingRequest(BaseModel):
    refresh: bool = True
    wait_timeout_ms: int = Field(default=15000, ge=1000, le=60000)


class WhatsAppConfigUpdateRequest(BaseModel):
    allowed_phone: str | None = Field(default=None, max_length=32)
    self_chat_only: bool | None = None


class WhatsAppSendRequest(BaseModel):
    number: str = Field(..., min_length=1, max_length=32)
    message: str = Field(..., min_length=1, max_length=8000)


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

    websocket.state.device_id = device_id
    websocket.state.channel = f"desktop:{device_id}"
    return True


async def _handle_desktop_websocket_message(
    payload: dict[str, Any],
    *,
    runtime: GatewayRuntime,
    adapter: DesktopAdapter,
    channel: str,
) -> None:
    message_type = str(payload.get("type") or "").strip()
    request_id = str(payload.get("request_id") or "").strip() or None

    if message_type == "ping":
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
        response = await runtime.build_resume_payload(
            channel=channel,
            request_id=request_id,
            requested_session_id=str(payload.get("session_id") or "").strip() or None,
            known_task_ids=[str(item) for item in known_task_ids if str(item).strip()],
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
            await runtime.fulfill_processed_message(result)
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
        await adapter.send(
            _error_payload(
                request_id,
                "NOT_IMPLEMENTED",
                "task.input_reply is not implemented in this backend build yet.",
            ),
            channel=channel,
        )
        return

    if message_type == "cancel":
        await adapter.send(
            _error_payload(
                request_id,
                "NOT_IMPLEMENTED",
                "cancel is not implemented in this backend build yet.",
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

    adapter = runtime.registry.adapters.get("desktop")
    if not isinstance(adapter, DesktopAdapter):
        await websocket.close(code=1011, reason="Desktop adapter unavailable")
        return

    await websocket.accept()
    channel = websocket.state.channel
    device_id = websocket.state.device_id
    await adapter.register_connection(websocket, device_id=device_id, channel=channel)

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

            await _handle_desktop_websocket_message(
                payload,
                runtime=runtime,
                adapter=adapter,
                channel=channel,
            )
    except WebSocketDisconnect:
        pass
    finally:
        await adapter.unregister_connection(channel, websocket)


@router.post("/internal/channels/whatsapp/incoming")
async def whatsapp_incoming(
    payload: dict[str, Any],
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    adapter = runtime.registry.adapters.get("whatsapp")
    if adapter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="WhatsApp adapter is not registered")

    try:
        result = adapter.normalize_message(payload)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    processed = await runtime.process_incoming_user_message(result)
    try:
        await runtime.fulfill_processed_message(processed)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return processed


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


@router.get("/channels")
async def list_channels(
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return {"channels": runtime.list_channels()}


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
